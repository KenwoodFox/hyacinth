from __future__ import annotations

import asyncio
import logging
import random
import re
from typing import Any, Callable, Pattern

import discord
import wrapt
from discord import Message, Thread

from notifier_bot.discord.notifier_setup import CraigslistNotifierSetupInteraction
from notifier_bot.discord.thread_interaction import ThreadInteraction
from notifier_bot.models import SearchSpecSource
from notifier_bot.monitor import MarketplaceMonitor
from notifier_bot.notifier import DiscordNotifier
from notifier_bot.settings import get_settings

settings = get_settings()
_logger = logging.getLogger(__name__)

_discord_notifier_bot_commands: dict[Pattern, Callable] = {}

AFFIRMATIONS = ["Okay", "Sure", "Sounds good", "No problem", "Roger that", "Got it"]
THANKS = [*AFFIRMATIONS, "Thanks", "Thank you"]
DEBUG_COMMAND_PREFIX = r"(d|debug) "


class DiscordNotifierBot:
    def __init__(
        self,
        client: discord.Client,
        command_prefix: str | None = "$",
    ) -> None:

        self.client = client
        self.command_prefix = command_prefix

        self.monitor = MarketplaceMonitor()
        self.notifiers: dict[int, DiscordNotifier] = {}  # channel ID -> notifier
        self.active_threads: dict[int, ThreadInteraction] = {}  # thread ID -> setup handler

    def get_command_from_message(self, message: Message) -> str | None:
        """
        Get the bot command string from a raw Discord message.

        If the message is not a bot command, return None.
        """
        # all mentions are automatically interpreted as commands
        if self.client.user is not None and self.client.user.mentioned_in(message):
            mention_regex = rf"<@!?{self.client.user.id}>"
            command = re.sub(mention_regex, "", message.content, 1).strip()
            return command

        # alternatively, commands can be prefixed with a string to indicate they are for the bot
        elif self.command_prefix is not None and message.content.startswith(self.command_prefix):
            command = message.content[len(self.command_prefix) :].strip()
            return command

        return None

    async def on_message(self, message: Message) -> None:
        _logger.debug(f"Received message: {message}")
        # if this bot sent the message, never do anything
        if message.author == self.client.user:
            return

        # if the message is in a thread with an ongoing setup process, pass it to the setup handler
        if isinstance(message.channel, Thread) and message.channel.id in self.active_threads:
            thread_interaction = self.active_threads[message.channel.id]
            await thread_interaction.on_message(message)
            if thread_interaction.completed:
                _logger.debug(f"Completed interaction on thread {message.channel.id}")
                await thread_interaction.finish()
                self.active_threads.pop(message.channel.id)
            return

        # otherwise check if the message is a command and pass it to the appropriate command handler
        command = self.get_command_from_message(message)
        if command is None:
            return

        _logger.info(f"Received command: {command}")
        for pattern in _discord_notifier_bot_commands:
            match = pattern.match(command)
            if match:
                await _discord_notifier_bot_commands[pattern](self, message, match)
                break

    @staticmethod
    def command(r: str) -> Callable[..., Any]:
        """
        Helper decorator for defining bot commands matching a given regex.

        After receiving a command, the bot will call the first @command function whose regex
        matches the given command.
        """

        def deco(f: Callable[..., Any]) -> Callable[..., Any]:
            @wrapt.decorator
            def wrapper(
                wrapped: Callable[..., Any], _instance: Any, args: list, kwargs: dict
            ) -> Any:
                return wrapped(*args, **kwargs)

            _discord_notifier_bot_commands[re.compile(r, re.IGNORECASE)] = f

            return wrapper

        return deco

    def affirmation(self) -> str:
        return random.choice(AFFIRMATIONS)

    def thank(self) -> str:
        return random.choice(THANKS)

    @command(r"notify (?P<source_name>.+?)( (?P<params>(\w+=\w+ ?)+)$|$)")
    async def create_notifier(self, message: Message, command: re.Match) -> None:
        source_name: str = command.group("source_name").lower()
        params: dict[str, str] | None = None
        if command.group("params"):
            params = dict((p.split("=") for p in command.group("params").split(" ")))
        _logger.info(f"Received request to create notifier from {source_name=} {params=}")

        try:
            source = SearchSpecSource(source_name)
        except ValueError:
            await message.channel.send(
                f'Sorry {message.author.mention}, "{source_name}" is not a source I support sending'
                " notifications for."
            )
            return

        if source == SearchSpecSource.CRAIGSLIST:
            setup_interaction = CraigslistNotifierSetupInteraction(self, message)
        else:
            raise NotImplementedError(f"{source_name} not implemented")

        if not params:
            await setup_interaction.begin()
            self.active_threads[setup_interaction.thread_id] = setup_interaction
        else:
            setup_interaction.answers = params
            await setup_interaction.finish()
            await message.channel.send(
                f"{self.affirmation()} {message.author.mention}, I've created a notifier for you"
                " based on the parameters you gave."
            )


async def run() -> None:
    loop = asyncio.get_running_loop()

    intents = discord.Intents(messages=True, guild_messages=True, message_content=True, guilds=True)
    client = discord.Client(intents=intents, loop=loop)
    discord_bot: DiscordNotifierBot = DiscordNotifierBot(client)

    @client.event
    async def on_ready() -> None:
        _logger.info(f"We have logged in as {client.user}")

    @client.event
    async def on_message(message: Message) -> None:
        await discord_bot.on_message(message)

    await client.start(settings.discord_token)