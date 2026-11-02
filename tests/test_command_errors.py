"""The shared command failure path: what gets logged, and what the user is told.

Every assertion here is about a command that went wrong, which is the one case
the happy-path tests cannot reach. Two surfaces are covered because discord.py
has two: an app command callback reports through CommandTree.on_error, and a
button on that command's own reply reports through View.on_error, whose default
implementation never answers the interaction at all.
"""

import asyncio
import inspect
import io
import json
import logging
import pkgutil
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from discord import app_commands

import src
from src.commands.setup import SyncPromptView
from src.core.bot import Bot
from src.core.view import GuardedView
from src.services import command_errors
from src.services.command_errors import (BAD_OPTION_MESSAGE, STALE_COMMAND_MESSAGE,
                                         STORE_REJECTED_MESSAGE, STORE_UNAVAILABLE_MESSAGE,
                                         UNEXPECTED_MESSAGE, classify, origin, report, tell,
                                         unwrap)
from src.services.logger import JsonFormatter
from src.services.store_client import StoreError, StoreUnavailable


@pytest.fixture
def interaction():
    """An interaction that has not been answered yet."""
    return SimpleNamespace(
        command=SimpleNamespace(name="setup"), data={},
        response=SimpleNamespace(is_done=lambda: False, send_message=AsyncMock(),
                                 edit_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )


@pytest.fixture
def emitted(monkeypatch):
    """Capture what the shared logger really writes, formatter included.

    Asserting on the formatted output rather than the record is the only way to
    see what redaction left behind, which is the whole point of these checks.
    """
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    monkeypatch.setattr(command_errors.log, "handlers", [handler])
    return stream


def http_error(status, code=0, text=""):
    body = {"code": code, "message": "Discord said no"} if code else "Discord said no"
    error = discord.HTTPException(SimpleNamespace(status=status, reason="reason"), body)
    if text:
        error.text = text
    return error


def transformer_error():
    """The failure discord.py raises when an option cannot be resolved."""
    return app_commands.TransformerError("4", discord.AppCommandOptionType.user,
                                         app_commands.Transformer())


# --- classification ---------------------------------------------------------

@pytest.mark.parametrize("error,message,level,unexpected", [
    (StoreUnavailable("offline"), STORE_UNAVAILABLE_MESSAGE, logging.ERROR, False),
    (StoreError("bad request"), STORE_REJECTED_MESSAGE, logging.ERROR, False),
    # A stale client list and an unresolvable option are not this bot breaking,
    # so they are logged quieter and explained differently.
    (app_commands.CommandNotFound("gone", []), STALE_COMMAND_MESSAGE, logging.WARNING, False),
    (app_commands.CommandSignatureMismatch(SimpleNamespace(name="setup")),
     STALE_COMMAND_MESSAGE, logging.WARNING, False),
    (transformer_error(), BAD_OPTION_MESSAGE, logging.WARNING, False),
    (RuntimeError("boom"), UNEXPECTED_MESSAGE, logging.ERROR, True),
])
def test_classify_separates_the_causes_it_can_act_on(error, message, level, unexpected):
    failure = classify(error)
    assert failure.message == message
    assert failure.level == level
    assert failure.unexpected is unexpected


def test_classify_keeps_a_fixable_discord_refusal_distinct_from_a_bug():
    """A 403 is an admin's role ordering, not an error for the owner to chase."""
    failure = classify(http_error(403, 50013))
    assert "Manage Roles" in failure.message
    assert failure.unexpected is False
    assert "HTTP 403 (Discord error 50013)" in failure.cause


def test_classify_does_not_put_a_discord_response_body_in_the_reply():
    error = http_error(400, 50035, text="Invalid Form Body: private-internal-detail")
    failure = classify(error)
    assert "private-internal-detail" not in failure.message
    assert "private-internal-detail" not in failure.cause


def test_unwrap_reaches_the_error_the_command_actually_raised():
    """discord.py hands the tree a CommandInvokeError wrapping the real cause."""
    cause = StoreUnavailable("offline")
    wrapped = app_commands.CommandInvokeError(SimpleNamespace(name="sync"), cause)
    assert unwrap(wrapped) is cause
    assert classify(unwrap(wrapped)).message == STORE_UNAVAILABLE_MESSAGE
    # An unwrapped error is returned as-is rather than losing itself.
    assert unwrap(cause) is cause


# --- the raising location, since the formatter keeps no traceback ------------

def test_origin_names_the_line_that_raised():
    try:
        raise RuntimeError("boom")
    except RuntimeError as error:
        located = origin(error)
    assert "tests/test_command_errors.py:" in located
    assert "in test_origin_names_the_line_that_raised" in located


def test_origin_is_empty_for_an_exception_that_was_never_raised():
    assert origin(RuntimeError("constructed, not raised")) == ""


def test_origin_of_a_dependency_failure_hides_the_host_filesystem_layout():
    """An absolute path outside the repo would only expose the host's layout.

    Compiled against a path outside the repository so the traceback is real and
    the filename is the same wherever this runs.
    """
    inside_a_dependency = compile("raise RuntimeError('boom')",
                                  "/opt/vendor/aiohttp/client.py", "exec")
    try:
        exec(inside_a_dependency, {})
    except RuntimeError as error:
        located = origin(error)
    assert located == " at client.py:1 in <module>"


# --- delivering the reply ---------------------------------------------------

async def test_tell_answers_an_untouched_interaction_directly(interaction):
    assert await tell(interaction, "nope") is True
    interaction.response.send_message.assert_awaited_once_with("nope", ephemeral=True)
    interaction.followup.send.assert_not_awaited()


async def test_tell_follows_up_an_interaction_that_already_answered(interaction):
    """A deferred or answered interaction cannot be responded to a second time."""
    interaction.response.is_done = lambda: True
    assert await tell(interaction, "nope") is True
    interaction.followup.send.assert_awaited_once_with("nope", ephemeral=True)
    interaction.response.send_message.assert_not_awaited()


@pytest.mark.parametrize("failure", [
    discord.HTTPException(SimpleNamespace(status=404, reason="Not Found"), "unknown webhook"),
    # InteractionResponded is a ClientException, not an HTTPException, so a
    # catch narrowed to HTTP errors would let it escape into discord.py.
    discord.InteractionResponded(SimpleNamespace(id=1)),
    # Reaching Discord can fail outright; aiohttp's connection errors and
    # TimeoutError are both OSError subclasses.
    TimeoutError("timed out"),
    ConnectionResetError("connection reset"),
])
async def test_tell_reports_rather_than_raises_when_the_reply_fails(interaction, failure):
    interaction.response.send_message = AsyncMock(side_effect=failure)
    assert await tell(interaction, "nope") is False


# --- report: one line, then the user ----------------------------------------

async def test_report_logs_one_line_and_answers_the_user(interaction, emitted):
    await report(interaction, StoreUnavailable("offline"), subject="/setup")
    raw = emitted.getvalue()
    assert len(raw.splitlines()) == 1
    entry = json.loads(raw)
    assert entry["level"] == "ERROR"
    assert "/setup could not reach the store" in entry["message"]
    assert "StoreUnavailable" in entry["message"]
    assert interaction.response.send_message.await_args.args[0] == STORE_UNAVAILABLE_MESSAGE
    assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True


async def test_report_records_where_an_unexpected_error_came_from(interaction, emitted):
    """JsonFormatter keeps no traceback, so the location is all there is to go on."""
    try:
        raise RuntimeError("boom")
    except RuntimeError as error:
        await report(interaction, error, subject="/setup")
    entry = json.loads(emitted.getvalue())
    assert "raised an unexpected error at tests/test_command_errors.py:" in entry["message"]
    assert "RuntimeError" in entry["message"]


async def test_report_says_so_when_the_user_could_not_be_told(interaction, emitted):
    """A failure nobody heard about reads very differently during an incident."""
    interaction.response.send_message = AsyncMock(
        side_effect=discord.HTTPException(SimpleNamespace(status=404, reason="gone"), "expired"))
    await report(interaction, StoreUnavailable("offline"), subject="/setup")
    lines = emitted.getvalue().splitlines()
    assert len(lines) == 2
    assert "could not be reported to the user" in json.loads(lines[1])["message"]


async def test_report_does_not_leak_a_credential_from_the_exception(interaction, emitted,
                                                                   monkeypatch):
    """The store's bearer key reaches this path inside aiohttp and store errors."""
    secret = "synthetic-store-bearer-value"
    monkeypatch.setenv("TAGBOT_STORE_KEY", secret)
    await report(interaction, StoreError(f"401 for Bearer {secret}"), subject="/setup")
    raw = emitted.getvalue()
    assert secret not in raw
    # The type survives redaction, so the line is still worth reading.
    assert "StoreError" in json.loads(raw)["message"]
    assert secret not in interaction.response.send_message.await_args.args[0]


# --- the command surface ----------------------------------------------------

async def test_the_tree_handler_reports_through_the_shared_path(interaction, emitted):
    """Bot._on_tree_error is the only route Discord uses for a command failure."""
    await Bot._on_tree_error(None, interaction, app_commands.CommandInvokeError(
        SimpleNamespace(name="setup"), StoreUnavailable("offline")))
    assert "/setup could not reach the store" in json.loads(emitted.getvalue())["message"]
    assert interaction.response.send_message.await_args.args[0] == STORE_UNAVAILABLE_MESSAGE


# --- the component surface --------------------------------------------------

async def test_a_failing_button_answers_the_person_who_pressed_it(interaction, emitted):
    """View.on_error logs and returns by default, leaving the press unanswered."""
    view = SyncPromptView("1")
    view.sync_now.callback = AsyncMock(side_effect=StoreUnavailable("offline"))
    try:
        # Drive discord.py's own dispatch rather than calling on_error, so this
        # covers the wiring as well as the handler.
        await discord.ui.View._scheduled_task(view, view.sync_now, interaction)
    finally:
        view.stop()

    entry = json.loads(emitted.getvalue())
    assert "/setup (Sync now) could not reach the store" in entry["message"]
    assert interaction.response.send_message.await_args.args[0] == STORE_UNAVAILABLE_MESSAGE
    assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True


async def test_a_button_failure_is_not_left_to_the_library_default():
    """The default implementation only logs, which is what this replaces."""
    assert SyncPromptView.on_error is GuardedView.on_error
    assert GuardedView.on_error is not discord.ui.View.on_error


async def test_an_unlabelled_item_is_named_by_its_type(interaction, emitted):
    """Not every component carries a label; the log line still has to identify it."""
    view = GuardedView()
    view.command_name = "setup"
    item = discord.ui.Select(options=[discord.SelectOption(label="x")])
    await view.on_error(interaction, StoreUnavailable("offline"), item)
    assert "/setup (Select)" in json.loads(emitted.getvalue())["message"]


async def test_the_view_reports_without_the_command_on_the_interaction(interaction, emitted):
    """A component interaction carries no command, so the view supplies the name."""
    interaction.command = None
    view = SyncPromptView("1")
    try:
        await view.on_error(interaction, StoreUnavailable("offline"), view.later)
    finally:
        view.stop()
    assert "/setup (Later)" in json.loads(emitted.getvalue())["message"]


def test_every_view_in_the_bot_reports_its_failures():
    """A view added without GuardedView would go quiet on failure again.

    Asserted over a walk of src rather than a hand-kept list, so a new view
    cannot be added without either extending GuardedView or failing here.
    """
    views = set()
    for module in pkgutil.walk_packages(src.__path__, prefix="src."):
        for _, cls in inspect.getmembers(import_module(module.name), inspect.isclass):
            if issubclass(cls, discord.ui.View) and cls.__module__.startswith("src."):
                views.add(cls)
    assert SyncPromptView in views, "the walk found no views, so it checks nothing"
    for cls in views - {GuardedView}:
        assert issubclass(cls, GuardedView), f"{cls.__name__} must extend GuardedView"
        assert cls.command_name != "?", f"{cls.__name__} must name its command"


async def test_a_button_that_works_reports_nothing(interaction, emitted, monkeypatch):
    """The failure path must stay out of the way of a press that succeeds."""
    from src.services.tag_sync import TagSyncService
    monkeypatch.setattr(TagSyncService, "_instance",
                        SimpleNamespace(is_syncing=lambda gid: True, safe_full_sync=AsyncMock()))
    view = SyncPromptView("1")
    try:
        await discord.ui.View._scheduled_task(view, view.sync_now, interaction)
        await asyncio.sleep(0)
    finally:
        view.stop()
    assert emitted.getvalue() == ""
    interaction.response.send_message.assert_not_awaited()
