"""TagSyncService keeps reward roles in sync with server tag adoption.

Public multi-guild edition: any server can invite the bot and configure a
reward role with /setup. Discord exposes the equipped server tag on the User
object as `primary_guild` ({ identity_guild_id, identity_enabled, tag, badge }).
A member is repping a server's tag when identity_guild_id matches that guild
and identity_enabled is true. The GUILD_MEMBER_UPDATE gateway event fires when
the field changes, so role updates land within seconds across every guild.
"""

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

import discord

from src.services.environment import Environment
from src.services.guild_config import GuildConfig
from src.services.logger import get_logger

log = get_logger("TagSyncService")


@dataclass
class GuildStats:
    """Per-guild statistics from the most recent sync."""

    members_checked: int = 0
    roles_added: int = 0
    roles_removed: int = 0
    currently_repping: int = 0
    live_updates: int = 0
    last_sync_at: datetime | None = field(default=None)


class TagSyncService:
    """Watches raw gateway dispatches and reconciles reward roles everywhere."""

    _instance: "TagSyncService | None" = None

    def __init__(self, client: discord.Client):
        self._client = client
        self._stats: dict[str, GuildStats] = {}
        self._sync_locks: dict[str, asyncio.Lock] = {}
        client.event(self.on_socket_raw_receive)
        TagSyncService._instance = self
        log.info("__init__ - Listening for member updates and joins (all guilds)")

    @classmethod
    def get_instance(cls) -> "TagSyncService | None":
        return cls._instance

    def stats_for(self, guild_id: str) -> GuildStats:
        return self._stats.setdefault(str(guild_id), GuildStats())

    def _lock_for(self, guild_id: str) -> asyncio.Lock:
        return self._sync_locks.setdefault(str(guild_id), asyncio.Lock())

    def is_syncing(self, guild_id: str) -> bool:
        return self._lock_for(guild_id).locked()

    @staticmethod
    def is_repping(user: dict, guild_id: str) -> bool:
        """True when the raw user payload is repping THIS guild's tag."""
        primary_guild = user.get("primary_guild") or {}
        return bool(primary_guild.get("identity_enabled")) and \
            primary_guild.get("identity_guild_id") == str(guild_id)

    async def on_socket_raw_receive(self, msg) -> None:
        """Raw dispatch hook - fires for every gateway event."""
        if not isinstance(msg, str):
            return
        try:
            payload = json.loads(msg)
        except (ValueError, TypeError):
            return

        event = payload.get("t")
        if event not in ("GUILD_MEMBER_UPDATE", "GUILD_MEMBER_ADD"):
            return

        data = payload.get("d") or {}
        guild_id = data.get("guild_id")
        if not guild_id or "user" not in data:
            return

        # Only act in guilds an admin has configured
        role_id = GuildConfig.get_instance().get_role(guild_id)
        if role_id is None:
            return

        user = data["user"]
        if event == "GUILD_MEMBER_UPDATE":
            self.stats_for(guild_id).live_updates += 1
            await self.apply_role(guild_id, role_id, user["id"],
                                  self.is_repping(user, guild_id), data.get("roles"))
        elif self.is_repping(user, guild_id):
            await self.apply_role(guild_id, role_id, user["id"], True, data.get("roles"))

    async def apply_role(self, guild_id: str, role_id: str, user_id: str,
                         should_have: bool, current_roles=None) -> None:
        """Add or remove the reward role, skipping redundant API calls when possible."""
        has_role = None
        if isinstance(current_roles, list):
            has_role = role_id in current_roles

        stats = self.stats_for(guild_id)
        try:
            if should_have and has_role is not True:
                await self._client.http.add_role(int(guild_id), int(user_id), int(role_id))
                stats.roles_added += 1
                log.info("apply_role - [%s] Added role to %s", guild_id, user_id)
            elif not should_have and has_role is not False:
                await self._client.http.remove_role(int(guild_id), int(user_id), int(role_id))
                stats.roles_removed += 1
                log.info("apply_role - [%s] Removed role from %s", guild_id, user_id)
        except discord.HTTPException as exc:
            log.error("apply_role - [%s] Failed for %s: %s", guild_id, user_id, exc)

    async def full_sync(self, guild_id: str) -> GuildStats:
        """Walk every member of one guild and reconcile the reward role.

        Member list payloads can omit primary_guild, so users are re-fetched
        individually (throttled) when the field is missing.
        """
        guild_id = str(guild_id)
        stats = self.stats_for(guild_id)

        role_id = GuildConfig.get_instance().get_role(guild_id)
        if role_id is None:
            return stats

        if self.is_syncing(guild_id):
            log.warning("full_sync - [%s] Sync already in progress, skipping", guild_id)
            return stats

        async with self._lock_for(guild_id):
            config = Environment.get_config()
            log.info("full_sync - [%s] Starting full member sync...", guild_id)
            checked = repping = 0
            after = 0

            while True:
                batch = await self._client.http.get_members(int(guild_id), limit=1000, after=after)
                if not batch:
                    break

                for member in batch:
                    user = member["user"]
                    after = int(user["id"])
                    checked += 1

                    if "primary_guild" not in user:
                        try:
                            user = await self._client.http.get_user(int(user["id"]))
                        except discord.HTTPException:
                            continue
                        await asyncio.sleep(config.sync_delay_ms / 1000)

                    should_have = self.is_repping(user, guild_id)
                    if should_have:
                        repping += 1
                    await self.apply_role(guild_id, role_id, user["id"], should_have, member.get("roles"))

                if len(batch) < 1000:
                    break

            stats.members_checked = checked
            stats.currently_repping = repping
            stats.last_sync_at = datetime.now(timezone.utc)
            log.info("full_sync - [%s] Complete: %d members checked, %d repping", guild_id, checked, repping)

        return stats

    async def sync_all_guilds(self) -> None:
        """Reconcile every configured guild, one at a time (startup)."""
        guilds = GuildConfig.get_instance().all_guilds()
        log.info("sync_all_guilds - Syncing %d configured guilds...", len(guilds))
        for guild_id, _role_id in guilds:
            await self.full_sync(guild_id)
        log.info("sync_all_guilds - Done")
