"""Status command - shows current tag sync statistics."""

import discord
from discord import app_commands

from src.core.command import Command, CommandHelpInfo
from src.services.environment import Environment
from src.services.tag_sync import TagSyncService


class StatusCommand(Command):
    help_info = CommandHelpInfo(
        name="status",
        description="Show sync statistics: members checked, roles added/removed, and live update count",
        usage="/status",
        examples=["/status"],
        category="Utility",
    )

    guild_only = True

    def register(self, tree: app_commands.CommandTree) -> None:
        @tree.command(name="status", description="Show tag reward sync status and statistics")
        async def status(interaction: discord.Interaction) -> None:
            if not await self.guard(interaction):
                return
            await self.execute(interaction)

    async def execute(self, interaction: discord.Interaction) -> None:
        service = TagSyncService.get_instance()

        if service is None:
            await interaction.response.send_message("❌ Tag sync service is not running.", ephemeral=True)
            return

        stats = service.stats
        config = Environment.get_config()

        last_sync = "Not yet run"
        if stats.last_sync_at is not None:
            last_sync = f"<t:{int(stats.last_sync_at.timestamp())}:R>"

        embed = (
            discord.Embed(title="🏷️ Tag Reward Status", color=0x5865F2)
            .add_field(name="🎁 Reward Role", value=f"<@&{config.tag_role_id}>", inline=True)
            .add_field(name="🔄 Sync State", value="Running" if service.syncing else "Idle", inline=True)
            .add_field(name="👥 Members Checked", value=str(stats.members_checked), inline=True)
            .add_field(name="🏷️ Repping the Tag", value=str(stats.currently_repping), inline=True)
            .add_field(name="➕ Roles Added", value=str(stats.roles_added), inline=True)
            .add_field(name="➖ Roles Removed", value=str(stats.roles_removed), inline=True)
            .add_field(name="⚡ Live Updates Seen", value=str(stats.live_updates), inline=True)
            .add_field(name="🕐 Last Full Sync", value=last_sync, inline=True)
        )
        embed.timestamp = discord.utils.utcnow()

        await interaction.response.send_message(embed=embed)
