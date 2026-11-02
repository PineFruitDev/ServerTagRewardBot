"""MEMBERS_INTENT flag and the limited-mode fallback when Discord refuses the intent."""

import asyncio

import discord
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import main
from src.core.bot import CommandTree
from src.services.environment import Config
from src.services.tag_sync import TagSyncService
from src.services.guild_config import GuildConfig
from src.services.store_client import StoreUnavailable


def _config(members_intent=True):
    return Config(discord_token="x" * 60, environment="test", sync_on_start=False,
                  sync_delay_ms=0, members_intent=members_intent)


async def test_falls_back_to_limited_mode_on_4014(monkeypatch):
    calls = []

    async def fake_start(config, members):
        calls.append(members)
        if members:
            raise discord.PrivilegedIntentsRequired(None)

    monkeypatch.setattr(main, "start", fake_start)
    await main.run(_config())
    assert calls == [True, False]


async def test_full_mode_when_intent_granted(monkeypatch):
    calls = []

    async def fake_start(config, members):
        calls.append(members)

    monkeypatch.setattr(main, "start", fake_start)
    await main.run(_config())
    assert calls == [True]


async def test_flag_off_skips_the_attempt(monkeypatch):
    calls = []

    async def fake_start(config, members):
        calls.append(members)

    monkeypatch.setattr(main, "start", fake_start)
    await main.run(_config(members_intent=False))
    assert calls == [False]


async def test_refusal_in_limited_mode_propagates(monkeypatch):
    async def fake_start(config, members):
        raise discord.PrivilegedIntentsRequired(None)

    monkeypatch.setattr(main, "start", fake_start)
    with pytest.raises(discord.PrivilegedIntentsRequired):
        await main.run(_config(members_intent=False))


async def test_build_bot_modes():
    full = main.build_bot(_config(), members=True)
    assert full.intents.members is True and full.limited_mode is False
    assert TagSyncService.get_instance() is not None
    # The tree subclass is what reports command failures to the user; a plain
    # app_commands.CommandTree would log them and never reply.
    assert isinstance(full.tree, CommandTree)
    assert full.commands_synced is False

    limited = main.build_bot(_config(), members=False)
    assert limited.intents.members is False and limited.limited_mode is True
    assert TagSyncService.get_instance() is None
    # slash commands are still registered in limited mode
    names = {c.name for c in limited.tree.get_commands()}
    assert {"setup", "status", "repped", "leaderboard"} <= names
    await full.close()
    await limited.close()


@pytest.mark.parametrize("error", [discord.LoginFailure("invalid test login"), RuntimeError("test failure")])
async def test_non_intent_failures_do_not_trigger_limited_mode(monkeypatch, error):
    start = AsyncMock(side_effect=error)
    monkeypatch.setattr(main, "start", start)
    with pytest.raises(type(error)):
        await main.run(_config())
    start.assert_awaited_once()


async def test_ready_retries_failed_command_registration(monkeypatch):
    monkeypatch.setattr(GuildConfig, "_instance", SimpleNamespace(count=AsyncMock(return_value=0)))
    bot = main.build_bot(_config(), members=False)
    bot.sync_commands = AsyncMock(side_effect=[RuntimeError("temporary failure"), 8])
    try:
        with pytest.raises(RuntimeError, match="temporary failure"):
            await bot.on_ready()
        await bot.on_ready()
        await bot.on_ready()
        assert bot.sync_commands.await_count == 2
    finally:
        await bot.close()


async def test_ready_store_outage_does_not_reregister_commands(monkeypatch):
    count = AsyncMock(side_effect=[StoreUnavailable("offline"), 0])
    monkeypatch.setattr(GuildConfig, "_instance", SimpleNamespace(count=count))
    bot = main.build_bot(_config(), members=False)
    bot.sync_commands = AsyncMock(return_value=8)
    try:
        await bot.on_ready()
        await bot.on_ready()
        bot.sync_commands.assert_awaited_once()
        assert count.await_count == 2
    finally:
        await bot.close()


async def test_overlapping_ready_events_register_commands_once(monkeypatch):
    monkeypatch.setattr(GuildConfig, "_instance", SimpleNamespace(count=AsyncMock(return_value=0)))
    entered, release, second_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    bot = main.build_bot(_config(), members=False)

    async def register():
        entered.set()
        await release.wait()
        return 8

    async def second_ready():
        second_started.set()
        await bot.on_ready()

    bot.sync_commands = AsyncMock(side_effect=register)
    first = asyncio.create_task(bot.on_ready())
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(second_ready())
        await asyncio.wait_for(second_started.wait(), 1)
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second else []))
        await bot.close()
    bot.sync_commands.assert_awaited_once()
