"""Keep tests independent of local credentials, .env files, and singleton state."""

from unittest.mock import patch

import pytest

from src.services import tasks
from src.services.cooldowns import Cooldowns
from src.services.environment import Environment, OPTIONAL_VARS, REQUIRED_VARS
from src.services.guild_config import GuildConfig
from src.services.store_client import StoreClient
from src.services.streaks import StreakStore
from src.services.tag_sync import TagSyncService
from tests.integration_harness import (GUILD, ROLE, Clock, CommandBot, FakeStore, TagBot,
                                       config_for_tests, streaks_module)


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
    for service in (Cooldowns, GuildConfig, StoreClient, StreakStore, TagSyncService):
        monkeypatch.setattr(service, "_instance", None)
    monkeypatch.setattr(Environment, "_config", None)
    # A test that drains background work must not leave spawn() closed for the
    # rest of the session.
    monkeypatch.setattr(tasks, "_closing", False)


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
    """Config with no inter-request sync delay, so full syncs run at test speed.

    Rate limits are off here so that a test is free to run one command twice
    and to replay one gateway event. The limits themselves are configured
    explicitly, against a clock the test controls, in tests/test_cooldowns.py.
    """
    config = config_for_tests()
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
