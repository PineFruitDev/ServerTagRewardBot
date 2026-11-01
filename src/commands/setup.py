"""Setup command - walks an admin through configuring this server's reward."""

import discord
from discord import app_commands

from src.core.command import Command, CommandHelpInfo, user_permissions
from src.services.guild_config import GuildConfig
from src.services.tag_sync import SYNC_PAUSED_MESSAGE, TagSyncService
from src.services.tasks import spawn


def handout_refusal(interaction: discord.Interaction, role: discord.Role) -> str | None:
    """Why this invoker may not make `role` the reward, or None when they may.

    Mirrors Discord's own rule for handing out a role: you need Manage Roles,
    and the role has to sit below your own highest one. Manage Server, which
    is what /setup gates on, confers no role control at all, so without this
    a member holding only Manage Server could name any role beneath the bot
    as the reward, equip the tag and collect it.

    Only the guild owner is exempt from the rank half. Administrator grants
    every permission but does not lift you over the hierarchy, so a non-owner
    Administrator still cannot hand out a role above their own; exempting them
    here would reopen the climb from one rung higher up.

    Refuses when it cannot tell, rather than falling through to the write.
    """
    guild = interaction.guild
    if guild is None:
        return "I can't tell which server this is. Run /setup again from the server itself."

    # The reward lands on everyone wearing the tag, so Administrator would make
    # equipping a tag a route to taking the server over. Checked before the
    # owner exemption: nobody opts into this, the owner included.
    if role.permissions.administrator:
        return (f"{role.mention} has the **Administrator** permission, so I won't hand it out "
                "automatically to everyone repping the tag. Pick a role without it.")

    if getattr(guild, "owner_id", None) == interaction.user.id:
        return None  # the owner sits above every role by definition

    held = user_permissions(interaction)
    author_top = getattr(interaction.user, "top_role", None)
    if held is None or author_top is None:
        return ("I can't read your roles in this server yet. "
                "Give me a moment and run /setup again.")

    if not getattr(held, "manage_roles", False):
        return ("You need **Manage Roles** as well as Manage Server to pick the reward, "
                "because I hand that role out on your behalf. Ask an admin to run /setup.")

    if author_top <= role:
        return (f"{role.mention} is not below your own highest role, so you can't make it the "
                "reward. Pick a role beneath yours, or ask an admin ranked above it.")

    return None


class SyncPromptView(discord.ui.View):
    """Final step of setup: offer to run the first sync right away."""

    def __init__(self, guild_id: str, author_id: int):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Only the admin who ran /setup may press these.

        "Sync now" starts the same full member walk /sync gates on Manage
        Server, so it cannot be the one path in with no check of its own. The
        reply is ephemeral, which in practice means nobody else is shown the
        buttons, but that is a keyword in another function and this does not
        depend on it.
        """
        if interaction.user.id == self.author_id:
            return True
        await interaction.response.send_message(
            "❌ Only the admin who ran /setup can use these buttons.", ephemeral=True
        )
        return False

    async def _finish(self, interaction: discord.Interaction, message: str) -> None:
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content=message, view=self)

    @discord.ui.button(label="Sync now", style=discord.ButtonStyle.primary, emoji="🔄")
    async def sync_now(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        service = TagSyncService.get_instance()
        if service is None:
            await self._finish(interaction, f"✅ Saved. {SYNC_PAUSED_MESSAGE}")
            return
        await self._finish(interaction, "🔄 Syncing your server tag rewards now. Check /status for progress.")
        if not service.is_syncing(self.guild_id):
            spawn(service.safe_full_sync(self.guild_id), name=f"setup-sync-{self.guild_id}")

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
    required_user_permissions = ("manage_guild", "administrator")
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
            was_configured = await GuildConfig.get_instance().disable(str(interaction.guild_id))
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

        refusal = handout_refusal(interaction, role)
        if refusal is not None:
            await interaction.response.send_message(f"❌ {refusal}", ephemeral=True)
            return

        # Guild.me is annotated as Member but is really get_member(self), which
        # is None whenever the self member was never cached.
        me = guild.me
        if me is None:
            await interaction.response.send_message(
                "❌ I can't see my own roles in this server yet. Give me a moment and run /setup again.",
                ephemeral=True,
            )
            return

        if me.top_role <= role:
            await interaction.response.send_message(
                f"❌ My highest role is below {role.mention}. Drag my role above it in "
                "Server Settings, Roles, then run /setup again.", ephemeral=True
            )
            return

        await GuildConfig.get_instance().set_role(str(guild.id), str(role.id))

        await interaction.response.send_message(
            f"✅ Set up! Members repping this server's tag now receive {role.mention}.\n\n"
            "**Would you like to sync your server tag rewards now?** "
            "This checks every current member once so existing tag-wearers get the role immediately.",
            view=SyncPromptView(str(guild.id), interaction.user.id),
            ephemeral=True,
        )
