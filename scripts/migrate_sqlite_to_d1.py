"""One-time migration: copy a legacy data.db (SQLite) into the tagbot-db D1 database.

    python scripts/migrate_sqlite_to_d1.py path/to/data.db            # dry run: counts only
    python scripts/migrate_sqlite_to_d1.py path/to/data.db --apply    # import + verify

Requires wrangler logged in, run from anywhere (it uses ../worker). Set
CLOUDFLARE_ACCOUNT_ID if wrangler sees more than one account. Rows are written
with INSERT OR REPLACE, so re-running is safe. The generated SQL holds member
IDs, so it is written to a private temp file and deleted afterwards. After
import, D1 row counts are compared to the source and the script exits non-zero
on any mismatch.
"""

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

WORKER_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "worker")
DB_NAME = "tagbot-db"
TABLES = {
    "guild_config": ("guild_id", "role_id", "enabled", "updated_at"),
    "tag_streaks": ("guild_id", "user_id", "active_since", "total_seconds", "longest_seconds"),
}


def sql_literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, int):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def read_source(path: str) -> dict[str, list[tuple]]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {t: conn.execute(f"SELECT {', '.join(cols)} FROM {t}").fetchall() for t, cols in TABLES.items()}
    finally:
        conn.close()


def wrangler(*args: str) -> str:
    local = shutil.which("wrangler")
    npx = shutil.which("npx")
    if local:
        cmd = [local]
    elif npx:
        cmd = [npx, "--yes", "wrangler"]
    else:
        raise SystemExit("wrangler/npx not found; install Node.js")
    result = subprocess.run([*cmd, *args], cwd=WORKER_DIR,
                            capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        # stderr is wrangler's own output; it never contains our data rows
        raise SystemExit(f"wrangler {args[0:3]} failed:\n{result.stderr[-2000:]}")
    return result.stdout


# Same query on both sides: row counts plus value checksums, so a partial or
# mangled import can't pass on counts alone.
FINGERPRINT_SQL = (
    "SELECT 'guild_config' AS t, COUNT(*) AS n, COALESCE(SUM(enabled), 0) AS a, 0 AS b, 0 AS c "
    "FROM guild_config UNION ALL "
    "SELECT 'tag_streaks', COUNT(*), COALESCE(SUM(total_seconds), 0), COALESCE(SUM(longest_seconds), 0), "
    "COUNT(active_since) FROM tag_streaks"
)


def source_fingerprint(path: str) -> dict:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {r[0]: list(r[1:]) for r in conn.execute(FINGERPRINT_SQL).fetchall()}
    finally:
        conn.close()


def d1_fingerprint(target: list[str]) -> dict:
    out = wrangler("d1", "execute", DB_NAME, *target, "--json", "--command", FINGERPRINT_SQL)
    return {r["t"]: [r["n"], r["a"], r["b"], r["c"]] for r in json.loads(out)[0]["results"]}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("db_path")
    parser.add_argument("--apply", action="store_true", help="actually import (default: dry run)")
    parser.add_argument("--local", metavar="PERSIST_DIR",
                        help="rehearse against a local D1 (wrangler --local --persist-to DIR)")
    args = parser.parse_args()
    target = ["--local", "--persist-to", args.local] if args.local else ["--remote"]

    source = read_source(args.db_path)
    expected = source_fingerprint(args.db_path)
    print("source [rows, sum_a, sum_b, active]:", expected)
    if not args.apply:
        print("dry run; pass --apply to import")
        return

    before = d1_fingerprint(target)
    if any(v[0] for v in before.values()):
        print("note: D1 already has rows; INSERT OR REPLACE will upsert", before)

    fd, sql_path = tempfile.mkstemp(suffix=".sql", prefix="tagbot-migrate-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for table, rows in source.items():
                cols = ", ".join(TABLES[table])
                for row in rows:
                    values = ", ".join(sql_literal(v) for v in row)
                    fh.write(f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({values});\n")
        wrangler("d1", "execute", DB_NAME, *target, "--yes", "--file", sql_path)
    finally:
        os.remove(sql_path)

    actual = d1_fingerprint(target)
    print("D1     [rows, sum_a, sum_b, active]:", actual)
    if actual != expected:
        print("MISMATCH")
        sys.exit(1)
    print("counts and checksums match")


if __name__ == "__main__":
    main()
