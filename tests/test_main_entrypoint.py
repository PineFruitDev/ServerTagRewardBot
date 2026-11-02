"""Coverage for main.main(), the process entry point, and the startup sync branch.

main() was outside the measured scope and had no tests, so the part of the bot
a supervisor actually interacts with was unpinned: what it does before it
connects, and what exit code it leaves behind. Those codes are a contract.
systemd and Docker restart policies read them, so "bad token" has to be a
failure (1) and "Ctrl-C" has to be a clean stop (0). Returning 0 for a reset
token would leave a restart loop looking healthy.

main.run is replaced throughout; nothing here connects to Discord.
"""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

import main
from src.services import tasks
from src.services.environment import Config, Environment
from src.services.guild_config import GuildConfig
from src.services.store_client import StoreClient
from src.services.streaks import StreakStore
from src.services.tag_sync import TagSyncService

TOKEN = "main." + "t" * 60


@pytest.fixture(autouse=True)
def library_loggers():
    """Restore the dependency loggers; main() reconfigures global process state.

    Without this, the first main() call leaves `discord` and `aiohttp` with
    propagate=False for the rest of the session, and any later test reading one
    of their records through caplog silently captures nothing.
    """
    names = ("discord", "aiohttp")
    saved = [(logging.getLogger(n), logging.getLogger(n).handlers[:],
              logging.getLogger(n).level, logging.getLogger(n).propagate) for n in names]
    yield
    for logger, handlers, level, propagate in saved:
        logger.handlers[:] = handlers
        logger.setLevel(level)
        logger.propagate = propagate


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", TOKEN)
    monkeypatch.setenv("TAGBOT_STORE_URL", "https://store.test")
    monkeypatch.setenv("TAGBOT_STORE_KEY", "k" * 48)


@pytest.fixture
def started(monkeypatch):
    """Capture the Config main() hands to run(), without running anything."""
    calls = []
    monkeypatch.setattr(main, "run", AsyncMock(side_effect=lambda config: calls.append(config)))
    return calls


def fail_with(monkeypatch, error):
    monkeypatch.setattr(main, "run", AsyncMock(side_effect=error))


# --------------------------------------------------------------------------- #
# The exit-code contract.
# --------------------------------------------------------------------------- #


def test_a_clean_shutdown_exits_zero(configured, started, caplog):
    with caplog.at_level("INFO"):
        main.main()  # returning normally is exit code 0

    assert "Shutdown complete" in caplog.text
    assert len(started) == 1


@pytest.mark.parametrize("error, expected", [
    (discord.LoginFailure("token was reset"), "DISCORD_TOKEN"),
    (discord.PrivilegedIntentsRequired(shard_id=None), "even without privileged intents"),
])
def test_a_rejected_connection_exits_nonzero(configured, monkeypatch, caplog, error, expected):
    """A credential or intent refusal must not look like a successful run."""
    fail_with(monkeypatch, error)

    # INFO, not CRITICAL: the "Shutdown complete" assertion below is only
    # meaningful if an INFO line could have been captured in the first place.
    with caplog.at_level("INFO"), pytest.raises(SystemExit) as exc:
        main.main()

    assert exc.value.code == 1
    assert expected in caplog.text
    # An aborted start is not a shutdown; claiming otherwise hides the failure.
    assert "Shutdown complete" not in caplog.text


def test_ctrl_c_before_startup_finishes_is_a_clean_stop(configured, monkeypatch, caplog):
    """KeyboardInterrupt is a deliberate stop, so it must not exit nonzero."""
    fail_with(monkeypatch, KeyboardInterrupt())

    with caplog.at_level("INFO"):
        main.main()  # no SystemExit: a Ctrl-C is exit code 0

    assert "Interrupted before startup finished" in caplog.text
    assert "Shutdown complete" in caplog.text


def test_a_token_rejected_without_credentials_is_not_swallowed(configured, monkeypatch):
    """Only the three handled failures are caught; the rest must surface."""
    fail_with(monkeypatch, RuntimeError("something else broke"))

    with pytest.raises(RuntimeError, match="something else broke"):
        main.main()


# --------------------------------------------------------------------------- #
# What happens before the connection opens.
# --------------------------------------------------------------------------- #


def test_invalid_configuration_aborts_before_any_service_is_built(monkeypatch, started):
    """Validation is first, so a bad .env costs nothing and explains itself."""
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)

    with pytest.raises(SystemExit) as exc:
        main.main()

    assert "configuration problem" in str(exc.value)
    assert started == [], "started connecting despite invalid configuration"
    assert StoreClient._instance is None, "built a store client before validating"


def test_startup_order_adopts_library_logging_before_connecting(configured, monkeypatch, started):
    """discord.py's own loggers must be redacted before they can emit anything."""
    order = []
    monkeypatch.setattr(main, "configure_library_logging",
                        lambda: order.append("configure_library_logging"))
    monkeypatch.setattr(main, "run", AsyncMock(side_effect=lambda config: order.append("run")))

    main.main()

    assert order == ["configure_library_logging", "run"]


def test_the_store_backed_services_are_wired_before_the_first_command(configured, started):
    """Commands reach these through get_instance(); main() is what populates them."""
    main.main()

    assert isinstance(StoreClient._instance, StoreClient)
    assert isinstance(GuildConfig._instance, GuildConfig)
    assert isinstance(StreakStore._instance, StreakStore)
    # All three share one client, so there is one connection pool, not three.
    assert GuildConfig._instance._client is StoreClient._instance
    assert StreakStore._instance._client is StoreClient._instance


def test_the_validated_config_is_what_gets_run(configured, monkeypatch, started):
    monkeypatch.setenv("SYNC_ON_START", "true")
    monkeypatch.setenv("MEMBERS_INTENT", "false")

    main.main()

    assert started[0] is Environment.get_config()
    assert started[0].sync_on_start is True
    assert started[0].members_intent is False


# --------------------------------------------------------------------------- #
# on_ready's startup-sync branch (main.py's last uncovered line).
# --------------------------------------------------------------------------- #


def _config(sync_on_start):
    return Config(discord_token="x" * 60, environment="test", sync_on_start=sync_on_start,
                  sync_delay_ms=0, members_intent=True)


@pytest.fixture
def ready(monkeypatch):
    """A bot whose on_ready can run offline, with tag sync stubbed out."""
    monkeypatch.setattr(GuildConfig, "_instance", SimpleNamespace(count=AsyncMock(return_value=1)))
    sync_all = AsyncMock(return_value=None)
    monkeypatch.setattr(TagSyncService, "sync_all_guilds", sync_all)
    return sync_all


async def test_sync_on_start_launches_one_supervised_startup_sync(ready, monkeypatch):
    bot = main.build_bot(_config(sync_on_start=True), members=True)
    bot.sync_commands = AsyncMock(return_value=7)
    try:
        await bot.on_ready()
        # spawn() holds the reference; draining is how the task gets awaited.
        assert tasks.pending() == 1
        assert await tasks.drain(timeout=1) == 0
    finally:
        await bot.close()
        TagSyncService.detach()

    ready.assert_awaited_once()


async def test_startup_sync_stays_off_by_default(ready, monkeypatch):
    """The initial sync belongs to /setup, so a restart must not re-walk guilds."""
    bot = main.build_bot(_config(sync_on_start=False), members=True)
    bot.sync_commands = AsyncMock(return_value=7)
    try:
        await bot.on_ready()
        assert tasks.pending() == 0
    finally:
        await bot.close()
        TagSyncService.detach()

    ready.assert_not_awaited()


async def test_limited_mode_never_starts_a_sync_it_cannot_finish(ready):
    """Without the members intent there is no tag data, so sync_on_start is moot."""
    bot = main.build_bot(_config(sync_on_start=True), members=False)
    bot.sync_commands = AsyncMock(return_value=7)
    try:
        await bot.on_ready()
        assert tasks.pending() == 0
    finally:
        await bot.close()

    ready.assert_not_awaited()
