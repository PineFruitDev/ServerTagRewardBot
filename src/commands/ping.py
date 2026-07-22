"""Simple ping command - demonstrates basic command structure."""

import discord
from discord import app_commands

from src.core.command import Command, CommandHelpInfo


class PingCommand(Command):
    help_info = CommandHelpInfo(
        name="ping",
        description="Check if the bot is responding and get latency information",
        usage="/ping",
        examples=["/ping"],
        category="Utility",
    )

    def register(self, tree: app_commands.CommandTree) -> None:
        @tree.command(name="ping", description="Check if the bot is responding")
        async def ping(interaction: discord.Interaction) -> None:
            if not await self.guard(interaction):
                return
            await self.execute(interaction)

    async def execute(self, interaction: discord.Interaction) -> None:
        api_latency = round(interaction.client.latency * 1000)
        await interaction.response.send_message(
            f"🏓 **Pong!**\n"
            f"💓 **API Latency:** {api_latency}ms"
        )
