"""Manual full sync command - reconciles the reward role for every member of this server."""

import discord
from discord import app_commands

from src.core.command import Command, CommandHelpInfo
from src.core.options import require_guild_scope
from src.services.guild_config import GuildConfig
from src.services.tag_sync import SYNC_PAUSED_MESSAGE, TagSyncService

#: Said in both places this command can discover that another sync owns the
#: guild, because it means the same thing in both: this run reconciled nothing.
#: The check before deferring saves a round trip when a sync is already visible;
#: the one after it catches a sync that started during that round trip.
SYNC_IN_PROGRESS = "⏳ A sync is already in progress."


class SyncCommand(Command):
    help_info = CommandHelpInfo(
        name="sync",
        description="Walk every member of this server and reconcile the reward role against their equipped tag",
        usage="/sync",
        examples=["/sync"],
        category="Admin",
    )

    required_permissions = ("manage_roles",)
    required_user_permissions = ("manage_guild", "administrator")
    guild_only = True

    # The most expensive thing the bot can be asked to do: one API call per
    # member, plus a re-fetch for every member whose payload omits the tag.
    # full_sync's lock already refuses an overlapping run; these stop a queue
    # of runs forming behind the one in flight.
    user_cooldown = 60.0
    guild_rate = (3, 300.0)

    def register(self, tree: app_commands.CommandTree) -> None:
        @tree.command(name="sync", description="Run a full member sync of the tag reward role")
        @app_commands.default_permissions(manage_guild=True)
        async def sync(interaction: discord.Interaction) -> None:
            async with self.observed(interaction):
                if not await self.guard(interaction):
                    return
                await self.execute(interaction)

    async def execute(self, interaction: discord.Interaction) -> None:
        guild_id = await require_guild_scope(interaction)
        if guild_id is None:
            return

        if await GuildConfig.get_instance().get_role(guild_id) is None:
            await interaction.response.send_message(
                "ℹ️ This server isn't set up yet. Run /setup role:@Role first.", ephemeral=True
            )
            return

        service = TagSyncService.get_instance()
        if service is None:
            await interaction.response.send_message(SYNC_PAUSED_MESSAGE, ephemeral=True)
            return

        if service.is_syncing(guild_id):
            await interaction.response.send_message(SYNC_IN_PROGRESS, ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        stats = await service.full_sync(guild_id)

        if stats is None:
            # The service refused the run: deferring is a round trip to
            # Discord, and a sync can start inside it (a second admin, the
            # "Sync now" button on /setup, the startup sync). The figures it
            # would have returned belong to that run, so reporting them here
            # would announce somebody else's sync as this one's result.
            await interaction.followup.send(SYNC_IN_PROGRESS)
            return

        if stats.role_blocked is not None:
            await interaction.followup.send(
                f"⚠️ **Sync stopped early:** I can't manage the reward role here "
                f"({stats.role_blocked}).\n"
                f"Give me **Manage Roles**, and make sure my own highest role sits above the "
                f"reward role in Server Settings > Roles, then run /sync again.\n"
                f"👥 **Members checked before stopping:** {stats.members_checked}"
            )
            return

        if stats.stalled_after is not None:
            # Distinct from role_blocked: nothing here is for the admin to fix,
            # so it must not tell them to go changing permissions.
            await interaction.followup.send(
                f"⚠️ **Sync stopped early:** {stats.stalled_after} role updates failed in a row, "
                f"after retries, so I stopped instead of repeating that for every member. "
                f"That usually means Discord is rate limiting me or having trouble.\n"
                f"Nothing was lost; please run /sync again shortly.\n"
                f"👥 **Members checked before stopping:** {stats.members_checked}\n"
                f"➕ **Roles added:** {stats.last_sync_roles_added}\n"
                f"➖ **Roles removed:** {stats.last_sync_roles_removed}"
            )
            return

        # Every figure here describes the run that just finished. stats also
        # carries lifetime role totals (every sync plus every live gateway
        # update since startup), and those belong to /status: quoting them back
        # here told an admin running /sync a second time that it had granted
        # every role the first run did.
        await interaction.followup.send(
            f"✅ **Sync complete**\n"
            f"👥 **Members checked:** {stats.members_checked}\n"
            f"🏷️ **Repping the tag:** {stats.currently_repping}\n"
            f"➕ **Roles added:** {stats.last_sync_roles_added}\n"
            f"➖ **Roles removed:** {stats.last_sync_roles_removed}"
        )
