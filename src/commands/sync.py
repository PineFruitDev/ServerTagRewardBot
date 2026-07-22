"""Manual full sync command - reconciles the reward role for every member."""

import discord
from discord import app_commands

from src.core.command import Command, CommandHelpInfo
from src.services.tag_sync import TagSyncService


class SyncCommand(Command):
    help_info = CommandHelpInfo(
        name="sync",
        description="Walk every member and reconcile the reward role against their equipped server tag",
        usage="/sync",
        examples=["/sync"],
        category="Admin",
    )

    required_permissions = ("manage_roles",)
    guild_only = True

    def register(self, tree: app_commands.CommandTree) -> None:
        @tree.command(name="sync", description="Run a full member sync of the tag reward role")
        @app_commands.default_permissions(manage_guild=True)
        async def sync(interaction: discord.Interaction) -> None:
            if not await self.guard(interaction):
                return
            await self.execute(interaction)

    async def execute(self, interaction: discord.Interaction) -> None:
        service = TagSyncService.get_instance()

        if service is None:
            await interaction.response.send_message("❌ Tag sync service is not running.", ephemeral=True)
            return

        if service.syncing:
            await interaction.response.send_message("⏳ A sync is already in progress.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        stats = await service.full_sync()

        await interaction.followup.send(
            f"✅ **Sync complete**\n"
            f"👥 **Members checked:** {stats.members_checked}\n"
            f"🏷️ **Repping the tag:** {stats.currently_repping}\n"
            f"➕ **Roles added:** {stats.roles_added}\n"
            f"➖ **Roles removed:** {stats.roles_removed}"
        )
