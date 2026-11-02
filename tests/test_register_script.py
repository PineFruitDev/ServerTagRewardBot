"""Coverage for register.py, the hand-run slash command registration entry point.

register.py is the only production module no test imported, so the coverage
floor said nothing about it. It is run by hand after a command changes, and it
has one property that is easy to lose and expensive to lose: it has to hang up.
`on_ready` closes the connection in a `finally`, so a sync that raises still
ends the process instead of parking it on the gateway until someone notices the
terminal never came back.

Nothing here opens a connection. `Bot.run` is replaced with a stand-in that
drives the registered `on_ready` the way the gateway would.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

import register
from src.commands import ALL_COMMANDS

TOKEN = "register." + "t" * 60


def http_error(message):
    """An HTTPException carrying a response the library can read a status off."""
    return discord.HTTPException(SimpleNamespace(status=500, reason="Internal Server Error"), message)


def registered_lines(caplog):
    """Only register.py's inventory lines; CommandManager logs its own."""
    return [record.getMessage() for record in caplog.records
            if record.name == "Register" and "Registered: /" in record.getMessage()]


@pytest.fixture
def configured(monkeypatch):
    """A valid environment, so Environment.validate() passes on its own terms."""
    monkeypatch.setenv("DISCORD_TOKEN", TOKEN)
    monkeypatch.setenv("TAGBOT_STORE_URL", "https://store.test")
    monkeypatch.setenv("TAGBOT_STORE_KEY", "k" * 48)


@pytest.fixture
def gateway(monkeypatch):
    """Replace Bot.run with a stand-in that records the call and fires on_ready.

    Yields the record; `ready` is None until something calls run(), which is how
    the tests assert that validation happens before the bot ever connects.
    """
    record = {"ready": None, "token": None, "kwargs": None, "closed": 0}

    def fake_run(self, token, **kwargs):
        record["token"] = token
        record["kwargs"] = kwargs
        real_close = self.close

        async def counting_close():
            record["closed"] += 1
            await real_close()

        self.close = counting_close
        try:
            asyncio.run(self.on_ready())
            record["ready"] = "returned"
        except BaseException as exc:  # recorded, then re-raised to the caller
            record["ready"] = exc
            raise

    monkeypatch.setattr(register.Bot, "run", fake_run)
    return record


def test_validation_runs_before_the_bot_connects(gateway, monkeypatch):
    """A misconfigured environment must abort, not open a gateway session."""
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)

    with pytest.raises(SystemExit) as exc:
        register.main()

    assert "configuration problem" in str(exc.value)
    assert gateway["token"] is None, "connected despite invalid configuration"


def test_run_receives_the_token_and_suppresses_discords_log_handler(configured, gateway, monkeypatch):
    """log_handler=None keeps discord.py from replacing this bot's JSON logging."""
    monkeypatch.setattr(register.Bot, "sync_commands", AsyncMock(return_value=0))

    register.main()

    assert gateway["token"] == TOKEN
    assert gateway["kwargs"] == {"log_handler": None}


def test_registration_syncs_the_tree_then_hangs_up(configured, gateway, monkeypatch, caplog):
    sync = AsyncMock(return_value=len(ALL_COMMANDS))
    monkeypatch.setattr(register.Bot, "sync_commands", sync)

    with caplog.at_level("INFO"):
        register.main()

    sync.assert_awaited_once()
    assert gateway["closed"] == 1, "the script stayed connected after registering"
    assert f"Successfully reloaded {len(ALL_COMMANDS)} application (/) commands" in caplog.text


def test_a_failed_sync_still_closes_the_connection(configured, gateway, monkeypatch):
    """The `finally` is the whole point: a raising sync must not leave us online.

    Without it the script keeps the gateway session open forever and the
    operator gets a hung terminal rather than a stack trace and a shell prompt.
    """
    monkeypatch.setattr(register.Bot, "sync_commands",
                        AsyncMock(side_effect=http_error("Discord rejected the tree")))

    with pytest.raises(discord.HTTPException):
        register.main()

    assert gateway["closed"] == 1, "a failed sync left the connection open"


def test_every_command_is_listed_once(configured, gateway, monkeypatch, caplog):
    """The printed inventory is the only confirmation the operator gets."""
    monkeypatch.setattr(register.Bot, "sync_commands", AsyncMock(return_value=len(ALL_COMMANDS)))

    with caplog.at_level("INFO"):
        register.main()

    registered = registered_lines(caplog)
    assert len(registered) == len(ALL_COMMANDS)
    for command in ALL_COMMANDS:
        info = command.help_info
        assert any(f"/{info.name} [{info.category}]" in line for line in registered)


def test_registration_does_not_request_a_privileged_intent(configured, gateway, monkeypatch):
    """Pushing the command tree is an HTTP call; it must not need members.

    Requesting the privileged intent here would make the script fail with close
    code 4014 on any bot that has not been approved for it, for no benefit.
    """
    built = {}
    original = register.Bot.__init__

    def record_init(self, commands, intents=None, **kwargs):
        built["intents"] = intents
        original(self, commands, intents=intents, **kwargs)

    monkeypatch.setattr(register.Bot, "__init__", record_init)
    monkeypatch.setattr(register.Bot, "sync_commands", AsyncMock(return_value=0))

    register.main()

    assert built["intents"].members is False
    assert built["intents"] == discord.Intents.default()
