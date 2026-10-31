"""MEMBERS_INTENT flag and the limited-mode fallback when Discord refuses the intent."""

import discord
import pytest

import main
from src.services.environment import Config
from src.services.tag_sync import TagSyncService


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

    limited = main.build_bot(_config(), members=False)
    assert limited.intents.members is False and limited.limited_mode is True
    assert TagSyncService.get_instance() is None
    # slash commands are still registered in limited mode
    names = {c.name for c in limited.tree.get_commands()}
    assert {"setup", "status", "repped", "leaderboard"} <= names
