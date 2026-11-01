"""Fakes that hold the bot to its real contracts, for the integration tests.

The rest of the suite tests one unit at a time, replacing each collaborator with an
AsyncMock that records calls and returns canned values. That is the right tool for
branch coverage, but it cannot catch a request the real tagbot-store would reject,
and it never runs two services against each other.

So the fakes here are behavioural rather than recording:

* FakeStore implements the routes in worker/src/index.js, including its snowflake
  validation, its 50-mark cap and its two streak statements. A request the Worker
  would answer with HTTP 400 raises StoreError here too, so the bot-to-store
  contract is checked rather than assumed.
* FakeHTTP stands in for discord.py's HTTPClient and keeps the role state its calls
  imply, so a test can assert on a member's resulting roles instead of replaying a
  call sequence.
* TagBot and CommandBot wire the real GuildConfig, StreakStore and TagSyncService
  (and, for commands, the real Bot and command tree) between those two fakes.

Fixtures that build these live in conftest.py.
"""

import json
import re
from types import SimpleNamespace

import discord

from src.services import streaks as streaks_module
from src.services.environment import Config
from src.services.guild_config import GuildConfig
from src.services.store_client import StoreError
from src.services.streaks import StreakStore
from src.services.tag_sync import TagSyncService

GUILD = "900000000000000001"
OTHER_GUILD = "900000000000000002"
ROLE = "800000000000000001"
OTHER_ROLE = "800000000000000002"

# The Worker's own limits, mirrored so the fakes reject what it would reject
MAX_MARKS = 50
_SNOWFLAKE = re.compile(r"^\d{1,20}$")

#: Distinguishes "argument not given" from an explicit None
_UNSET = object()


def user_id(n: int) -> str:
    """A distinct, snowflake-shaped user id for member number n."""
    return str(700000000000000000 + n)


class Clock:
    """A stand-in for time.time() that only moves when a test moves it."""

    def __init__(self, start: int = 1_700_000_000):
        self.now = start

    def __call__(self) -> int:
        return self.now

    def advance(self, seconds: int) -> int:
        self.now += seconds
        return self.now


# --------------------------------------------------------------------------- #
# tagbot-store
# --------------------------------------------------------------------------- #


class FakeStore:
    """The tagbot-store Worker in memory: same routes, same validation, same streak math."""

    def __init__(self):
        # guild_id -> {"role_id": str, "enabled": bool}
        self.guilds: dict[str, dict] = {}
        # (guild_id, user_id) -> streak row
        self.streaks: dict[tuple[str, str], dict] = {}
        self.calls: list[tuple[str, str, dict | None]] = []
        #: When set, every request raises this instead of being served
        self.outage: Exception | None = None

    # -- helpers for tests ------------------------------------------------- #

    def seed_guild(self, guild_id: str = GUILD, role_id: str = ROLE) -> None:
        self.guilds[str(guild_id)] = {"role_id": str(role_id), "enabled": True}

    def row(self, guild_id: str, member: str) -> dict | None:
        return self.streaks.get((str(guild_id), str(member)))

    def paths(self, method: str | None = None) -> list[str]:
        return [path for verb, path, _ in self.calls if method is None or verb == method]

    @property
    def mark_requests(self) -> list[dict]:
        return [body for _, path, body in self.calls if path.endswith("/mark") and body]

    # -- the StoreClient interface ----------------------------------------- #

    async def get(self, path: str):
        return self._route("GET", path, None)

    async def post(self, path: str, body: dict | None = None):
        return self._route("POST", path, body or {})

    async def put(self, path: str, body: dict):
        return self._route("PUT", path, body)

    # -- routing ----------------------------------------------------------- #

    def _route(self, method: str, path: str, body: dict | None):
        self.calls.append((method, path, body))
        if self.outage is not None:
            raise self.outage
        parts = [part for part in path.split("/") if part]

        if parts == ["guilds"] and method == "GET":
            enabled = sorted((gid, row["role_id"]) for gid, row in self.guilds.items() if row["enabled"])
            return {"guilds": [[gid, role] for gid, role in enabled]}

        if parts[:1] == ["guilds"] and len(parts) >= 2:
            guild_id = self._snowflake(parts[1], "guild_id")
            if len(parts) == 2 and method == "GET":
                row = self.guilds.get(guild_id)
                return {"role_id": row["role_id"] if row and row["enabled"] else None}
            if len(parts) == 2 and method == "PUT":
                role_id = self._snowflake((body or {}).get("role_id"), "role_id")
                self.guilds[guild_id] = {"role_id": role_id, "enabled": True}
                return {"ok": True}
            if len(parts) == 3 and parts[2] == "disable" and method == "POST":
                row = self.guilds.get(guild_id)
                was_configured = bool(row and row["enabled"])
                if row:
                    row["enabled"] = False
                return {"was_configured": was_configured}

        if parts[:1] == ["streaks"] and len(parts) >= 2:
            guild_id = self._snowflake(parts[1], "guild_id")
            if len(parts) == 2 and method == "GET":
                return {"rows": [dict(row) for (gid, _), row in self.streaks.items() if gid == guild_id]}
            if len(parts) == 3 and parts[2] == "mark" and method == "POST":
                return self._mark(guild_id, body or {})
            if len(parts) == 3 and method == "GET":
                member = self._snowflake(parts[2], "user_id")
                row = self.streaks.get((guild_id, member))
                return {"row": dict(row) if row else None}

        raise StoreError(f"{method} {path} -> HTTP 404: not found")

    def _mark(self, guild_id: str, body: dict):
        now = body.get("now")
        marks = body.get("marks")
        if not isinstance(marks, list) or not 1 <= len(marks) <= MAX_MARKS:
            raise StoreError(f"POST /mark -> HTTP 400: marks must be a list of 1-{MAX_MARKS} items")
        if not isinstance(now, int) or isinstance(now, bool) or now <= 0:
            raise StoreError("POST /mark -> HTTP 400: now must be a positive integer")
        for mark in marks:
            member = self._snowflake(mark.get("user_id"), "user_id")
            repping = mark.get("repping")
            if not isinstance(repping, bool):
                raise StoreError("POST /mark -> HTTP 400: repping must be a boolean")
            self._apply_mark(guild_id, member, repping, now)
        return {"ok": True, "count": len(marks)}

    def _apply_mark(self, guild_id: str, member: str, repping: bool, now: int) -> None:
        """The Worker's two mark statements, in Python."""
        key = (guild_id, member)
        row = self.streaks.get(key)
        if repping:
            if row is None:
                self.streaks[key] = {"user_id": member, "active_since": now,
                                     "total_seconds": 0, "longest_seconds": 0}
            elif row["active_since"] is None:
                # COALESCE keeps a running streak running and restarts a stopped one
                row["active_since"] = now
            return
        if row is None or row["active_since"] is None:
            return
        elapsed = max(0, now - row["active_since"])
        row["total_seconds"] += elapsed
        row["longest_seconds"] = max(row["longest_seconds"], elapsed)
        row["active_since"] = None

    @staticmethod
    def _snowflake(value, name: str) -> str:
        if isinstance(value, int) and not isinstance(value, bool):
            value = str(value)
        if not isinstance(value, str) or not _SNOWFLAKE.match(value):
            raise StoreError(f"HTTP 400: invalid {name}")
        return value


# --------------------------------------------------------------------------- #
# Discord
# --------------------------------------------------------------------------- #


def primary_guild(tag_guild: str | None, enabled: bool = True) -> dict:
    """The primary_guild field Discord puts on a User object."""
    if tag_guild is None:
        return {"identity_guild_id": None, "identity_enabled": False, "tag": None, "badge": None}
    return {"identity_guild_id": str(tag_guild), "identity_enabled": enabled, "tag": "TAG", "badge": "b"}


def member_payload(member: str, tag_guild: str | None = None, *, enabled: bool = True,
                   roles: list[str] | None = None, omit_primary_guild: bool = False) -> dict:
    """A GUILD_MEMBER_* style member object wrapping a user object."""
    user: dict = {"id": str(member), "username": f"member{member[-4:]}"}
    if not omit_primary_guild:
        user["primary_guild"] = primary_guild(tag_guild, enabled)
    return {"user": user, "roles": list(roles) if roles is not None else []}


def http_error(status: int, message: str) -> discord.HTTPException:
    """A discord.HTTPException without a real aiohttp response behind it."""
    return discord.HTTPException(SimpleNamespace(status=status, reason=message), message)


def _row_id(member) -> int | None:
    """The numeric user id on a member list row, or None when it has none."""
    user = member.get("user") if isinstance(member, dict) else None
    try:
        return int(user["id"])
    except (TypeError, ValueError, KeyError):
        return None


class FakeHTTP:
    """Stand-in for discord.py's HTTPClient, tracking the role state its calls imply."""

    def __init__(self):
        #: guild_id -> member payloads get_members serves, in ascending id order
        self.members: dict[str, list[dict]] = {}
        #: user_id -> user object get_user returns
        self.users: dict[str, dict] = {}
        #: (operation, user_id) pairs that should raise; add_role/remove_role/get_user
        self.failures: set[tuple[str, str]] = set()
        self.calls: list[tuple] = []
        self._roles: dict[tuple[str, str], set[str]] = {}

    # -- helpers for tests ------------------------------------------------- #

    def seed_members(self, guild_id: str, payloads: list[dict]) -> None:
        self.members[str(guild_id)] = list(payloads)

    def roles_of(self, guild_id: str, member: str) -> set[str]:
        return set(self._roles.get((str(guild_id), str(member)), set()))

    def grant(self, guild_id: str, member: str, role_id: str) -> None:
        """Pre-existing role state, as if an earlier sync had already granted it."""
        self._roles.setdefault((str(guild_id), str(member)), set()).add(str(role_id))

    def ops(self) -> list[tuple]:
        return [call for call in self.calls if call[0] in ("add_role", "remove_role")]

    # -- the HTTPClient surface the bot uses ------------------------------- #

    async def add_role(self, guild_id: int, member_id: int, role_id: int) -> None:
        self._role_call("add_role", guild_id, member_id, role_id)
        self._roles.setdefault((str(guild_id), str(member_id)), set()).add(str(role_id))

    async def remove_role(self, guild_id: int, member_id: int, role_id: int) -> None:
        self._role_call("remove_role", guild_id, member_id, role_id)
        self._roles.setdefault((str(guild_id), str(member_id)), set()).discard(str(role_id))

    def _role_call(self, op: str, guild_id: int, member_id: int, role_id: int) -> None:
        # discord.py would build a bad route from anything but an int
        assert all(isinstance(value, int) and not isinstance(value, bool)
                   for value in (guild_id, member_id, role_id)), \
            f"{op} called with non-int ids: {(guild_id, member_id, role_id)!r}"
        self.calls.append((op, str(guild_id), str(member_id), str(role_id)))
        if (op, str(member_id)) in self.failures:
            raise http_error(403, "Missing Permissions")

    async def get_members(self, guild_id: int, *, limit: int, after: int) -> list[dict]:
        self.calls.append(("get_members", str(guild_id), limit, after))
        population = self.members.get(str(guild_id), [])
        # A row with no readable id cannot be placed against the cursor, so it is
        # always served; that is the case full_sync's own guard has to survive.
        later = [m for m in population if _row_id(m) is None or _row_id(m) > after]
        return later[:limit]

    async def get_user(self, member_id: int) -> dict:
        self.calls.append(("get_user", str(member_id)))
        if ("get_user", str(member_id)) in self.failures:
            raise http_error(404, "Unknown User")
        return self.users.get(str(member_id), {"id": str(member_id), "primary_guild": primary_guild(None)})

    async def close(self) -> None:
        """discord.Client.close() closes its HTTP layer; nothing to do here."""


class FakeClient:
    """Enough of discord.Client for TagSyncService: an HTTP layer and event registration."""

    def __init__(self):
        self.http = FakeHTTP()
        self.handlers: dict[str, object] = {}

    def event(self, coro):
        self.handlers[coro.__name__] = coro
        return coro


# --------------------------------------------------------------------------- #
# Harnesses
# --------------------------------------------------------------------------- #


class _Bot:
    """Shared reads for the two harnesses."""

    store: FakeStore
    http: FakeHTTP
    streaks: StreakStore
    service: TagSyncService | None

    def roles_of(self, member: str, guild_id: str = GUILD) -> set[str]:
        return self.http.roles_of(guild_id, member)

    def has_reward(self, member: str, guild_id: str = GUILD, role_id: str = ROLE) -> bool:
        return role_id in self.roles_of(member, guild_id)

    async def streak(self, member: str, guild_id: str = GUILD, now: int | None = None) -> dict:
        return await self.streaks.get(guild_id, member, now=now)

    def stats(self, guild_id: str = GUILD):
        return self.service.stats_for(guild_id)


class TagBot(_Bot):
    """Real GuildConfig, StreakStore and TagSyncService, between the two fakes."""

    def __init__(self, store: FakeStore, cache_ttl: float = 0):
        self.store = store
        self.client = FakeClient()
        self.http = self.client.http
        # Default to no config caching so the store stays the single source of
        # truth; pass the real TTL to exercise what the live bot caches.
        self.config = GuildConfig(store, cache_ttl=cache_ttl)
        self.streaks = StreakStore(store)
        self.service = TagSyncService(self.client)

    async def raw(self, frame) -> None:
        """Feed an arbitrary frame to the raw listener, valid or not."""
        await self.client.handlers["on_socket_raw_receive"](frame)

    async def dispatch(self, event: str, data) -> None:
        await self.raw(json.dumps({"t": event, "s": 1, "op": 0, "d": data}))

    async def equip(self, member: str, guild_id: str = GUILD, **kwargs) -> None:
        """The member equipped this guild's tag."""
        await self.dispatch("GUILD_MEMBER_UPDATE",
                            {"guild_id": guild_id, **member_payload(member, guild_id, **kwargs)})

    async def unequip(self, member: str, guild_id: str = GUILD, **kwargs) -> None:
        """The member removed their tag."""
        await self.dispatch("GUILD_MEMBER_UPDATE",
                            {"guild_id": guild_id, **member_payload(member, None, **kwargs)})

    async def join(self, member: str, tag_guild: str | None = GUILD, guild_id: str = GUILD,
                   **kwargs) -> None:
        """The member joined, carrying whatever tag they already had."""
        await self.dispatch("GUILD_MEMBER_ADD",
                            {"guild_id": guild_id, **member_payload(member, tag_guild, **kwargs)})


class CommandBot(TagBot):
    """As TagBot, but the client is the real Bot with its real command tree.

    Built through main.build_bot, so the commands under test are the ones actually
    registered at startup, reached through their real tree callbacks. Only the REST
    layer and the store are swapped out; no socket is ever opened.
    """

    def __init__(self, store: FakeStore, config: Config, *, members: bool = True,
                 cache_ttl: float = 0):
        import main  # deferred: importing main has module-level side effects

        self.store = store
        self.bot = main.build_bot(config, members=members)
        self.http = FakeHTTP()
        self.bot.http = self.http
        self.config = GuildConfig(store, cache_ttl=cache_ttl)
        self.streaks = StreakStore(store)
        self.service = TagSyncService.get_instance()

    async def raw(self, frame) -> None:
        await self.bot.on_socket_raw_receive(frame)

    async def run(self, name: str, *args, interaction=None, **kwargs):
        """Invoke /name through the registered tree callback and return the interaction."""
        command = self.bot.tree.get_command(name)
        assert command is not None, f"/{name} is not registered on the tree"
        interaction = interaction if interaction is not None else FakeInteraction()
        await command.callback(interaction, *args, **kwargs)
        return interaction

    async def close(self) -> None:
        await self.bot.close()


# --------------------------------------------------------------------------- #
# Interactions
# --------------------------------------------------------------------------- #


class FakeResponse:
    """discord.InteractionResponse: records what the command sent back."""

    def __init__(self):
        self.messages: list[dict] = []
        self.edits: list[dict] = []
        self.deferred = False

    def is_done(self) -> bool:
        return self.deferred or bool(self.messages)

    async def send_message(self, content: str | None = None, **kwargs) -> None:
        assert not self.is_done(), "command responded to the same interaction twice"
        self.messages.append({"content": content, **kwargs})

    async def edit_message(self, **kwargs) -> None:
        self.edits.append(kwargs)

    async def defer(self, **kwargs) -> None:
        assert not self.is_done(), "command deferred an interaction it had already answered"
        self.deferred = True


class FakeFollowup:
    def __init__(self):
        self.messages: list[dict] = []

    async def send(self, content: str | None = None, **kwargs) -> None:
        self.messages.append({"content": content, **kwargs})


class FakeRole:
    """A discord.Role stand-in; ordering follows position, the way the real one does."""

    def __init__(self, role_id: str = ROLE, position: int = 5, *,
                 managed: bool = False, default: bool = False, name: str = "Tag Crew",
                 permissions: discord.Permissions | None = None):
        self.id = int(role_id)
        self.name = name
        self.position = position
        self.managed = managed
        self._default = default
        # Permissions.none() is falsy, so this is compared against None
        self.permissions = discord.Permissions.none() if permissions is None else permissions

    def is_default(self) -> bool:
        return self._default

    @property
    def mention(self) -> str:
        return f"<@&{self.id}>"

    def __le__(self, other) -> bool:
        return self.position <= other.position

    def __lt__(self, other) -> bool:
        return self.position < other.position

    def __ge__(self, other) -> bool:
        return self.position >= other.position

    def __gt__(self, other) -> bool:
        return self.position > other.position


class FakeMember:
    def __init__(self, member: str, *, bot: bool = False, name: str = "Member",
                 permissions: discord.Permissions | None = None, top_role: FakeRole | None = None):
        self.id = int(member)
        self.bot = bot
        self.display_name = name
        # Permissions.none() is falsy, so these have to be compared against None
        self.guild_permissions = discord.Permissions(manage_roles=True) if permissions is None else permissions
        self.top_role = FakeRole("800000000000000009", position=50, name="Bot") if top_role is None else top_role
        self.display_avatar = SimpleNamespace(url="https://cdn.example/avatar.png")


class FakeGuild:
    def __init__(self, guild_id: str = GUILD, me=_UNSET):
        self.id = int(guild_id)
        # Guild.me is None whenever the self member was never cached, which is
        # the normal case in limited mode. Pass me=None to reproduce that.
        self.me = FakeMember(user_id(99), name="TagBot") if me is _UNSET else me


class FakeInteraction:
    """A guild interaction: enough of discord.Interaction for a command to run."""

    def __init__(self, guild=_UNSET, user: FakeMember | None = None,
                 app_permissions: discord.Permissions | None = None, dm: bool = False):
        self.guild = None if dm else (FakeGuild() if guild is _UNSET else guild)
        self.guild_id = None if self.guild is None else self.guild.id
        # The default invoker is an admin holding Manage Server, which is what
        # the state-changing commands gate on; guild.me separately holds the
        # Manage Roles the bot itself needs. Pass a user to vary either.
        self.user = user if user is not None else FakeMember(
            user_id(1), name="Admin", permissions=discord.Permissions(manage_guild=True))
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.command = SimpleNamespace(name="test")
        self.app_permissions = (discord.Permissions(manage_roles=True)
                                if app_permissions is None else app_permissions)

    # -- reads ------------------------------------------------------------- #

    @property
    def replied(self) -> str:
        """The content of the single message the command sent."""
        assert len(self.response.messages) == 1, f"expected one reply, got {self.response.messages}"
        return self.response.messages[0]["content"] or ""

    @property
    def embed(self) -> discord.Embed:
        assert len(self.response.messages) == 1, f"expected one reply, got {self.response.messages}"
        embed = self.response.messages[0].get("embed")
        assert isinstance(embed, discord.Embed), "command did not reply with an embed"
        return embed

    @property
    def view(self):
        return self.response.messages[0].get("view")

    @property
    def followed_up(self) -> str:
        assert len(self.followup.messages) == 1, f"expected one follow-up, got {self.followup.messages}"
        return self.followup.messages[0]["content"] or ""


def button(view: discord.ui.View, label: str) -> discord.ui.Button:
    """The button carrying this label, so tests click by label rather than by index."""
    for child in view.children:
        if getattr(child, "label", None) == label:
            return child
    raise AssertionError(f"no {label!r} button in {[getattr(c, 'label', c) for c in view.children]}")


def field_values(embed: discord.Embed) -> dict[str, str]:
    """An embed's fields as {name: value}, with the leading emoji stripped off each name."""
    return {field.name.split(" ", 1)[-1]: field.value for field in embed.fields}


__all__ = [
    "GUILD", "OTHER_GUILD", "ROLE", "OTHER_ROLE", "MAX_MARKS",
    "Clock", "CommandBot", "FakeClient", "FakeHTTP", "FakeStore", "TagBot",
    "FakeFollowup", "FakeGuild", "FakeInteraction", "FakeMember", "FakeResponse", "FakeRole",
    "button", "field_values", "http_error", "member_payload", "primary_guild",
    "streaks_module", "user_id",
]
