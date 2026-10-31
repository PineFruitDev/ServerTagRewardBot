"""Streak tracking - how long members have been repping the server tag.

Every equip/unequip transition the bot observes updates this store, and full
syncs reconcile it, so streaks survive restarts. Data lives in tagbot-store
(Cloudflare D1); the Worker applies each transition atomically in SQL. Times
are unix seconds; a streak is "active" while active_since is set.
"""

import time

from src.services.logger import get_logger
from src.services.store_client import StoreClient

log = get_logger("StreakStore")

# Max marks per request; matches the Worker's MAX_MARKS
MARK_BATCH = 50


class StreakStore:
    """Remote store of per-member tag streaks."""

    _instance: "StreakStore | None" = None

    def __init__(self, client: StoreClient | None = None):
        self._client = client or StoreClient.get_instance()
        StreakStore._instance = self
        log.info("__init__ - Using remote store")

    @classmethod
    def get_instance(cls) -> "StreakStore":
        if cls._instance is None:
            cls._instance = StreakStore()
        return cls._instance

    async def mark(self, guild_id: str, user_id: str, repping: bool, now: int | None = None) -> None:
        """Reconcile a member's streak with their current repping state. Idempotent."""
        await self.mark_many(guild_id, [(user_id, repping)], now=now)

    async def mark_many(self, guild_id: str, marks: list[tuple[str, bool]], now: int | None = None) -> None:
        """Reconcile many members at once (used by full syncs). Idempotent."""
        now = now or int(time.time())
        guild_id = str(guild_id)
        for start in range(0, len(marks), MARK_BATCH):
            chunk = marks[start:start + MARK_BATCH]
            await self._client.post(f"/streaks/{guild_id}/mark", {
                "now": now,
                "marks": [{"user_id": str(uid), "repping": bool(rep)} for uid, rep in chunk],
            })

    async def get(self, guild_id: str, user_id: str, now: int | None = None) -> dict:
        """Return current/longest/total seconds for a member (zeros if untracked)."""
        now = now or int(time.time())
        data = await self._client.get(f"/streaks/{guild_id}/{user_id}")
        return summarize(data.get("row"), now)

    async def leaderboard(self, guild_id: str, sort: str = "current", limit: int = 10,
                          now: int | None = None) -> list[tuple[str, int]]:
        """Top members by 'current', 'longest', or 'total' streak seconds."""
        now = now or int(time.time())
        data = await self._client.get(f"/streaks/{guild_id}")
        return rank(data.get("rows", []), sort, limit, now)


def summarize(row: dict | None, now: int) -> dict:
    """Turn a stored streak row into current/longest/total seconds."""
    if not row:
        return {"current": 0, "longest": 0, "total": 0, "active": False}
    active_since = row.get("active_since")
    current = max(0, now - active_since) if active_since is not None else 0
    return {
        "current": current,
        "longest": max(row.get("longest_seconds", 0), current),
        "total": row.get("total_seconds", 0) + current,
        "active": active_since is not None,
    }


def rank(rows: list[dict], sort: str, limit: int, now: int) -> list[tuple[str, int]]:
    """Score rows by the requested sort and return the top `limit` (user_id, seconds)."""
    key = sort if sort in ("current", "longest") else "total"
    scored: list[tuple[str, int]] = []
    for row in rows:
        score = summarize(row, now)[key]
        if score > 0:
            scored.append((str(row["user_id"]), score))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:limit]


def humanize(seconds: int) -> str:
    """Turn a duration into '12d 5h' style text."""
    if seconds < 60:
        return "less than a minute"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if not days and minutes:
        parts.append(f"{minutes}m")
    return " ".join(parts) or "less than a minute"
