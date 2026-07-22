"""Main entry point for ServerTagRewardBot."""

import asyncio

import discord
from dotenv import load_dotenv

from src.commands import ALL_COMMANDS
from src.core.bot import Bot
from src.services.environment import Environment
from src.services.guild_config import GuildConfig
from src.services.logger import get_logger
from src.services.tag_sync import TagSyncService

log = get_logger("Main")

# Load environment variables
load_dotenv()


def main() -> None:
    Environment.validate()
    config = Environment.get_config()

    log.info("Initializing bot with %d commands", len(ALL_COMMANDS))

    # Per-guild reward roles live here (public bot, unlimited servers)
    GuildConfig()

    # GuildMembers is required for member update events (privileged intent)
    intents = discord.Intents.default()
    intents.members = True

    # enable_debug_events exposes raw gateway dispatches for tag detection
    bot = Bot(ALL_COMMANDS, intents=intents, enable_debug_events=True)

    # Attach the tag sync engine
    tag_sync = TagSyncService(bot)

    @bot.event
    async def on_ready() -> None:
        log.info("on_ready - Logged in as %s across %d guilds", bot.user, len(bot.guilds))
        # Hosted mode: make sure slash commands are registered, then reconcile
        if not getattr(bot, "_commands_synced", False):
            bot._commands_synced = True
            await bot.sync_commands()
        if config.sync_on_start:  # off by default; initial sync happens in /setup
            asyncio.create_task(tag_sync.sync_all_guilds())

    bot.run(config.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
