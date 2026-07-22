"""Setup command - walks an admin through configuring this server's reward."""

import asyncio

import discord
from discord import app_commands

from src.core.command import Command, CommandHelpInfo
from src.services.guild_config import GuildConfig
from src.services.tag_sync import TagSyncService


class SyncPromptView(discord.ui.View):
    """Final step of setup: offer to run the first sync right away."""

    def __init__(self, guild_id: str):
        super().__init__(timeout=300)
        self.guild_id = guild_id

    async def _finish(self, interaction: discord.Interaction, message: str) -> None:
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content=message, view=self)

    @discord.ui.button(label="Sync now", style=discord.ButtonStyle.primary, emoji="🔄")
    async def sync_now(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._finish(interaction, "🔄 Syncing your server tag rewards now. Check /status for progress.")
        service = TagSyncService.get_instance()
        if service is not None and not service.is_syncing(self.guild_id):
            asyncio.create_task(service.full_sync(self.guild_id))

    @discord.ui.button(label="Later", style=discord.ButtonStyle.secondary)
    async def later(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._finish(
            interaction,
            "👍 No problem. Members get the role as they equip the tag, or run /sync anytime to catch everyone at once.",
        )


class SetupCommand(Command):
    help_info = CommandHelpInfo(
        name="setup",
        description="Choose the role members earn while repping this server's tag",
        usage="/setup role:@Role",
        examples=["/setup role:@Tag Crew"],
        category="Admin",
    )

    required_permissions = ("manage_roles",)
    guild_only = True

    def register(self, tree: app_commands.CommandTree) -> None:
        @tree.command(name="setup", description="Choose the role members earn while repping this server's tag")
        @app_commands.describe(role="The role to grant while the server tag is equipped")
        @app_commands.default_permissions(manage_guild=True)
        async def setup(interaction: discord.Interaction, role: discord.Role) -> None:
            if not await self.guard(interaction):
                return
            await self._configure(interaction, role)

        @tree.command(name="disable", description="Stop managing the tag reward role in this server")
        @app_commands.default_permissions(manage_guild=True)
        async def disable(interaction: discord.Interaction) -> None:
            if not await self.guard(interaction):
                return
            was_configured = GuildConfig.get_instance().disable(str(interaction.guild_id))
            message = (
                "🛑 Tag rewards disabled for this server. Existing roles are left as they are; run /setup to turn it back on."
                if was_configured else
                "ℹ️ This server wasn't configured yet. Run /setup to get started."
            )
            await interaction.response.send_message(message, ephemeral=True)

    async def execute(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message("Use /setup role:@Role to configure.", ephemeral=True)

    async def _configure(self, interaction: discord.Interaction, role: discord.Role) -> None:
        guild = interaction.guild

        # Sanity checks that save admins a support message later
        if role.is_default() or role.managed:
            await interaction.response.send_message(
                "❌ That role can't be assigned by bots. Pick a regular server role.", ephemeral=True
            )
            return

        if guild.me.top_role <= role:
            await interaction.response.send_message(
                f"❌ My highest role is below {role.mention}. Drag my role above it in "
                "Server Settings, Roles, then run /setup again.", ephemeral=True
            )
            return

        GuildConfig.get_instance().set_role(str(guild.id), str(role.id))

        await interaction.response.send_message(
            f"✅ Set up! Members repping this server's tag now receive {role.mention}.\n\n"
            "**Would you like to sync your server tag rewards now?** "
            "This checks every current member once so existing tag-wearers get the role immediately.",
            view=SyncPromptView(str(guild.id)),
            ephemeral=True,
        )
