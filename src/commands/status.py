"""Status command - shows this server's tag sync statistics."""

import discord
from discord import app_commands

from src.core.command import Command, CommandHelpInfo
from src.services.guild_config import GuildConfig
from src.services.tag_sync import TagSyncService


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
            if not await self.guard(interaction):
                return
            await self.execute(interaction)

    async def execute(self, interaction: discord.Interaction) -> None:
        guild_id = str(interaction.guild_id)
        role_id = GuildConfig.get_instance().get_role(guild_id)

        if role_id is None:
            await interaction.response.send_message(
                "ℹ️ This server isn't set up yet. An admin can run /setup role:@Role to get started.",
                ephemeral=True,
            )
            return

        service = TagSyncService.get_instance()
        if service is None:
            await interaction.response.send_message("❌ Tag sync service is not running.", ephemeral=True)
            return

        stats = service.stats_for(guild_id)

        last_sync = "Not yet run"
        if stats.last_sync_at is not None:
            last_sync = f"<t:{int(stats.last_sync_at.timestamp())}:R>"

        embed = (
            discord.Embed(title="🏷️ Tag Reward Status", color=0x5865F2)
            .add_field(name="🎁 Reward Role", value=f"<@&{role_id}>", inline=True)
            .add_field(name="🔄 Sync State", value="Running" if service.is_syncing(guild_id) else "Idle", inline=True)
            .add_field(name="👥 Members Checked", value=str(stats.members_checked), inline=True)
            .add_field(name="🏷️ Repping the Tag", value=str(stats.currently_repping), inline=True)
            .add_field(name="➕ Roles Added", value=str(stats.roles_added), inline=True)
            .add_field(name="➖ Roles Removed", value=str(stats.roles_removed), inline=True)
            .add_field(name="⚡ Live Updates Seen", value=str(stats.live_updates), inline=True)
            .add_field(name="🕐 Last Full Sync", value=last_sync, inline=True)
        )
        embed.timestamp = discord.utils.utcnow()

        await interaction.response.send_message(embed=embed)
