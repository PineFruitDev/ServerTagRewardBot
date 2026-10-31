"""Main Bot class that owns the Discord client and command management."""

import discord
from discord import app_commands

from src.core.command import Command
from src.core.command_manager import CommandManager
from src.services.logger import get_logger
from src.services.store_client import StoreClient, StoreError, StoreUnavailable

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
        original = getattr(error, "original", error)
        log.error("tree - Error executing /%s: %s",
                  getattr(interaction.command, "name", "?"), original)
        if isinstance(original, StoreUnavailable):
            message = ("⚠️ I can't reach my storage right now, so I couldn't do that. "
                       "Nothing was lost; please try again in a minute.")
        elif isinstance(original, StoreError):
            message = "⚠️ My storage rejected that request. The bot owner has been notified in the logs."
        else:
            message = "❌ An error occurred while executing this command."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass
