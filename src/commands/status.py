"""Status command - shows this server's tag sync statistics."""

from typing import NamedTuple

import discord
from discord import app_commands

from src.core.command import Command, CommandHelpInfo
from src.core.options import require_guild_scope
from src.services.guild_config import GuildConfig
from src.services.tag_sync import GuildStats, SYNC_PAUSED_MESSAGE, TagSyncService

#: The embed is the whole reply, so its colour is the part an admin reads
#: before any field. Blurple for a server that is working, red for a reward
#: role the bot cannot manage at all, yellow for a stall that passes on its own.
OK_COLOR = 0x5865F2
BLOCKED_COLOR = 0xED4245
STALLED_COLOR = 0xFEE75C


class Problem(NamedTuple):
    """A reward problem worth putting in front of the statistics."""

    name: str
    detail: str
    color: int


def problem(stats: GuildStats) -> Problem | None:
    """What is stopping this server's rewards, or None when nothing is.

    Both causes are recorded by apply_role on the live gateway path as well as
    by a full sync, and that is what makes them worth reporting here. A server
    whose reward role sits above the bot's own has every grant refused from the
    first member who equips the tag, with no sync having run: the statistics
    below are then all zero, last_sync_at is still "Not yet run", and the embed
    reads as a server where nothing has happened yet rather than one where
    nothing can. /sync reports it, but /sync is the most expensive thing the bot
    can be asked to do, and /status is where an admin looks first.

    The two are not flattened into one warning, because they want opposite
    responses: role_blocked is a permission the admin has to go and change,
    while stalled_after passes on its own and must not send them into Server
    Settings after something that was never their configuration.
    """
    if stats.role_blocked is not None:
        return Problem(
            "⚠️ Rewards Are Not Being Applied",
            f"I can't manage the reward role ({stats.role_blocked}), so nobody is "
            "getting it and nobody is losing it. Give me **Manage Roles**, and make "
            "sure my own highest role sits above the reward role in "
            "Server Settings > Roles.",
            BLOCKED_COLOR,
        )
    if stats.stalled_after is not None:
        return Problem(
            "⚠️ Last Full Sync Stopped Early",
            f"{stats.stalled_after} role updates failed in a row, after retries, so I "
            "stopped instead of repeating that for every member. That usually means "
            "Discord is rate limiting me or having trouble, rather than anything here "
            "needing to be changed. Nothing was lost; run /sync again shortly.",
            STALLED_COLOR,
        )
    return None


class StatusCommand(Command):
    help_info = CommandHelpInfo(
        name="status",
        description="Show this server's sync statistics: members checked, roles added/removed, live updates",
        usage="/status",
        examples=["/status"],
        category="Utility",
    )

    guild_only = True

    def register(self, tree: app_commands.CommandTree) -> None:
        @tree.command(name="status", description="Show tag reward sync status for this server")
        async def status(interaction: discord.Interaction) -> None:
            async with self.observed(interaction):
                if not await self.guard(interaction):
                    return
                await self.execute(interaction)

    async def execute(self, interaction: discord.Interaction) -> None:
        guild_id = await require_guild_scope(interaction)
        if guild_id is None:
            return
        role_id = await GuildConfig.get_instance().get_role(guild_id)

        if role_id is None:
            await interaction.response.send_message(
                "ℹ️ This server isn't set up yet. An admin can run /setup role:@Role to get started.",
                ephemeral=True,
            )
            return

        service = TagSyncService.get_instance()
        if service is None:
            await interaction.response.send_message(
                f"🎁 Reward role: <@&{role_id}>\n{SYNC_PAUSED_MESSAGE}", ephemeral=True
            )
            return

        stats = service.stats_for(guild_id)

        last_sync = "Not yet run"
        if stats.last_sync_at is not None:
            last_sync = f"<t:{int(stats.last_sync_at.timestamp())}:R>"

        reported = problem(stats)
        embed = discord.Embed(
            title="🏷️ Tag Reward Status",
            color=OK_COLOR if reported is None else reported.color,
        )
        if reported is not None:
            # First, and spanning the row rather than sharing it: every count
            # below is explained by this, and a warning an admin has to look
            # past the statistics to find is one they will read as a footnote.
            embed.add_field(name=reported.name, value=reported.detail, inline=False)

        embed = (
            embed.add_field(name="🎁 Reward Role", value=f"<@&{role_id}>", inline=True)
            .add_field(name="🔄 Sync State", value="Running" if service.is_syncing(guild_id) else "Idle", inline=True)
            .add_field(name="👥 Members Checked", value=str(stats.members_checked), inline=True)
            .add_field(name="🏷️ Repping the Tag", value=str(stats.currently_repping), inline=True)
            .add_field(name="➕ Roles Added", value=str(stats.roles_added), inline=True)
            .add_field(name="➖ Roles Removed", value=str(stats.roles_removed), inline=True)
            .add_field(name="⚡ Live Updates Seen", value=str(stats.live_updates), inline=True)
            .add_field(name="⏭️ Duplicate Updates Skipped",
                       value=str(stats.duplicates_collapsed), inline=True)
            .add_field(name="🕐 Last Full Sync", value=last_sync, inline=True)
        )
        embed.timestamp = discord.utils.utcnow()

        await interaction.response.send_message(embed=embed)
