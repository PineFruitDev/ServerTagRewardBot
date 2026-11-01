"""Keep tests independent of local credentials, .env files, and singleton state."""

from unittest.mock import patch

import pytest

from src.services import discord_retry, tasks
from src.services.environment import Config, Environment, OPTIONAL_VARS, REQUIRED_VARS
from src.services.guild_config import GuildConfig
from src.services.store_client import StoreClient
from src.services.streaks import StreakStore
from src.services.tag_sync import TagSyncService
from tests.integration_harness import (GUILD, ROLE, Clock, CommandBot, FakeStore, TagBot,
                                       streaks_module)


def pytest_sessionstart(session):
    # main imports load_dotenv during collection, before fixtures can run.
    session.config._dotenv_patch = patch("dotenv.load_dotenv", return_value=False)
    session.config._dotenv_patch.start()


def pytest_sessionfinish(session):
    session.config._dotenv_patch.stop()


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch):
    for name in (*REQUIRED_VARS, *OPTIONAL_VARS):
        monkeypatch.delenv(name, raising=False)
    for service in (GuildConfig, StoreClient, StreakStore, TagSyncService):
        monkeypatch.setattr(service, "_instance", None)
    monkeypatch.setattr(Environment, "_config", None)
    # A test that drains background work must not leave spawn() closed for the
    # rest of the session.
    monkeypatch.setattr(tasks, "_closing", False)
    # Retry the Discord REST paths without waiting between attempts. The
    # attempts themselves stay on, so every test keeps exercising the real
    # retry behaviour; only the wall clock is given up. tests/test_discord_retry
    # sets a real backoff explicitly and asserts the delays against a fake sleep.
    monkeypatch.setattr(discord_retry, "BACKOFF_SECONDS", 0.0)


# --------------------------------------------------------------------------- #
# Integration fixtures. The fakes and harnesses they build are in
# tests/integration_harness.py, which explains what they are for.
# --------------------------------------------------------------------------- #


@pytest.fixture
def clock(monkeypatch):
    """Freeze the clock StreakStore stamps marks with, so durations are exact."""
    ticking = Clock()
    monkeypatch.setattr(streaks_module.time, "time", ticking)
    return ticking


@pytest.fixture
def test_config(isolated_state):
    """Config with no inter-request sync delay, so full syncs run at test speed."""
    config = Config(discord_token="x" * 60, environment="test", sync_on_start=False,
                    sync_delay_ms=0, members_intent=True)
    Environment._config = config
    return config


@pytest.fixture
def store():
    return FakeStore()


@pytest.fixture
def bot(store, test_config):
    """Gateway-driven bot in a guild that already rewards a role."""
    store.seed_guild(GUILD, ROLE)
    return TagBot(store)


@pytest.fixture
def fresh_bot(store, test_config):
    """Gateway-driven bot in a guild nobody has run /setup in yet."""
    return TagBot(store)


@pytest.fixture
async def ui(store, test_config):
    """Command-driven bot in a guild that already rewards a role."""
    store.seed_guild(GUILD, ROLE)
    harness = CommandBot(store, test_config)
    yield harness
    await harness.close()


@pytest.fixture
async def fresh_ui(store, test_config):
    """Command-driven bot in a guild nobody has run /setup in yet."""
    harness = CommandBot(store, test_config)
    yield harness
    await harness.close()


@pytest.fixture
async def limited_ui(store, test_config):
    """Command-driven bot running without the Server Members intent."""
    store.seed_guild(GUILD, ROLE)
    harness = CommandBot(store, test_config, members=False)
    yield harness
    await harness.close()


@pytest.fixture
async def fresh_ui_limited(store, test_config):
    """Limited mode in a guild nobody has run /setup in yet."""
    harness = CommandBot(store, test_config, members=False)
    yield harness
    await harness.close()
