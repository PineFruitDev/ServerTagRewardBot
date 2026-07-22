"""Help command that automatically uses all registered commands.

Demonstrates how the centralized registry makes help generation automatic.
"""

import discord
from discord import app_commands

from src.core.command import Command, CommandHelpInfo


class HelpCommand(Command):
    help_info = CommandHelpInfo(
        name="help",
        description="Get help with bot commands and see detailed usage instructions",
        usage="/help [command]",
        examples=["/help", "/help command:ping"],
        category="Utility",
    )

    def __init__(self, all_commands: list[Command]):
        self._all_commands = all_commands

    def register(self, tree: app_commands.CommandTree) -> None:
        @tree.command(name="help", description="Get help with bot commands")
        @app_commands.describe(command="Get detailed help for a specific command")
        async def help_cmd(interaction: discord.Interaction, command: str | None = None) -> None:
            if not await self.guard(interaction):
                return
            if command:
                await self._show_specific(interaction, command)
            else:
                await self._show_all(interaction)

    async def execute(self, interaction: discord.Interaction) -> None:
        await self._show_all(interaction)

    async def _show_specific(self, interaction: discord.Interaction, name: str) -> None:
        target = next((cmd for cmd in self._all_commands if cmd.name == name.lower()), None)

        if target is None:
            await interaction.response.send_message(
                f"❌ Command \"{name}\" not found.", ephemeral=True
            )
            return

        info = target.help_info
        embed = (
            discord.Embed(title=f"📖 Help: /{info.name}", description=info.description, color=0x00AE86)
            .add_field(name="📋 Usage", value=f"`{info.usage}`", inline=False)
            .add_field(name="🎯 Examples", value="\n".join(f"`{ex}`" for ex in info.examples), inline=False)
            .add_field(name="📂 Category", value=info.category, inline=True)
        )

        restrictions = []
        if target.guild_only:
            restrictions.append("Server only")
        if target.developer_only:
            restrictions.append("Developer only")
        if restrictions:
            embed.add_field(name="⚠️ Restrictions", value=", ".join(restrictions), inline=True)

        embed.timestamp = discord.utils.utcnow()
        await interaction.response.send_message(embed=embed)

    async def _show_all(self, interaction: discord.Interaction) -> None:
        categories: dict[str, list[Command]] = {}
        for cmd in self._all_commands:
            categories.setdefault(cmd.help_info.category, []).append(cmd)

        embed = discord.Embed(
            title="🤖 Bot Commands",
            description="Here are all available commands, organized by category.",
            color=0x5865F2,
        )
        embed.set_footer(text="Use /help command:<name> for detailed help on a specific command")
        embed.timestamp = discord.utils.utcnow()

        for category, commands in categories.items():
            listing = "\n".join(f"`/{cmd.name}` - {cmd.description}" for cmd in commands)
            embed.add_field(name=f"📂 {category}", value=listing, inline=False)

        await interaction.response.send_message(embed=embed)
