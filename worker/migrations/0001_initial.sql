-- Same shape as the old local data.db so the one-time migration is a straight copy.
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id   TEXT PRIMARY KEY,
    role_id    TEXT NOT NULL,
    enabled    INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tag_streaks (
    guild_id        TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    active_since    INTEGER,
    total_seconds   INTEGER NOT NULL DEFAULT 0,
    longest_seconds INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);
