"""This entire file is bad idea on bad idea"""
from __future__ import annotations

import inspect
import logging
from functools import partial
from io import BytesIO
from operator import itemgetter
from typing import TYPE_CHECKING, Any, Callable, Optional, Union, cast

import discord
import orjson
import utils
from discord.ext import commands
from discord.ext.commands.core import (get_converter, get_signature_parameters,
                                       run_converters, unwrap_function)
from discord.http import Route
from discord.types.interactions import (
    ApplicationCommandInteractionData as SlashCommandData,
    ApplicationCommandInteractionDataOption as SlashCommandOption
)

if TYPE_CHECKING:
    from main import TTSBot


NoneType = type(None)
type_lookup = {
    discord.User: 6, discord.Member: 6, discord.abc.GuildChannel: 7,
    discord.Role: 8, bool: 5, int: 4, float: 10, str: 3, discord.Object: 9,
}


def parse_options(command: commands.Command) -> list[dict[str, Any]]:
    if isinstance(command, commands.Group):
        return [
            {
                "name": subcommand.name,
                "description": subcommand.help or "no description",
                "type": 2 if isinstance(subcommand, commands.Group) else 1,
                "options": parse_options(subcommand)
            }
            for subcommand in command.commands
        ]

    # Parses all the params into options then filters out all the None options
    # (like self or ctx) then sorts required before optional
    return sorted(filter(None, (
        parse_param(name, param)
        for name, param in get_params(command).items()
    )), key=itemgetter("required"), reverse=True)

def get_params(command: commands.Command) -> dict[str, inspect.Parameter]:
    callback = command.callback
    func = cast(Callable, getattr(callback, "__original_func__", callback))

    unwrap = unwrap_function(func)
    globalns = getattr(unwrap, "__globals__", {})
    return get_signature_parameters(func, globalns)

def parse_param(name: str, param: inspect.Parameter) -> Optional[dict[str, Any]]:
    if name in {"self", "ctx"}:
        return

    # Unwraps Union types and similar to a tuple of their types
    param_types: Union[tuple[type, ...], tuple[object, ...]] = getattr(
        param.annotation, "__args__", (param.annotation,)
    )

    option = {
        "type": None,
        "name": name,
        "required": NoneType not in param_types,
    }

    if all(not isinstance(t, type) for t in param_types):
        param_types = cast(tuple[object, ...], param_types)
        option["choices"] = [{
            "name": literal_value,
            "value": literal_value
        } for literal_value in param_types]

    param_types = tuple(t if isinstance(t, type) else type(t) for t in param_types)
    option["description"] = f"Type(s): {', '.join(t.__name__ for t in param_types)}"

    for original, num in type_lookup.items():
        for param_type in param_types:
            if issubclass(param_type, original):
                option["type"] = num
                break

        if option["type"] is not None:
            break

    if option["type"] is None:
        option["type"] = type_lookup[str]

    return option

def unpack_group(group: commands.Group, subcommands: list[str]) -> commands.Command:
    for subcommand in subcommands:
        group = group.all_commands[subcommand] # type: ignore

    return group # type: ignore

async def convert_params(
    ctx: utils.TypedContext,
    options: list[SlashCommandOption]
) -> tuple[list[Union[utils.CommonCog, utils.TypedContext]], dict[str, Any]]:
    if ctx.guild is None:
        get_channel = ctx.bot.get_channel
        fetch_user = ctx.bot.fetch_user
        get_role = lambda _: None
    else:
        get_channel = ctx.guild.get_channel
        fetch_user = ctx.guild.fetch_member
        get_role = ctx.guild.get_role

    kwargs = {}
    for option in options:
        ctx.bot.logger.debug(f"\nParsing {option}")
        if option["type"] in {1, 2}:
            continue

        cleaned_value = option.get("value")
        ctx.bot.logger.debug(f"Cleaned value {cleaned_value} {type(cleaned_value)} | Option Type {option['type']}")

        if option["type"] == 3:
            param = ctx.command.params[option["name"]]
            converter = get_converter(param)

            if converter != str:
                coro = run_converters(ctx, converter, option["value"], param)
                cleaned_value = await coro
        elif option["type"] == 6:
            cleaned_value = await fetch_user(int(option["value"]))
        elif option["type"] == 7:
            cleaned_value = get_channel(int(option["value"]))
        elif option["type"] == 8:
            cleaned_value = get_role(int(option["value"]))
        elif option["type"] == 9:
            cleaned_value = discord.Object(int(option["value"]))

        ctx.bot.logger.debug(f"Parsed: {cleaned_value}")
        kwargs[option["name"]] = cleaned_value

    return [ctx.cog, ctx], kwargs


async def _parse_arguments(ctx: utils.TypedContext, callback=None):
    if ctx.interaction is None or callback is None:
        await callback(ctx)

async def defer_task(ctx: utils.TypedContext):
    assert ctx.interaction is not None

    if not ctx.interaction.response.is_done():
        await ctx.interaction.response.defer()
        ctx.defered = True # type: ignore

def setup(bot: TTSBot):
    bot.add_cog(SlashCommands(bot))


class SlashCommands(utils.CommonCog):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if self.bot.cluster_id in {None, 0}:
            self.bot.create_task(self.ready_commands())

    async def ready_commands(self):
        await self.bot.wait_until_ready()
        headers = {"Authorization": f"Bot {self.bot.http.token}"}
        url = f"{Route.BASE}/applications/{self.bot.application_id}/commands"
        #url = f"{Route.BASE}/applications/{self.bot.application_id}/guilds/{self.bot.get_support_server().id}/commands"

        slash_commands = [
            {
                "name": command.name,
                "description": command.help or "no description",
                "options": parse_options(command),
            }
            for command in self.bot.commands
            if not (command.hidden or command.name == "help")
        ]

        if logging.DEBUG >= self.bot.logger.level:
            await self.bot.channels["logs"].send(file=discord.File(
                BytesIO(
                    orjson.dumps(
                        slash_commands,
                        option=orjson.OPT_INDENT_2
                    )
                ), filename="slash_commands.json"
            ))

        async with self.bot.session.put(url, headers=headers, json=slash_commands) as resp:
            if not resp.ok:
                self.bot.logger.error(f"Error {resp.status}: {await resp.json()}")

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        self.bot.logger.debug(f"Interaction: {interaction.data}")
        if interaction.type != discord.InteractionType.application_command:
            return

        assert interaction.user is not None
        assert interaction.channel is not None

        if not isinstance(interaction.channel, (
            discord.TextChannel, discord.DMChannel,
            discord.Thread, discord.PartialMessageable,
        )):
            msg = "Sorry, but this channel type is unsupported for commands!"
            log_msg = f"Slash Command invoked in unsupported channel type: {type(interaction.channel)}"

            self.bot.logger.error(log_msg)
            return await interaction.response.send_message(msg, ephemeral=True)

        interaction.data = cast(SlashCommandData, interaction.data)
        command_options = interaction.data.get("options") or []
        command_name = interaction.data["name"]

        # I am going to hell for this
        message = cast(discord.Message, utils.construct_unslotted(
            cls=discord.PartialMessage,
            channel=interaction.channel,
            id=interaction.id
        ))

        prefix = await self.bot.command_prefix(self.bot, message)
        message.edited_at = None # type: ignore
        message.author = interaction.user

        subcommands = []
        for option in command_options:
            if option["type"] in {1, 2}:
                subcommand = option["name"]
                command_options = option.get("options") or []

                subcommands.append(subcommand)
                command_name += " " + subcommand

        message.content = f"{prefix}{command_name}"
        message.clean_content = message.content # type: ignore

        ctx = await self.bot.get_context(message)
        self.bot.loop.call_later(2.5, self.bot.create_task, defer_task(ctx))
        if ctx.command:
            ctx.prefix = "/"
            ctx.interaction = interaction

            # Unpacks ctx.command to be a group
            if isinstance(ctx.command, commands.Group):
                ctx.command = unpack_group(ctx.command, subcommands)

            ctx.args, ctx.kwargs = await convert_params(ctx, command_options)
            if not hasattr(ctx.command._parse_arguments, "func"):
                # if not already done before, patch _parse_arguments
                # to return if there is an interaction, as we already do
                # parsing via convert_params
                ctx.command._parse_arguments = partial(_parse_arguments, callback=ctx.command._parse_arguments)

            try:
                self.bot.log("on_slash_command")
                self.bot.logger.debug(f"\nInvoking {ctx.command}")
                await ctx.command.invoke(ctx)
            except commands.CommandError as err:
                await ctx.command.dispatch_error(ctx, err)
            else:
                defered = getattr(ctx, "defered", False)
                if defered or not ctx.interaction.response.is_done():
                    await ctx.send(f"Finished `{command_name}`!")
        else:
            self.bot.log("on_unknown_slash_command")
            await ctx.send("I cannot find this command! Please wait 1 hour if the bot has just updated")
