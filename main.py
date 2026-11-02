"""Main entry point for ServerTagRewardBot."""

import asyncio
import sys

import discord
from dotenv import load_dotenv

from src.commands import ALL_COMMANDS
from src.core.bot import Bot
from src.services.environment import Config, Environment
from src.services.guild_config import GuildConfig
from src.services.lifecycle import serve
from src.services.streaks import StreakStore
from src.services.store_client import StoreClient, StoreError
from src.services.logger import configure_library_logging, get_logger
from src.services.tag_sync import TagSyncService
from src.services.tasks import spawn

log = get_logger("Main")

# Load environment variables
load_dotenv()


def build_bot(config: Config, members: bool) -> Bot:
    """Create the client. Without the members intent, tag sync is off (limited mode)."""
    intents = discord.Intents.default()
    # GuildMembers is required for member update events (privileged intent)
    intents.members = members

    # enable_debug_events exposes raw gateway dispatches for tag detection
    bot = Bot(ALL_COMMANDS, intents=intents, enable_debug_events=members)
    bot.limited_mode = not members

    tag_sync = TagSyncService(bot) if members else None
    if tag_sync is None:
        TagSyncService.detach()

    commands_sync_lock = asyncio.Lock()

    @bot.event
    async def on_ready() -> None:
        mode = "LIMITED (no Server Members intent; tag sync off)" if bot.limited_mode else "FULL (tag sync on)"
        log.info("on_ready - Logged in as %s across %d guilds, mode: %s", bot.user, len(bot.guilds), mode)
        # Hosted mode: make sure slash commands are registered, then reconcile
        async with commands_sync_lock:
            if not bot.commands_synced:
                await bot.sync_commands()
                bot.commands_synced = True
        try:
            log.info("on_ready - Store reachable, %d guilds configured", await GuildConfig.get_instance().count())
        except StoreError as exc:
            log.error("on_ready - Store check failed (%s); commands will report it until it recovers", exc)
            return
        if tag_sync is not None and config.sync_on_start:  # off by default; initial sync happens in /setup
            spawn(tag_sync.sync_all_guilds(), name="startup-sync")

    return bot


async def start(config: Config, members: bool) -> None:
    bot = build_bot(config, members)
    # Leaving the block closes the gateway and the store session, which serve()
    # relies on: it drains background work first, while both are still usable.
    async with bot:
        await serve(bot, config.discord_token)


async def run(config: Config) -> None:
    if not config.members_intent:
        log.warning("run - MEMBERS_INTENT=false: starting in LIMITED mode (tag sync off, commands on)")
        await start(config, members=False)
        return
    try:
        await start(config, members=True)
    except discord.PrivilegedIntentsRequired:
        log.warning(
            "run - Discord rejected the SERVER MEMBERS privileged intent (close code 4014). It is not "
            "enabled/approved for this bot yet (Developer Portal > Bot > Privileged Gateway Intents; verified "
            "bots need Discord's approval). Reconnecting in LIMITED mode: slash commands keep working, "
            "live tag sync and role updates are OFF until the intent is approved and the bot restarts."
        )
        await start(config, members=False)


def main() -> None:
    Environment.validate()
    config = Environment.get_config()

    # discord.py reports rate limits and gateway trouble on its own loggers.
    # Adopt them before connecting so those lines arrive structured and
    # redacted rather than raw on stderr.
    configure_library_logging()

    log.info("Initializing bot with %d commands", len(ALL_COMMANDS))

    # Per-guild reward roles + streak history live in tagbot-store (Cloudflare D1),
    # reached over HTTPS. Nothing is stored on the bot host.
    store = StoreClient()
    GuildConfig(store)
    StreakStore(store)

    try:
        asyncio.run(run(config))
    except discord.PrivilegedIntentsRequired:
        log.critical("main - Discord rejected the connection even without privileged intents. Check the bot's settings.")
        sys.exit(1)
    except discord.LoginFailure:
        log.critical("main - Discord rejected DISCORD_TOKEN (invalid or reset). Update .env and restart.")
        sys.exit(1)
    except KeyboardInterrupt:
        # Only reachable for a Ctrl-C landing before the loop installed its
        # handlers; after that, serve() handles SIGINT without raising.
        log.info("main - Interrupted before startup finished")
    log.info("main - Shutdown complete")


if __name__ == "__main__":
    main()
