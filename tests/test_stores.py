"""GuildConfig / StreakStore against a fake client (no network)."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from src.services.guild_config import GuildConfig
from src.services.store_client import StoreClient, StoreError, StoreUnavailable
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


@pytest.mark.parametrize("operation", ["set_role", "disable"])
async def test_failed_write_preserves_last_known_configuration(operation):
    client = FakeClient({("GET", "/guilds/1"): {"role_id": "10"}})
    cfg = GuildConfig(client)
    assert await cfg.get_role("1") == "10"
    client.put = client.post = AsyncMock(side_effect=StoreUnavailable("offline"))
    with pytest.raises(StoreUnavailable):
        if operation == "set_role":
            await cfg.set_role("1", "11")
        else:
            await cfg.disable("1")
    assert await cfg.get_role("1") == "10"


@pytest.mark.parametrize("operation,expected", [("set_role", "11"), ("disable", None)])
@pytest.mark.parametrize("read_method", ["get_role", "all_guilds"])
async def test_delayed_read_cannot_overwrite_newer_configuration(operation, expected, read_method):
    entered, release = asyncio.Event(), asyncio.Event()
    client = FakeClient()

    async def delayed_get(path):
        entered.set()
        await release.wait()
        return {"role_id": "10"} if path == "/guilds/1" else {"guilds": [["1", "10"]]}

    client.get = delayed_get
    cfg = GuildConfig(client)
    pending = asyncio.create_task(cfg.get_role("1") if read_method == "get_role" else cfg.all_guilds())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if operation == "set_role":
            await cfg.set_role("1", "11")
        else:
            await cfg.disable("1")
    finally:
        release.set()
        result = await pending
    if read_method == "get_role":
        assert result == expected
    assert await cfg.get_role("1") == expected


async def test_missing_configuration_is_cached_per_guild():
    client = FakeClient({("GET", "/guilds/2"): {"role_id": "20"}})
    cfg = GuildConfig(client)
    assert await cfg.get_role("1") is None
    assert await cfg.get_role(1) is None
    assert await cfg.get_role("2") == "20"
    assert len(client.calls) == 2


@pytest.mark.parametrize("count", [0, 1, 50, 51, 100, 101])
async def test_mark_batches_preserve_every_member_and_timestamp(count):
    client = FakeClient()
    store = StreakStore(client)
    marks = [(str(i), bool(i % 2)) for i in range(count)]
    await store.mark_many("1", marks, now=1000)
    flattened = [mark for _, _, body in client.calls for mark in body["marks"]]
    assert flattened == [{"user_id": uid, "repping": rep} for uid, rep in marks]
    assert all(1 <= len(body["marks"]) <= 50 and body["now"] == 1000 for _, _, body in client.calls)
    assert len(client.calls) == (count + 49) // 50


async def test_mark_delegates_single_member_and_stops_on_failed_batch():
    client = FakeClient()
    store = StreakStore(client)
    await store.mark("1", "42", True, now=1000)
    assert client.calls == [("POST", "/streaks/1/mark", {
        "now": 1000, "marks": [{"user_id": "42", "repping": True}],
    })]
    client.post = AsyncMock(side_effect=StoreUnavailable("offline"))
    with pytest.raises(StoreUnavailable):
        await store.mark_many("1", [(str(i), True) for i in range(101)], now=1000)
    assert client.post.await_count == 1


async def test_mark_forwards_a_member_who_stopped_repping():
    """mark()'s only job is to forward its arguments, including repping=False.

    Every existing case passes True, so a mark() that hardcoded True would
    still pass the suite. Production reaches the store through mark_many, so
    nothing is broken today; this pins the single-member entry point before a
    live equip/unequip handler starts using it, because the failure mode there
    is a streak that can be started and never closed.
    """
    client = FakeClient()
    store = StreakStore(client)
    await store.mark("1", "42", False, now=1000)
    assert client.calls == [("POST", "/streaks/1/mark", {
        "now": 1000, "marks": [{"user_id": "42", "repping": False}],
    })]


# --------------------------------------------------------------------------- #
# Types on the way to the store. The Worker screens ids with isSafeInteger and
# requires a strict boolean, so an int or a truthy non-bool that reaches it is
# a 400, and a real 19-digit snowflake sent as a JSON number loses precision
# before it gets there (that is why the Worker screens them at all). These
# conversions are the bot's half of that contract, and every existing caller
# already passes clean strings, so dropping them changed nothing the suite
# could see.
# --------------------------------------------------------------------------- #


async def test_marks_are_normalised_before_they_reach_the_store():
    client = FakeClient()
    store = StreakStore(client)
    # An int guild id, int snowflakes, and truthy/falsy non-bools.
    await store.mark_many(1, [(987654321098745632, 1), (42, 0)], now=1000)

    assert client.calls == [("POST", "/streaks/1/mark", {
        "now": 1000,
        "marks": [
            {"user_id": "987654321098745632", "repping": True},
            {"user_id": "42", "repping": False},
        ],
    })]
    marks = client.calls[0][2]["marks"]
    assert all(isinstance(m["user_id"], str) for m in marks)
    assert all(m["repping"] is True or m["repping"] is False for m in marks)


async def test_set_role_normalises_both_ids():
    client = FakeClient()
    cfg = GuildConfig(client)
    await cfg.set_role(900000000000000001, 800000000000000001)

    assert client.calls == [
        ("PUT", "/guilds/900000000000000001", {"role_id": "800000000000000001"}),
    ]
    assert isinstance(client.calls[0][2]["role_id"], str)
    # The write-through cache holds the converted value, not the int.
    assert await cfg.get_role(900000000000000001) == "800000000000000001"


async def test_listed_guilds_are_normalised_to_strings():
    client = FakeClient({("GET", "/guilds"): {"guilds": [[1, 10], [2, 20]]}})
    cfg = GuildConfig(client)
    assert await cfg.all_guilds() == [("1", "10"), ("2", "20")]


async def test_disable_reports_a_real_boolean():
    """Callers branch on this, and the store is free to answer 1 or 0."""
    client = FakeClient({("POST", "/guilds/1/disable"): {"was_configured": 1}})
    cfg = GuildConfig(client)
    result = await cfg.disable("1")
    assert result is True

    client.responses[("POST", "/guilds/2/disable")] = {}
    assert await cfg.disable("2") is False


async def test_leaderboard_defaults_to_a_top_ten():
    """/leaderboard passes limit explicitly, so the default is API surface only."""
    rows = [{"user_id": str(i), "total_seconds": i} for i in range(1, 26)]
    client = FakeClient({("GET", "/streaks/5"): {"rows": rows}})
    store = StreakStore(client)
    top = await store.leaderboard("5", "total", now=100)
    assert len(top) == 10
    assert top[0] == ("25", 25)


def test_leaderboard_entries_are_rendered_as_mentionable_strings():
    """<@{user_id}> only resolves for the id as text, and rank() promises str."""
    rows = [{"user_id": 987654321098745632, "total_seconds": 60}]
    assert rank(rows, "total", 10, 100) == [("987654321098745632", 60)]
    assert isinstance(rank(rows, "total", 10, 100)[0][0], str)


def test_future_streak_time_cannot_produce_negative_totals():
    assert summarize({"active_since": 200, "total_seconds": 20, "longest_seconds": 10}, 100) == {
        "current": 0, "longest": 10, "total": 20, "active": True,
    }


def test_leaderboard_excludes_zero_scores_and_honors_limit():
    rows = [{"user_id": str(i), "total_seconds": i} for i in range(4)]
    assert rank(rows, "total", 2, 1000) == [("3", 3), ("2", 2)]
    assert rank(rows, "current", 2, 1000) == []
    assert rank(rows, "total", 0, 1000) == []


@pytest.mark.parametrize("seconds,expected", [
    (0, "less than a minute"), (59, "less than a minute"), (60, "1m"),
    (3599, "59m"), (3600, "1h"), (86400, "1d"), (86460, "1d"),
    # The second either side of a day. Without 86399 the day divisor can be
    # off by one and every case above still reads correctly, because 86400
    # renders as "1d" under both.
    (86399, "23h 59m"), (86401, "1d"),
    # Days and hours are both shown, but minutes are dropped once a duration
    # spans days, so this is "1d" and not "1d 1m".
    (90000, "1d 1h"), (86520, "1d"),
])
def test_reward_duration_display_boundaries(seconds, expected):
    assert humanize(seconds) == expected


def test_active_streak_at_epoch_retains_historical_best():
    row = {"active_since": 0, "longest_seconds": 200, "total_seconds": 300}
    assert summarize(row, 100) == {"current": 100, "longest": 200, "total": 400, "active": True}


def test_equal_reward_scores_keep_source_order():
    rows = [{"user_id": uid, "total_seconds": 60} for uid in ("b", "a", "c")]
    assert rank(rows, "total", 2, 100) == [("b", 60), ("a", 60)]


# --------------------------------------------------------------------------- #
# Singleton wiring. main() builds both stores over one StoreClient, while
# commands and the gateway handlers reach them through get_instance(). If those
# two halves ever stop agreeing, the bot quietly runs on a second store built
# from the environment, and nothing else in the suite would notice.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("service", [GuildConfig, StreakStore])
def test_constructing_a_store_makes_it_the_shared_one(service):
    store = service(FakeClient())
    assert service.get_instance() is store


@pytest.mark.parametrize("service", [GuildConfig, StreakStore])
def test_get_instance_builds_the_store_once(service, monkeypatch):
    monkeypatch.setattr(StoreClient, "get_instance", classmethod(lambda cls: FakeClient()))
    assert service.get_instance() is service.get_instance()


async def test_stores_default_to_the_shared_store_client(monkeypatch):
    """Neither store should open its own connection to the Worker."""
    shared = FakeClient({("GET", "/guilds/1"): {"role_id": "10"}})
    monkeypatch.setattr(StoreClient, "get_instance", classmethod(lambda cls: shared))

    assert await GuildConfig.get_instance().get_role("1") == "10"
    await StreakStore.get_instance().mark("1", "2", True, now=5)

    assert [call[:2] for call in shared.calls] == [
        ("GET", "/guilds/1"), ("POST", "/streaks/1/mark"),
    ]


@pytest.mark.parametrize("service", [GuildConfig, StreakStore])
def test_lazy_store_without_credentials_reports_configuration(service):
    """An unconfigured host should say which variables are missing."""
    with pytest.raises(StoreError, match="TAGBOT_STORE_URL"):
        service.get_instance()
    # The failed attempt must not leave a half-built store behind.
    assert service._instance is None
