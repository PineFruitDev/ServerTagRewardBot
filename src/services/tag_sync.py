"""TagSyncService keeps a role in sync with server tag adoption.

Discord exposes the equipped server tag on the User object as `primary_guild`
({ identity_guild_id, identity_enabled, tag, badge }). A member is repping this
server's tag when identity_guild_id matches the guild and identity_enabled is
true. The GUILD_MEMBER_UPDATE gateway event fires when the field changes, so
role updates land within seconds of a member equipping or removing the tag.
"""

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

import discord

from src.services.environment import Environment
from src.services.logger import get_logger

log = get_logger("TagSyncService")


@dataclass
class SyncStats:
    """Statistics from the most recent sync."""

    members_checked: int = 0
    roles_added: int = 0
    roles_removed: int = 0
    currently_repping: int = 0
    live_updates: int = 0
    last_sync_at: datetime | None = field(default=None)


class TagSyncService:
    """Watches raw gateway dispatches and reconciles the reward role."""

    _instance: "TagSyncService | None" = None

    def __init__(self, client: discord.Client):
        self._client = client
        self.stats = SyncStats()
        self._sync_lock = asyncio.Lock()
        client.event(self.on_socket_raw_receive)
        TagSyncService._instance = self
        log.info("__init__ - Listening for member updates and joins")

    @classmethod
    def get_instance(cls) -> "TagSyncService | None":
        return cls._instance

    @property
    def syncing(self) -> bool:
        return self._sync_lock.locked()

    @staticmethod
    def is_repping(user: dict) -> bool:
        """True when the raw user payload is repping THIS server's tag."""
        config = Environment.get_config()
        primary_guild = user.get("primary_guild") or {}
        return bool(primary_guild.get("identity_enabled")) and \
            primary_guild.get("identity_guild_id") == config.guild_id

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

        config = Environment.get_config()
        data = payload.get("d") or {}
        if data.get("guild_id") != config.guild_id or "user" not in data:
            return

        user = data["user"]
        if event == "GUILD_MEMBER_UPDATE":
            self.stats.live_updates += 1
            await self.apply_role(user["id"], self.is_repping(user), data.get("roles"))
        elif self.is_repping(user):
            await self.apply_role(user["id"], True, data.get("roles"))

    async def apply_role(self, user_id: str, should_have: bool, current_roles=None) -> None:
        """Add or remove the reward role, skipping redundant API calls when possible."""
        config = Environment.get_config()
        has_role = None
        if isinstance(current_roles, list):
            has_role = config.tag_role_id in current_roles

        try:
            if should_have and has_role is not True:
                await self._client.http.add_role(
                    int(config.guild_id), int(user_id), int(config.tag_role_id)
                )
                self.stats.roles_added += 1
                log.info("apply_role - Added role to %s", user_id)
            elif not should_have and has_role is not False:
                await self._client.http.remove_role(
                    int(config.guild_id), int(user_id), int(config.tag_role_id)
                )
                self.stats.roles_removed += 1
                log.info("apply_role - Removed role from %s", user_id)
        except discord.HTTPException as exc:
            log.error("apply_role - Failed for %s: %s", user_id, exc)

    async def full_sync(self) -> SyncStats:
        """Walk every member and reconcile the role against their tag state.

        Member list payloads can omit primary_guild, so users are re-fetched
        individually (throttled) when the field is missing.
        """
        if self.syncing:
            log.warning("full_sync - Sync already in progress, skipping")
            return self.stats

        async with self._sync_lock:
            config = Environment.get_config()
            log.info("full_sync - Starting full member sync...")
            checked = repping = 0
            after = 0

            while True:
                batch = await self._client.http.get_members(
                    int(config.guild_id), limit=1000, after=after
                )
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

                    should_have = self.is_repping(user)
                    if should_have:
                        repping += 1
                    await self.apply_role(user["id"], should_have, member.get("roles"))

                if len(batch) < 1000:
                    break

            self.stats.members_checked = checked
            self.stats.currently_repping = repping
            self.stats.last_sync_at = datetime.now(timezone.utc)
            log.info("full_sync - Complete: %d members checked, %d repping the tag", checked, repping)

        return self.stats
