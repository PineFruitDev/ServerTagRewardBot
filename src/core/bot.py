"""Main Bot class that owns the Discord client and command management."""

import discord
from discord import app_commands

from src.core.command import Command
from src.core.command_manager import CommandManager
from src.services.command_errors import report
from src.services.logger import get_logger
from src.services.store_client import StoreClient, StoreError

log = get_logger("Bot")


class Bot(discord.Client):
    """Discord client wired to the centralized command registry."""

    def __init__(self, commands: list[Command], intents: discord.Intents | None = None, **client_options):
        super().__init__(intents=intents or discord.Intents.default(), **client_options)
        self.tree = app_commands.CommandTree(self)
        self.command_manager = CommandManager(commands)
        self.command_manager.register_all(self.tree)
        self.tree.on_error = self._on_tree_error
        # True when running without the Server Members intent (tag sync off)
        self.limited_mode = False

    async def on_ready(self) -> None:
        log.info("on_ready - Logged in as %s", self.user)

    async def sync_commands(self) -> int:
        """Push the command tree to Discord. Returns how many commands synced."""
        synced = await self.tree.sync()
        log.info("sync_commands - Synced %d application (/) commands", len(synced))
        return len(synced)

    async def close(self) -> None:
        try:
            await StoreClient.get_instance().close()
        except StoreError:
            pass
        await super().close()

    async def _on_tree_error(self, interaction: discord.Interaction, error: Exception) -> None:
        """Report a slash command failure. Called by the tree, never directly.

        The classification, the wording and the ephemeral delivery all live in
        src/services/command_errors.py, because the buttons a command attaches
        to its own reply fail on a separate discord.py surface and have to
        answer the same way.
        """
        name = getattr(interaction.command, "name", "?")
        await report(interaction, error, subject=f"/{name}")
