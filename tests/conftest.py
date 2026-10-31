"""Keep tests independent of local credentials, .env files, and singleton state."""

from unittest.mock import patch

import pytest

from src.services.environment import Environment, OPTIONAL_VARS, REQUIRED_VARS
from src.services.guild_config import GuildConfig
from src.services.store_client import StoreClient
from src.services.streaks import StreakStore
from src.services.tag_sync import TagSyncService


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
