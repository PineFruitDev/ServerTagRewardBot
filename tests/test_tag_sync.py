"""Exercise gateway events and full reconciliation without connecting to Discord."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import discord
import pytest

from src.services.environment import Environment
from src.services.guild_config import GuildConfig
from src.services.store_client import StoreUnavailable
from src.services.streaks import StreakStore
from src.services.tag_sync import TagSyncService


def user(uid="42", guild="1", enabled=True):
    return {"id": uid, "primary_guild": {
        "identity_guild_id": guild, "identity_enabled": enabled,
    }}


def http_error(status=403):
    return discord.HTTPException(SimpleNamespace(status=status, reason="test failure"), "test failure")


@pytest.fixture
def sync(monkeypatch):
    http = SimpleNamespace(**{name: AsyncMock() for name in (
        "add_role", "remove_role", "get_members", "get_user",
    )})
    http.get_members.return_value = []
    client = SimpleNamespace(http=http, event=Mock())
    config = SimpleNamespace(get_role=AsyncMock(return_value="10"), all_guilds=AsyncMock())
    streaks = SimpleNamespace(mark_many=AsyncMock())
    monkeypatch.setattr(GuildConfig, "_instance", config)
    monkeypatch.setattr(StreakStore, "_instance", streaks)
    monkeypatch.setattr(Environment, "_config", SimpleNamespace(sync_delay_ms=350))
    service = TagSyncService(client)
    return SimpleNamespace(service=service, http=http, config=config, streaks=streaks)


@pytest.mark.parametrize("payload,expected", [
    ({}, False), ({"primary_guild": None}, False),
    (user(), True), (user(enabled=False), False), (user(guild="2"), False),
    ({"primary_guild": {"identity_enabled": True}}, False),
])
def test_tag_must_be_enabled_for_this_guild(payload, expected):
    assert TagSyncService.is_repping(payload, "1") is expected


@pytest.mark.parametrize("should_have,roles,action", [
    (True, [], "add_role"), (True, ["10"], None),
    (False, ["10"], "remove_role"), (False, [], None),
    (True, None, "add_role"), (False, None, "remove_role"),
    (True, ["100"], "add_role"),
])
async def test_role_changes_skip_redundant_calls(sync, should_have, roles, action):
    await sync.service.apply_role("1", "10", "42", should_have, roles)
    for name in ("add_role", "remove_role"):
        mock = getattr(sync.http, name)
        if name == action:
            mock.assert_awaited_once_with(1, 42, 10)
        else:
            mock.assert_not_awaited()
    stats = sync.service.stats_for("1")
    assert (stats.roles_added, stats.roles_removed) == (int(action == "add_role"), int(action == "remove_role"))


@pytest.mark.parametrize("should_have,action", [(True, "add_role"), (False, "remove_role")])
async def test_failed_role_change_does_not_count_as_success(sync, should_have, action):
    getattr(sync.http, action).side_effect = http_error()
    await sync.service.apply_role("1", "10", "42", should_have)
    stats = sync.service.stats_for("1")
    assert (stats.roles_added, stats.roles_removed) == (0, 0)


@pytest.mark.parametrize("event,repping,action", [
    ("GUILD_MEMBER_UPDATE", True, "add_role"),
    ("GUILD_MEMBER_UPDATE", False, "remove_role"),
    ("GUILD_MEMBER_ADD", True, "add_role"),
    ("GUILD_MEMBER_ADD", False, None),
])
async def test_gateway_events_reconcile_roles_and_streaks(sync, event, repping, action):
    payload = {"t": event, "d": {"guild_id": "1", "user": user(enabled=repping)}}
    await sync.service.on_socket_raw_receive(json.dumps(payload))
    sync.config.get_role.assert_awaited_once_with("1")
    if action:
        getattr(sync.http, action).assert_awaited_once_with(1, 42, 10)
        sync.streaks.mark_many.assert_awaited_once_with("1", [("42", repping)])
    else:
        sync.streaks.mark_many.assert_not_awaited()
    assert sync.http.add_role.await_count == int(action == "add_role")
    assert sync.http.remove_role.await_count == int(action == "remove_role")
    assert sync.service.stats_for("1").live_updates == int(event == "GUILD_MEMBER_UPDATE")


@pytest.mark.parametrize("payload", [
    b"compressed", "{", "null", "[]", "{}",
    '{"t":"MESSAGE_CREATE","d":{}}',
    '{"t":"GUILD_MEMBER_UPDATE","d":null}',
    '{"t":"GUILD_MEMBER_UPDATE","d":{"guild_id":"1"}}',
    '{"t":"GUILD_MEMBER_UPDATE","d":{"user":{}}}',
    '{"t":"GUILD_MEMBER_UPDATE","d":{"guild_id":"1","user":null}}',
])
async def test_malformed_or_irrelevant_events_cannot_change_roles(sync, payload):
    await sync.service.on_socket_raw_receive(payload)
    sync.http.add_role.assert_not_awaited()
    sync.http.remove_role.assert_not_awaited()
    sync.streaks.mark_many.assert_not_awaited()


async def test_unconfigured_guild_ignores_gateway_and_full_sync(sync):
    sync.config.get_role.return_value = None
    await sync.service.on_socket_raw_receive(json.dumps({
        "t": "GUILD_MEMBER_UPDATE", "d": {"guild_id": "1", "user": user()},
    }))
    stats = await sync.service.full_sync("1")
    assert stats.last_sync_at is None
    sync.http.add_role.assert_not_awaited()
    sync.http.get_members.assert_not_awaited()
    sync.streaks.mark_many.assert_not_awaited()


async def test_gateway_recovers_after_store_failure(sync):
    sync.config.get_role.side_effect = [StoreUnavailable("offline"), "10"]
    event = json.dumps({"t": "GUILD_MEMBER_UPDATE", "d": {"guild_id": "1", "user": user()}})
    await sync.service.on_socket_raw_receive(event)
    await sync.service.on_socket_raw_receive(event)
    sync.http.add_role.assert_awaited_once_with(1, 42, 10)


async def test_full_sync_refetches_only_missing_tags_and_preserves_unknown_members(sync, monkeypatch):
    sync.http.get_members.return_value = [
        {"user": user("1"), "roles": []},
        {"user": {"id": "2", "primary_guild": None}, "roles": ["10"]},
        {"user": {"id": "3"}, "roles": []},
        {"user": {"id": "4"}, "roles": ["10"]},
    ]
    sync.http.get_user.side_effect = [user("3"), http_error(404)]
    sleep = AsyncMock()
    monkeypatch.setattr("src.services.tag_sync.asyncio.sleep", sleep)
    stats = await sync.service.full_sync("1")
    assert sync.http.get_user.await_args_list == [call(3), call(4)]
    sleep.assert_awaited_once_with(0.35)
    assert sync.http.add_role.await_args_list == [call(1, 1, 10), call(1, 3, 10)]
    sync.http.remove_role.assert_awaited_once_with(1, 2, 10)
    sync.streaks.mark_many.assert_awaited_once_with("1", [("1", True), ("2", False), ("3", True)])
    assert (stats.members_checked, stats.currently_repping) == (4, 2)
    assert stats.last_sync_at.utcoffset().total_seconds() == 0


@pytest.mark.parametrize("member_count", [0, 1000, 1001])
async def test_full_sync_paginates_without_losing_members(sync, member_count):
    members = [{"user": user(str(i)), "roles": ["10"]} for i in range(1, member_count + 1)]
    sync.http.get_members.side_effect = [members[:1000], members[1000:]]
    stats = await sync.service.full_sync(1)
    expected = [call(1, limit=1000, after=0)]
    if member_count >= 1000:
        expected.append(call(1, limit=1000, after=1000))
    assert sync.http.get_members.await_args_list == expected
    marked = [mark for c in sync.streaks.mark_many.await_args_list for mark in c.args[1]]
    assert marked == [(str(i), True) for i in range(1, member_count + 1)]
    assert (stats.members_checked, stats.currently_repping) == (member_count, member_count)
    assert stats.last_sync_at is not None
    sync.http.add_role.assert_not_awaited()


async def test_streak_outage_does_not_block_role_reconciliation(sync):
    sync.http.get_members.return_value = [{"user": user(), "roles": []}]
    sync.streaks.mark_many.side_effect = StoreUnavailable("offline")
    stats = await sync.service.full_sync("1")
    sync.http.add_role.assert_awaited_once_with(1, 42, 10)
    assert stats.members_checked == 1
    assert not sync.service.is_syncing("1")


async def test_one_sync_per_guild_but_other_guilds_can_progress(sync):
    entered, release = asyncio.Event(), asyncio.Event()

    async def get_members(guild_id, **kwargs):
        if guild_id == 1:
            entered.set()
            await release.wait()
        return []

    sync.http.get_members.side_effect = get_members
    first = asyncio.create_task(sync.service.full_sync("1"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert sync.service.is_syncing("1")
        await sync.service.full_sync("1")
        other = await sync.service.full_sync("2")
        assert other.last_sync_at is not None
        assert sync.http.get_members.await_args_list == [
            call(1, limit=1000, after=0), call(2, limit=1000, after=0),
        ]
    finally:
        release.set()
        await first
    assert not sync.service.is_syncing("1")


async def test_failed_or_cancelled_sync_releases_lock(sync):
    sync.http.get_members.side_effect = http_error(503)
    with pytest.raises(discord.HTTPException):
        await sync.service.full_sync("1")
    assert not sync.service.is_syncing("1")
    assert sync.service.stats_for("1").last_sync_at is None
    sync.http.get_members.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await sync.service.full_sync("1")
    assert not sync.service.is_syncing("1")
    sync.http.get_members.side_effect = None
    assert (await sync.service.full_sync("1")).last_sync_at is not None


async def test_startup_sync_continues_after_one_guild_fails(sync, monkeypatch):
    sync.config.all_guilds.return_value = [("1", "10"), ("2", "20")]
    full_sync = AsyncMock(side_effect=[http_error(503), None])
    monkeypatch.setattr(sync.service, "full_sync", full_sync)
    await sync.service.sync_all_guilds()
    assert full_sync.await_args_list == [call("1"), call("2")]


async def test_background_sync_errors_are_contained(sync, monkeypatch):
    full_sync = AsyncMock(side_effect=StoreUnavailable("offline"))
    monkeypatch.setattr(sync.service, "full_sync", full_sync)
    await sync.service.safe_full_sync("1")
    sync.config.all_guilds.side_effect = StoreUnavailable("offline")
    await sync.service.sync_all_guilds()
    full_sync.assert_awaited_once_with("1")
