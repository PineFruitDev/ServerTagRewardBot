"""Per-member and per-server rate limits for commands and reward grants.

Two different problems live here, and they get deliberately different answers.

A slash command can be refused. Ten `/leaderboard` runs in a second are nine
runs of noise, and "you can do that again in a moment" costs the member
nothing while saving the store nine round trips. So commands get real
cooldowns, one per member per server and one for the server as a whole, plus a
friendly reply whichever of them trips.

A reward grant cannot be refused. Dropping one leaves a member wearing a role
they no longer earn, or missing one they do, until something else happens to
notice. What is safe to drop is a repeat of a decision already applied, and
that is most of what the bot sees: GUILD_MEMBER_UPDATE fires for every member
edit (a nickname, a timeout, someone else's roles), each one carrying the same
unchanged tag state and costing a store round trip to re-record a streak that
has not moved. So grants get duplicate collapsing and nothing else. A member
who equips and unequips the tag twice in five seconds still gets all four role
updates; a member who renames themselves fifty times costs one. Tag sync adds
one more condition before it drops an event (see TagSyncService._apply_live):
the roles the event carries have to agree with the decision too, so an event
that reveals a role gone astray is never mistaken for noise.

Both limits are in-memory and per-process, which is the right scope for each.
A member's interactions all land on one shard, so a cooldown holds wherever it
is enforced, and collapsing is only ever an optimisation, so losing it across a
restart costs one redundant role write rather than a wrong role.
"""

import math
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from src.services.environment import Environment

#: Checks between sweeps of expired keys. A sweep is a full scan, so it is
#: amortised rather than run per command; see RateLimiter.
_SWEEP_EVERY = 500

#: Which limit refused an attempt, and therefore which reply explains it
USER_SCOPE = "user"
GUILD_SCOPE = "guild"


def wait_text(seconds: float) -> str:
    """A wait as text: "4 seconds", "1 minute 5 seconds".

    Always rounded up and never zero, because "try again in 0 seconds" reads
    like a bug to the member who was just told to wait.
    """
    total = max(1, math.ceil(seconds))
    minutes, remainder = divmod(total, 60)
    parts = []
    if minutes:
        parts.append(f"{minutes} minute" + ("" if minutes == 1 else "s"))
    if remainder:
        parts.append(f"{remainder} second" + ("" if remainder == 1 else "s"))
    return " ".join(parts)


@dataclass(frozen=True)
class Wait:
    """A refused attempt: how long is left, and which limit refused it."""

    seconds: float
    scope: str

    def message(self, command: str) -> str:
        """What to tell the member.

        Neither wording treats them as a problem: one of these fires on a
        double-click, and the other fires because the server is busy, which is
        not the fault of whoever happened to run the command next.
        """
        if self.scope == USER_SCOPE:
            return (f"⏳ One moment! You can use `/{command}` again in "
                    f"{wait_text(self.seconds)}.")
        return (f"⏳ `/{command}` is being used a lot in this server right now. "
                f"Try again in {wait_text(self.seconds)}.")


@dataclass
class _Bucket:
    """Recent uses of one key, and when the last of them stops counting."""

    hits: deque[float] = field(default_factory=deque)
    expires_at: float = 0.0


class RateLimiter:
    """Sliding-window counters keyed by an arbitrary string.

    One mechanism covers every limit in this module: a cooldown is a window
    that allows a single use, and duplicate collapsing is a cooldown on a key
    that names the decision.

    Keys are created on demand, which for a public bot means forever, so
    expired keys are swept every `sweep_every` checks. That keeps the table
    proportional to recent traffic rather than to lifetime traffic, without
    scanning it on every command.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic,
                 sweep_every: int = _SWEEP_EVERY):
        self._buckets: dict[str, _Bucket] = {}
        self._clock = clock
        self._sweep_every = sweep_every
        self._since_sweep = 0

    def __len__(self) -> int:
        """How many keys are currently held (the tests read this to prove the sweep)."""
        return len(self._buckets)

    def check(self, key: str, uses: int, per_seconds: float, *, record: bool = True) -> float:
        """Seconds the caller still has to wait, or 0.0 when the use is allowed.

        An allowed use is recorded unless `record` is False, which lets a
        caller weigh several limits before committing to any of them. A refused
        use is never recorded, so hammering a command cannot push its own
        cooldown further and further out.
        """
        if uses <= 0 or per_seconds <= 0:
            # Not configured. Zero means off, in both directions, so that one
            # operator who wants no cooldowns does not need a second switch.
            return 0.0

        now = self._clock()
        self._maybe_sweep(now)

        bucket = self._buckets.get(key)
        if bucket is not None:
            cutoff = now - per_seconds
            while bucket.hits and bucket.hits[0] <= cutoff:
                bucket.hits.popleft()
            if len(bucket.hits) >= uses:
                # The window frees a slot when its oldest use ages out of it.
                return max(0.0, bucket.hits[0] + per_seconds - now)

        if not record:
            return 0.0
        if bucket is None:
            bucket = self._buckets.setdefault(key, _Bucket())
        bucket.hits.append(now)
        bucket.expires_at = now + per_seconds
        return 0.0

    def forget(self, key: str) -> None:
        """Drop a key's history, so its next use starts from nothing."""
        self._buckets.pop(key, None)

    def _maybe_sweep(self, now: float) -> None:
        self._since_sweep += 1
        if self._since_sweep < self._sweep_every:
            return
        self._since_sweep = 0
        for key in [key for key, bucket in self._buckets.items() if bucket.expires_at <= now]:
            del self._buckets[key]


@dataclass(frozen=True)
class Policy:
    """The configured limits. Any value of zero turns that limit off.

    The defaults match OPTIONAL_VARS in environment.py, which is what an
    unconfigured deployment actually gets; these are the fallback for a Policy
    built by hand (the tests) rather than from the environment.
    """

    user_seconds: float = 5.0
    guild_uses: int = 20
    guild_seconds: float = 60.0
    reward_seconds: float = 30.0

    @classmethod
    def from_config(cls) -> "Policy":
        config = Environment.get_config()
        return cls(
            user_seconds=config.command_cooldown_seconds,
            guild_uses=config.guild_command_limit,
            guild_seconds=config.guild_command_window_seconds,
            reward_seconds=config.reward_cooldown_seconds,
        )


class Cooldowns:
    """The bot's rate limits: one instance, shared by commands and tag sync."""

    _instance: "Cooldowns | None" = None

    def __init__(self, policy: Policy | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sweep_every: int = _SWEEP_EVERY):
        self.policy = Policy.from_config() if policy is None else policy
        self._limiter = RateLimiter(clock, sweep_every)
        Cooldowns._instance = self

    @classmethod
    def get_instance(cls) -> "Cooldowns":
        if cls._instance is None:
            cls._instance = Cooldowns()
        return cls._instance

    @property
    def tracked(self) -> int:
        """How many keys are being held across both limits."""
        return len(self._limiter)

    # -- commands ---------------------------------------------------------- #

    def check_command(self, command: str, user_id: str, guild_id: str | None, *,
                      user_seconds: float | None = None,
                      guild_rate: tuple[int, float] | None = None) -> Wait | None:
        """The wait a command run has to serve, or None when it may proceed.

        Both limits are weighed before either is recorded, so a run refused by
        one of them does not quietly spend the member's cooldown or the
        server's budget on the other.
        """
        seconds = self.policy.user_seconds if user_seconds is None else user_seconds
        uses, window = (guild_rate if guild_rate is not None
                        else (self.policy.guild_uses, self.policy.guild_seconds))

        # Per member *per server*: a busy server must not cool down a member
        # who has run nothing in it, and one member's spree must not spend
        # another server's budget.
        mine = f"c|{command}|{guild_id or 'dm'}|{user_id}"
        ours = f"c|{command}|{guild_id}"

        waiting = self._limiter.check(mine, 1, seconds, record=False)
        if waiting > 0:
            return Wait(waiting, USER_SCOPE)
        # A DM has no server to limit, and /help and /ping are the only
        # commands that reach one.
        if guild_id is not None:
            waiting = self._limiter.check(ours, uses, window, record=False)
            if waiting > 0:
                return Wait(waiting, GUILD_SCOPE)

        self._limiter.check(mine, 1, seconds)
        if guild_id is not None:
            self._limiter.check(ours, uses, window)
        return None

    # -- reward grants ----------------------------------------------------- #

    def collapse_duplicate(self, guild_id: str, user_id: str, repping: bool) -> bool:
        """True when this reward decision only repeats one already applied.

        A decision is keyed by what it decided, and record_reward() forgets the
        opposite key, so only a repeat is ever collapsed: the first event that
        flips a member's tag state always gets through, however recently the
        previous one was handled.
        """
        key = self._reward_key(guild_id, user_id, repping)
        return self._limiter.check(key, 1, self.policy.reward_seconds, record=False) > 0

    def record_reward(self, guild_id: str, user_id: str, repping: bool) -> None:
        """Remember a decision the bot applied, so its duplicates can be collapsed.

        Only ever called after a successful role write. A refused or failed
        write leaves nothing recorded, so the next event that carries the same
        state retries it instead of mistaking the failure for settled state.
        """
        self._limiter.forget(self._reward_key(guild_id, user_id, not repping))
        self._limiter.check(self._reward_key(guild_id, user_id, repping), 1,
                            self.policy.reward_seconds)

    @staticmethod
    def _reward_key(guild_id: str, user_id: str, repping: bool) -> str:
        return f"r|{guild_id}|{user_id}|{int(repping)}"
