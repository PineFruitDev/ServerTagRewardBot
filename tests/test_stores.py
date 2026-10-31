"""GuildConfig / StreakStore against a fake client (no network)."""

from src.services.guild_config import GuildConfig
from src.services.streaks import MARK_BATCH, StreakStore, humanize, rank, summarize


class FakeClient:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    async def _do(self, method, path, body=None):
        self.calls.append((method, path, body))
        return self.responses.get((method, path), {})

    async def get(self, path):
        return await self._do("GET", path)

    async def post(self, path, body=None):
        return await self._do("POST", path, body)

    async def put(self, path, body):
        return await self._do("PUT", path, body)


async def test_get_role_is_cached_and_writes_update_cache():
    client = FakeClient({("GET", "/guilds/1"): {"role_id": "10"}})
    cfg = GuildConfig(client)
    assert await cfg.get_role("1") == "10"
    assert await cfg.get_role("1") == "10"
    assert [c[:2] for c in client.calls] == [("GET", "/guilds/1")]

    await cfg.set_role("1", "11")
    assert await cfg.get_role("1") == "11"

    client.responses[("POST", "/guilds/1/disable")] = {"was_configured": True}
    assert await cfg.disable("1") is True
    assert await cfg.get_role("1") is None
    assert len([c for c in client.calls if c[0] == "GET"]) == 1


async def test_cache_expires():
    client = FakeClient({("GET", "/guilds/1"): {"role_id": None}})
    cfg = GuildConfig(client, cache_ttl=0)
    assert await cfg.get_role("1") is None
    assert await cfg.get_role("1") is None
    assert len(client.calls) == 2


async def test_all_guilds_and_count():
    client = FakeClient({("GET", "/guilds"): {"guilds": [["1", "10"], ["2", "20"]]}})
    cfg = GuildConfig(client)
    assert await cfg.all_guilds() == [("1", "10"), ("2", "20")]
    assert await cfg.count() == 2


async def test_mark_many_chunks_requests():
    client = FakeClient()
    store = StreakStore(client)
    marks = [(str(i), i % 2 == 0) for i in range(MARK_BATCH * 2 + 1)]
    await store.mark_many("5", marks, now=1000)
    assert [len(c[2]["marks"]) for c in client.calls] == [MARK_BATCH, MARK_BATCH, 1]
    assert all(c[1] == "/streaks/5/mark" and c[2]["now"] == 1000 for c in client.calls)
    assert client.calls[0][2]["marks"][0] == {"user_id": "0", "repping": True}


async def test_get_and_leaderboard_compute_like_before():
    rows = [
        {"user_id": "a", "active_since": 900, "total_seconds": 50, "longest_seconds": 40},
        {"user_id": "b", "active_since": None, "total_seconds": 500, "longest_seconds": 300},
        {"user_id": "c", "active_since": None, "total_seconds": 0, "longest_seconds": 0},
    ]
    client = FakeClient({
        ("GET", "/streaks/5/a"): {"row": rows[0]},
        ("GET", "/streaks/5/z"): {"row": None},
        ("GET", "/streaks/5"): {"rows": rows},
    })
    store = StreakStore(client)
    assert await store.get("5", "a", now=1000) == {"current": 100, "longest": 100, "total": 150, "active": True}
    assert await store.get("5", "z", now=1000) == {"current": 0, "longest": 0, "total": 0, "active": False}
    assert await store.leaderboard("5", "current", now=1000) == [("a", 100)]
    assert await store.leaderboard("5", "longest", now=1000) == [("b", 300), ("a", 100)]
    assert await store.leaderboard("5", "total", now=1000) == [("b", 500), ("a", 150)]


def test_helpers():
    assert summarize(None, 5)["active"] is False
    assert rank([], "current", 10, 0) == []
    assert humanize(30) == "less than a minute"
    assert humanize(90061) == "1d 1h"
    assert humanize(3720) == "1h 2m"
