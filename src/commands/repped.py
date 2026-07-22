"""Repped command - show how long a member has been repping the server tag."""

import discord
from discord import app_commands

from src.core.command import Command, CommandHelpInfo
from src.services.guild_config import GuildConfig
from src.services.streaks import StreakStore, humanize


class ReppedCommand(Command):
    help_info = CommandHelpInfo(
        name="repped",
        description="See how long you (or someone else) have been repping the server tag",
        usage="/repped [user]",
        examples=["/repped", "/repped user:@Pine"],
        category="Streaks",
    )

    guild_only = True

    def register(self, tree: app_commands.CommandTree) -> None:
        @tree.command(name="repped", description="See how long you (or someone else) have been repping the server tag")
        @app_commands.describe(user="Whose streak to check (defaults to you)")
        async def repped(interaction: discord.Interaction, user: discord.Member | None = None) -> None:
            if not await self.guard(interaction):
                return
            await self._show(interaction, user or interaction.user)

    async def execute(self, interaction: discord.Interaction) -> None:
        await self._show(interaction, interaction.user)

    async def _show(self, interaction: discord.Interaction, member: discord.Member) -> None:
        guild_id = str(interaction.guild_id)

        if GuildConfig.get_instance().get_role(guild_id) is None:
            await interaction.response.send_message(
                "ℹ️ This server isn't set up yet. An admin can run /setup role:@Role to get started.",
                ephemeral=True,
            )
            return

        stats = StreakStore.get_instance().get(guild_id, str(member.id))

        if member.bot:
            await interaction.response.send_message("🤖 Bots don't rep tags. Yet.", ephemeral=True)
            return

        title = "🏷️ Tag Streak"
        if stats["active"]:
            headline = f"{member.display_name} has been repping the tag for **{humanize(stats['current'])}**"
        elif stats["total"] > 0:
            headline = f"{member.display_name} isn't repping the tag right now"
        else:
            headline = f"{member.display_name} hasn't repped the tag yet"

        embed = (
            discord.Embed(title=title, description=headline, color=0x5865F2)
            .add_field(name="🔥 Current Streak", value=humanize(stats["current"]) if stats["active"] else "—", inline=True)
            .add_field(name="🏆 Longest Streak", value=humanize(stats["longest"]) if stats["longest"] else "—", inline=True)
            .add_field(name="⏱️ All-Time Repped", value=humanize(stats["total"]) if stats["total"] else "—", inline=True)
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.timestamp = discord.utils.utcnow()

        await interaction.response.send_message(embed=embed)
