"""Leaderboard command - who has repped the server tag the longest."""

import discord
from discord import app_commands

from src.core.command import Command, CommandHelpInfo
from src.services.guild_config import GuildConfig
from src.services.streaks import StreakStore, humanize

_MEDALS = ["🥇", "🥈", "🥉"]

_SORT_LABELS = {
    "current": "Current Streak",
    "longest": "Longest Streak",
    "total": "All-Time Repped",
}


class LeaderboardCommand(Command):
    help_info = CommandHelpInfo(
        name="leaderboard",
        description="Top tag reppers in this server by current, longest, or all-time streak",
        usage="/leaderboard [sort]",
        examples=["/leaderboard", "/leaderboard sort:longest"],
        category="Streaks",
    )

    guild_only = True

    def register(self, tree: app_commands.CommandTree) -> None:
        @tree.command(name="leaderboard", description="Top tag reppers in this server")
        @app_commands.describe(sort="How to rank reppers (defaults to current streak)")
        @app_commands.choices(sort=[
            app_commands.Choice(name="current streak", value="current"),
            app_commands.Choice(name="longest streak", value="longest"),
            app_commands.Choice(name="all-time repped", value="total"),
        ])
        async def leaderboard(interaction: discord.Interaction,
                              sort: app_commands.Choice[str] | None = None) -> None:
            if not await self.guard(interaction):
                return
            await self._show(interaction, sort.value if sort else "current")

    async def execute(self, interaction: discord.Interaction) -> None:
        await self._show(interaction, "current")

    async def _show(self, interaction: discord.Interaction, sort: str) -> None:
        guild_id = str(interaction.guild_id)

        if GuildConfig.get_instance().get_role(guild_id) is None:
            await interaction.response.send_message(
                "ℹ️ This server isn't set up yet. An admin can run /setup role:@Role to get started.",
                ephemeral=True,
            )
            return

        top = StreakStore.get_instance().leaderboard(guild_id, sort=sort, limit=10)

        if not top:
            await interaction.response.send_message(
                "📭 Nobody has repped the tag yet. Be the first: equip the server tag and start your streak!",
            )
            return

        lines = []
        for rank, (user_id, seconds) in enumerate(top):
            medal = _MEDALS[rank] if rank < len(_MEDALS) else f"`#{rank + 1}`"
            lines.append(f"{medal} <@{user_id}> — **{humanize(seconds)}**")

        embed = discord.Embed(
            title=f"🏆 Tag Rep Leaderboard — {_SORT_LABELS[sort]}",
            description="\n".join(lines),
            color=0xF1C40F,
        )
        embed.set_footer(text="Equip the server tag to climb the board")
        embed.timestamp = discord.utils.utcnow()

        await interaction.response.send_message(embed=embed)
