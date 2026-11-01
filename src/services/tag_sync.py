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
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import discord

from src.services.cooldowns import Cooldowns
from src.services.discord_errors import blocks_whole_guild, describe
from src.services.environment import Environment
from src.services.guild_config import GuildConfig
from src.services.logger import get_logger
from src.services.store_client import StoreError
from src.services.streaks import StreakStore

log = get_logger("TagSyncService")

# Shown by commands when the bot runs without the Server Members intent
SYNC_PAUSED_MESSAGE = (
    "⏸️ Live tag sync is paused while Discord reviews this bot's Server Members intent. "
    "Your settings and streaks are safe; roles will catch up once sync resumes."
)

_SNOWFLAKE = re.compile(r"^\d{1,20}$")


def snowflake(value: object) -> str | None:
    """Return value as a Discord id string, or None when it is not one.

    Gateway payloads are parsed straight from JSON, so every id has to be shape
    checked before it reaches int() or the store.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if value >= 0 else None
    if isinstance(value, str) and _SNOWFLAKE.match(value):
        return value
    return None


@dataclass
class GuildStats:
    """Per-guild statistics from the most recent sync."""

    members_checked: int = 0
    roles_added: int = 0
    roles_removed: int = 0
    currently_repping: int = 0
    live_updates: int = 0
    #: Member updates that carried a tag state the bot had already applied, so
    #: they cost no role write and no store round trip (see cooldowns.py).
    duplicates_collapsed: int = 0
    last_sync_at: datetime | None = field(default=None)
    #: Set when a role write failed in a way that affects the whole guild (see
    #: discord_errors.blocks_whole_guild). A full sync stops on it instead of
    #: repeating a doomed call per member, and commands report it.
    role_blocked: str | None = field(default=None)


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
        """The running service, or None in limited mode (no members intent)."""
        return cls._instance

    @classmethod
    def detach(cls) -> None:
        """Turn tag sync off (limited mode)."""
        cls._instance = None

    def stats_for(self, guild_id: str) -> GuildStats:
        return self._stats.setdefault(str(guild_id), GuildStats())

    def _lock_for(self, guild_id: str) -> asyncio.Lock:
        return self._sync_locks.setdefault(str(guild_id), asyncio.Lock())

    def is_syncing(self, guild_id: str) -> bool:
        return self._lock_for(guild_id).locked()

    @staticmethod
    def is_repping(user: dict, guild_id: str) -> bool:
        """True when the raw user payload is repping THIS guild's tag."""
        primary_guild = user.get("primary_guild")
        if not isinstance(primary_guild, dict):
            return False
        return bool(primary_guild.get("identity_enabled")) and \
            primary_guild.get("identity_guild_id") == str(guild_id)

    async def on_socket_raw_receive(self, msg) -> None:
        """Raw dispatch hook - fires for every gateway event.

        Never lets one malformed event or transient store/API error escape.
        """
        try:
            await self._handle_raw(msg)
        except Exception:
            log.exception("on_socket_raw_receive - Failed to handle gateway event")

    async def _handle_raw(self, msg) -> None:
        if not isinstance(msg, str):
            return
        try:
            payload = json.loads(msg)
        except (ValueError, TypeError):
            return
        # Valid JSON is not necessarily a dispatch object: a bare array or
        # scalar frame has no .get().
        if not isinstance(payload, dict):
            return

        event = payload.get("t")
        if event not in ("GUILD_MEMBER_UPDATE", "GUILD_MEMBER_ADD"):
            return

        data = payload.get("d")
        if not isinstance(data, dict):
            return

        # Shape check the whole payload before doing any work, so a malformed
        # event costs neither a store round trip nor a logged traceback.
        guild_id = snowflake(data.get("guild_id"))
        user = data.get("user")
        if guild_id is None or not isinstance(user, dict):
            return
        user_id = snowflake(user.get("id"))
        if user_id is None:
            return

        # Only act in guilds an admin has configured
        role_id = await GuildConfig.get_instance().get_role(guild_id)
        if role_id is None:
            return

        repping = self.is_repping(user, guild_id)
        if event == "GUILD_MEMBER_UPDATE":
            self.stats_for(guild_id).live_updates += 1
        elif not repping:
            # A member joining without this guild's tag has nothing to grant,
            # and nothing to remove either: they arrive with no roles.
            return

        await self._apply_live(guild_id, role_id, user_id, repping, data.get("roles"))

    async def _apply_live(self, guild_id: str, role_id: str, user_id: str,
                          repping: bool, current_roles=None) -> None:
        """Act on one gateway event, unless the event has nothing left to say.

        GUILD_MEMBER_UPDATE fires for every member edit, so most of these
        events repeat a tag state the bot acted on seconds ago, and each one
        otherwise costs a store round trip to re-record a streak that has not
        moved. Collapsing them is what keeps a nickname spree cheap.

        Two things have to hold before an event is dropped: the decision has
        to repeat one already applied, and the roles the event itself carries
        have to agree with it. The second is what makes this safe. An admin who
        pulls the reward role by hand produces an event whose roles disagree,
        and that event is acted on rather than mistaken for noise.
        """
        settled = (role_id in current_roles) == repping if isinstance(current_roles, list) else True
        if settled and Cooldowns.get_instance().collapse_duplicate(guild_id, user_id, repping):
            self.stats_for(guild_id).duplicates_collapsed += 1
            return

        await self.apply_role(guild_id, role_id, user_id, repping, current_roles)
        await self._record_streaks(guild_id, [(user_id, repping)])

    @staticmethod
    async def _record_streaks(guild_id: str, marks: list[tuple[str, bool]]) -> None:
        """Update streaks; a store outage is logged but never blocks role updates."""
        if not marks:
            return
        try:
            await StreakStore.get_instance().mark_many(guild_id, marks)
        except StoreError as exc:
            log.warning("_record_streaks - [%s] Could not record %d streak update(s): %s",
                        guild_id, len(marks), exc)

    async def apply_role(self, guild_id: str, role_id: str, user_id: str,
                         should_have: bool, current_roles=None) -> bool:
        """Add or remove the reward role, skipping redundant API calls when possible.

        Returns True when the member's role state is correct afterwards, False
        when Discord refused. Never raises: this runs on every member update,
        so a failure is reported and contained rather than propagated.
        """
        # role_id comes from the store and the ids from a gateway payload. A
        # single unparseable value would otherwise raise ValueError out of here
        # on every member update, so reject it once and keep going.
        guild_ref, user_ref, role_ref = (snowflake(guild_id), snowflake(user_id), snowflake(role_id))
        if guild_ref is None or user_ref is None or role_ref is None:
            log.error("apply_role - [%s] Skipped: ids must be numeric (user=%r, role=%r)",
                      guild_id, user_id, role_id)
            return False

        has_role = None
        if isinstance(current_roles, list):
            has_role = role_id in current_roles

        stats = self.stats_for(guild_id)
        try:
            if should_have and has_role is not True:
                await self._client.http.add_role(int(guild_ref), int(user_ref), int(role_ref))
                stats.roles_added += 1
                log.info("apply_role - [%s] Added role to %s", guild_id, user_id)
            elif not should_have and has_role is not False:
                await self._client.http.remove_role(int(guild_ref), int(user_ref), int(role_ref))
                stats.roles_removed += 1
                log.info("apply_role - [%s] Removed role from %s", guild_id, user_id)
        except discord.HTTPException as exc:
            # describe() puts the status in the message text: passing the
            # exception as a log argument would render it as "HTTPException"
            # and lose the one detail that tells these cases apart.
            detail = describe(exc)
            if blocks_whole_guild(exc):
                stats.role_blocked = detail
                log.error("apply_role - [%s] Cannot manage role %s (%s). Check that I have Manage "
                          "Roles and that my highest role is above it.", guild_id, role_id, detail)
            else:
                log.warning("apply_role - [%s] Role update failed for %s (%s)",
                            guild_id, user_id, detail)
            return False

        # Record what is now in effect, so the duplicate member updates that
        # follow cost nothing. Only reached on success: a failed write must
        # leave the next event free to retry it.
        Cooldowns.get_instance().record_reward(guild_ref, user_ref, should_have)
        return True

    async def full_sync(self, guild_id: str) -> GuildStats:
        """Walk every member of one guild and reconcile the reward role.

        Member list payloads can omit primary_guild, so users are re-fetched
        individually (throttled) when the field is missing.
        """
        guild_id = str(guild_id)
        stats = self.stats_for(guild_id)

        role_id = await GuildConfig.get_instance().get_role(guild_id)
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
            marks: list[tuple[str, bool]] = []
            # A fresh attempt re-tests the role: an admin may have fixed the
            # permissions since the last run.
            stats.role_blocked = None

            while True:
                batch = await self._fetch_members(guild_id, after)
                if not batch:
                    break

                cursor = after
                for member in batch:
                    listed = member.get("user") if isinstance(member, dict) else None
                    member_id = snowflake(listed.get("id")) if isinstance(listed, dict) else None
                    if not isinstance(listed, dict) or member_id is None:
                        # One unusable row must not abandon the rest of the guild.
                        log.warning("full_sync - [%s] Skipped a member with no usable id", guild_id)
                        continue
                    cursor = max(cursor, int(member_id))
                    checked += 1

                    user = listed
                    if "primary_guild" not in user:
                        fetched = await self._refetch_user(guild_id, member_id)
                        # Throttle whether or not the fetch worked. When a rate
                        # limit or an outage fails every member, skipping the
                        # delay would spin this loop at full speed into an API
                        # that is already struggling.
                        await asyncio.sleep(config.sync_delay_ms / 1000)
                        if fetched is None:
                            continue
                        user = fetched

                    should_have = self.is_repping(user, guild_id)
                    if should_have:
                        repping += 1
                    marks.append((member_id, should_have))
                    await self.apply_role(guild_id, role_id, member_id, should_have, member.get("roles"))
                    if stats.role_blocked is not None:
                        break

                # Batched: one store request per 50 members instead of one each
                await self._record_streaks(guild_id, marks)
                marks = []

                if stats.role_blocked is not None:
                    # Every remaining member would fail the same way, so stop
                    # rather than spend thousands of calls proving it. The work
                    # done so far is kept and reported.
                    log.error("full_sync - [%s] Stopped after %d members: role is unmanageable (%s)",
                              guild_id, checked, stats.role_blocked)
                    break

                if len(batch) < 1000:
                    break
                if cursor == after:
                    # A full page that never advanced the cursor would make the
                    # very same request forever.
                    log.warning("full_sync - [%s] Member page did not advance, stopping", guild_id)
                    break
                after = cursor

            stats.members_checked = checked
            stats.currently_repping = repping
            stats.last_sync_at = datetime.now(timezone.utc)
            log.info("full_sync - [%s] Complete: %d members checked, %d repping", guild_id, checked, repping)

        return stats

    async def _fetch_members(self, guild_id: str, after: int) -> list:
        """One page of the guild member list.

        Re-raises: an unfinished sync must not record a last_sync_at, and the
        caller turns the failure into a specific reply. Logged here because the
        status code is the only thing separating a withdrawn members intent
        from a transient outage, and describe() is the only way to keep it.
        """
        try:
            return await self._client.http.get_members(int(guild_id), limit=1000, after=after)
        except discord.HTTPException as exc:
            log.error("full_sync - [%s] Member list request failed (%s)", guild_id, describe(exc))
            raise

    async def _refetch_user(self, guild_id: str, member_id: str) -> dict | None:
        """One member's user payload, or None when Discord would not give it up."""
        try:
            # Annotated User by the stubs, but the HTTP layer returns the raw
            # payload, so it is shape checked below rather than trusted.
            fetched: Any = await self._client.http.get_user(int(member_id))
        except discord.HTTPException as exc:
            log.warning("full_sync - [%s] Could not refetch member %s (%s)",
                        guild_id, member_id, describe(exc))
            return None
        return fetched if isinstance(fetched, dict) else None

    async def safe_full_sync(self, guild_id: str) -> None:
        """full_sync for background tasks: logs failures instead of losing them."""
        try:
            await self.full_sync(guild_id)
        except Exception:
            log.exception("safe_full_sync - [%s] Full sync failed", guild_id)

    async def sync_all_guilds(self) -> None:
        """Reconcile every configured guild, one at a time (startup)."""
        try:
            guilds = await GuildConfig.get_instance().all_guilds()
        except StoreError as exc:
            log.error("sync_all_guilds - Could not load configured guilds: %s", exc)
            return
        log.info("sync_all_guilds - Syncing %d configured guilds...", len(guilds))
        for guild_id, _role_id in guilds:
            try:
                await self.full_sync(guild_id)
            except Exception:
                log.exception("sync_all_guilds - [%s] Full sync failed", guild_id)
        log.info("sync_all_guilds - Done")
