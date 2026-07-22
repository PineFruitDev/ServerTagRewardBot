"""Per-guild configuration store.

The bot is public and serves any number of servers, so each guild's reward
role lives in a small SQLite database next to the bot (data.db). Admins set
it with /setup; everything else reads from here.
"""

import sqlite3
import threading

from src.services.logger import get_logger

log = get_logger("GuildConfig")

_DB_PATH = "data.db"


class GuildConfig:
    """Thread-safe SQLite store mapping guild_id -> reward role."""

    _instance: "GuildConfig | None" = None

    def __init__(self, db_path: str = _DB_PATH):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS guild_config (
                guild_id TEXT PRIMARY KEY,
                role_id TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        self._conn.commit()
        GuildConfig._instance = self
        log.info("__init__ - Store ready (%s), %d guilds configured", db_path, self.count())

    @classmethod
    def get_instance(cls) -> "GuildConfig":
        if cls._instance is None:
            cls._instance = GuildConfig()
        return cls._instance

    def set_role(self, guild_id: str, role_id: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO guild_config (guild_id, role_id, enabled)
                VALUES (?, ?, 1)
                ON CONFLICT(guild_id) DO UPDATE SET
                    role_id = excluded.role_id,
                    enabled = 1,
                    updated_at = datetime('now')
                """,
                (str(guild_id), str(role_id)),
            )
            self._conn.commit()
        log.info("set_role - Guild %s now rewards role %s", guild_id, role_id)

    def get_role(self, guild_id: str) -> str | None:
        """Return the reward role for a guild, or None if unconfigured/disabled."""
        with self._lock:
            row = self._conn.execute(
                "SELECT role_id FROM guild_config WHERE guild_id = ? AND enabled = 1",
                (str(guild_id),),
            ).fetchone()
        return row[0] if row else None

    def disable(self, guild_id: str) -> bool:
        """Turn the bot off for a guild. Returns True if it was configured."""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE guild_config SET enabled = 0, updated_at = datetime('now') WHERE guild_id = ?",
                (str(guild_id),),
            )
            self._conn.commit()
        log.info("disable - Guild %s disabled", guild_id)
        return cursor.rowcount > 0

    def all_guilds(self) -> list[tuple[str, str]]:
        """All (guild_id, role_id) pairs with the bot enabled."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT guild_id, role_id FROM guild_config WHERE enabled = 1"
            ).fetchall()
        return [(row[0], row[1]) for row in rows]

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM guild_config WHERE enabled = 1"
            ).fetchone()
        return row[0]
