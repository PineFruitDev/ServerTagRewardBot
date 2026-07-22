"""Register slash commands with Discord, then exit.

Run this after adding or changing commands: python register.py
"""

import asyncio

import discord
from dotenv import load_dotenv

from src.commands import ALL_COMMANDS
from src.core.bot import Bot
from src.services.environment import Environment
from src.services.logger import get_logger

log = get_logger("Register")

load_dotenv()


def main() -> None:
    Environment.validate()
    config = Environment.get_config()

    bot = Bot(ALL_COMMANDS, intents=discord.Intents.default())

    @bot.event
    async def on_ready() -> None:
        try:
            count = await bot.sync_commands()
            for command in ALL_COMMANDS:
                info = command.help_info
                log.info("✅ Registered: /%s [%s] - %s", info.name, info.category, info.description)
            log.info("Successfully reloaded %d application (/) commands", count)
        finally:
            await bot.close()

    bot.run(config.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
