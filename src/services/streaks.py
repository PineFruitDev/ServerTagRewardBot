"""Streak tracking - how long members have been repping the server tag.

Every equip/unequip transition the bot observes updates this store, and full
syncs reconcile it, so streaks survive restarts. Times are stored as unix
seconds; a streak is "active" while active_since is set.
"""

import sqlite3
import threading
import time

from src.services.logger import get_logger

log = get_logger("StreakStore")

_DB_PATH = "data.db"


class StreakStore:
    """Thread-safe SQLite store of per-member tag streaks."""

    _instance: "StreakStore | None" = None

    def __init__(self, db_path: str = _DB_PATH):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tag_streaks (
                guild_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                active_since INTEGER,
                total_seconds INTEGER NOT NULL DEFAULT 0,
                longest_seconds INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )
            """
        )
        self._conn.commit()
        StreakStore._instance = self
        log.info("__init__ - Streak store ready (%s)", db_path)

    @classmethod
    def get_instance(cls) -> "StreakStore":
        if cls._instance is None:
            cls._instance = StreakStore()
        return cls._instance

    def mark(self, guild_id: str, user_id: str, repping: bool, now: int | None = None) -> None:
        """Reconcile a member's streak with their current repping state. Idempotent."""
        now = now or int(time.time())
        guild_id, user_id = str(guild_id), str(user_id)

        with self._lock:
            row = self._conn.execute(
                "SELECT active_since, total_seconds, longest_seconds FROM tag_streaks "
                "WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ).fetchone()

            if repping:
                if row is None:
                    self._conn.execute(
                        "INSERT INTO tag_streaks (guild_id, user_id, active_since) VALUES (?, ?, ?)",
                        (guild_id, user_id, now),
                    )
                elif row[0] is None:
                    self._conn.execute(
                        "UPDATE tag_streaks SET active_since = ? WHERE guild_id = ? AND user_id = ?",
                        (now, guild_id, user_id),
                    )
                # already active: nothing to do
            else:
                if row is not None and row[0] is not None:
                    duration = max(0, now - row[0])
                    total = row[1] + duration
                    longest = max(row[2], duration)
                    self._conn.execute(
                        "UPDATE tag_streaks SET active_since = NULL, total_seconds = ?, "
                        "longest_seconds = ? WHERE guild_id = ? AND user_id = ?",
                        (total, longest, guild_id, user_id),
                    )
                # not tracked or already inactive: nothing to do

            self._conn.commit()

    def get(self, guild_id: str, user_id: str, now: int | None = None) -> dict:
        """Return current/longest/total seconds for a member (zeros if untracked)."""
        now = now or int(time.time())
        with self._lock:
            row = self._conn.execute(
                "SELECT active_since, total_seconds, longest_seconds FROM tag_streaks "
                "WHERE guild_id = ? AND user_id = ?",
                (str(guild_id), str(user_id)),
            ).fetchone()

        if row is None:
            return {"current": 0, "longest": 0, "total": 0, "active": False}

        current = max(0, now - row[0]) if row[0] is not None else 0
        return {
            "current": current,
            "longest": max(row[2], current),
            "total": row[1] + current,
            "active": row[0] is not None,
        }

    def leaderboard(self, guild_id: str, sort: str = "current", limit: int = 10,
                    now: int | None = None) -> list[tuple[str, int]]:
        """Top members by 'current', 'longest', or 'total' streak seconds."""
        now = now or int(time.time())
        with self._lock:
            rows = self._conn.execute(
                "SELECT user_id, active_since, total_seconds, longest_seconds "
                "FROM tag_streaks WHERE guild_id = ?",
                (str(guild_id),),
            ).fetchall()

        scored: list[tuple[str, int]] = []
        for user_id, active_since, total, longest in rows:
            current = max(0, now - active_since) if active_since is not None else 0
            if sort == "current":
                score = current
            elif sort == "longest":
                score = max(longest, current)
            else:
                score = total + current
            if score > 0:
                scored.append((user_id, score))

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
