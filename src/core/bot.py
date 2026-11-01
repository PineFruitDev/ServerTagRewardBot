"""Main Bot class that owns the Discord client and command management."""

import discord
from discord import app_commands

from src.core.command import Command
from src.core.command_manager import CommandManager
from src.services import discord_retry
from src.services.discord_errors import API_FAILURES, describe, user_message
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
        """Push the command tree to Discord. Returns how many commands synced.

        Retried on a rate limit or an outage. Global command registration is
        one of the tightest buckets Discord has, and this runs on every
        on_ready, so a reconnect storm is exactly when it gets refused. Failing
        it without a retry leaves the bot connected but with stale commands.
        """
        synced = await discord_retry.call("tree.sync", self.tree.sync)
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
        name = getattr(interaction.command, "name", "?")
        if isinstance(original, StoreUnavailable):
            # An expected outage: one line, no traceback.
            log.error("tree - /%s could not reach the store: %s", name, original)
            message = ("⚠️ I can't reach my storage right now, so I couldn't do that. "
                       "Nothing was lost; please try again in a minute.")
        elif isinstance(original, StoreError):
            log.error("tree - /%s was rejected by the store: %s", name, original)
            message = "⚠️ My storage rejected that request. The bot owner has been notified in the logs."
        elif isinstance(original, API_FAILURES):
            # Discord refused the call rather than the bot breaking. The status
            # separates a fixable setup problem from an outage, so report it
            # as such instead of "an error occurred", and skip the traceback.
            # RateLimited is included deliberately: it is not an HTTPException,
            # so matching on that alone reported a rate limit as a crash.
            log.error("tree - /%s failed against the Discord API: %s", name, describe(original))
            message = user_message(original)
        else:
            # Unexpected, so keep the traceback: it is the only record of the bug.
            log.error("tree - Error executing /%s: %s", name, original, exc_info=original)
            message = "❌ An error occurred while executing this command."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except API_FAILURES:
            # Not retried: the interaction token is single-use for the initial
            # response and expires, so a delayed resend earns a 40060 rather
            # than delivering anything. Nothing is left to report to if the
            # report itself cannot be delivered.
            pass
