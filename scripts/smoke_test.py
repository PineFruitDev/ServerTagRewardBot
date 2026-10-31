"""End-to-end smoke test: real GuildConfig/StreakStore against a running tagbot-store.

Local (full read/write test against `wrangler dev`):
    cd worker && npx wrangler d1 migrations apply tagbot-db --local
    echo TAGBOT_STORE_KEY=<any 32+ char string> > .dev.vars && npx wrangler dev --port 8787
    TAGBOT_STORE_URL=http://127.0.0.1:8787 TAGBOT_STORE_KEY=<same> python scripts/smoke_test.py --write

Production (read-only: auth, health, guild listing):
    TAGBOT_STORE_URL=https://tagbot-store.<sub>.workers.dev TAGBOT_STORE_KEY=... python scripts/smoke_test.py

The key is read from the environment and never printed.
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services.guild_config import GuildConfig  # noqa: E402
from src.services.store_client import StoreClient, StoreError  # noqa: E402
from src.services.streaks import StreakStore  # noqa: E402

# Fake snowflakes that can't collide with real Discord IDs (too small)
G, R1, R2, U1, U2 = "1001", "2001", "2002", "3001", "3002"


def check(cond, label):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        raise SystemExit(1)


async def main(write: bool) -> None:
    client = StoreClient()
    cfg = GuildConfig(client, cache_ttl=0)
    streaks = StreakStore(client)
    try:
        check((await client.get("/health")) == {"ok": True}, "health with bearer key")

        bad = StoreClient(os.environ["TAGBOT_STORE_URL"], "x" * 40, attempts=1)
        try:
            await bad.get("/health")
            check(False, "wrong key rejected")
        except StoreError as exc:
            check("401" in str(exc), "wrong key rejected (401)")
        finally:
            await bad.close()

        guilds = await cfg.all_guilds()
        check(isinstance(guilds, list), f"list guilds ({len(guilds)} configured)")

        if not write:
            return

        await cfg.set_role(G, R1)
        check(await cfg.get_role(G) == R1, "set_role/get_role")
        await cfg.set_role(G, R2)
        check(await cfg.get_role(G) == R2, "set_role upserts")
        check((G, R2) in await cfg.all_guilds(), "all_guilds includes guild")
        check(await cfg.disable(G) is True, "disable returns was_configured")
        check(await cfg.get_role(G) is None, "disabled guild has no role")
        check(await cfg.disable("1009") is False, "disable unknown guild -> False")
        await cfg.set_role(G, R1)

        await streaks.mark(G, U1, True, now=1000)
        await streaks.mark(G, U1, True, now=1500)  # idempotent: keeps original start
        s = await streaks.get(G, U1, now=1100)
        check(s == {"current": 100, "longest": 100, "total": 100, "active": True}, "streak started once")
        await streaks.mark(G, U1, False, now=1300)
        await streaks.mark(G, U1, False, now=9999)  # idempotent: already closed
        s = await streaks.get(G, U1, now=5000)
        check(s == {"current": 0, "longest": 300, "total": 300, "active": False}, "streak closed atomically")
        await streaks.mark_many(G, [(U1, True), (U2, True)], now=2000)
        await streaks.mark(G, U2, False, now=2050)
        board = await streaks.leaderboard(G, "total", now=2100)
        check(board == [(U1, 400), (U2, 50)], "leaderboard total")
        check(await streaks.leaderboard(G, "current", now=2100) == [(U1, 100)], "leaderboard current")
        check(await streaks.get(G, "3999") == {"current": 0, "longest": 0, "total": 0, "active": False},
              "untracked member is zeros")

        try:
            await client.get("/guilds/not-a-snowflake")
            check(False, "bad id rejected")
        except StoreError as exc:
            check("400" in str(exc), "bad id rejected (400)")
        print("ALL PASS")
    finally:
        await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="exercise writes (local/dev store only)")
    asyncio.run(main(parser.parse_args().write))
