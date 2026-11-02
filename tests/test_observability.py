"""What one command invocation leaves behind in the logs.

These assertions are about the shape of the record rather than about what any
command does, because the operator-facing contract is the shape: an id that
ties a command's lines together, the command and guild on every one of them,
and exactly one closing line saying how it ended and how long it took. The
commands themselves are tested elsewhere.

Everything here runs through the real tree callbacks and the real handlers, so
a command that forgets to open an observed() block fails
test_every_registered_command_reports_how_it_ended rather than silently going
unobserved in production.
"""

import asyncio
import logging
from types import SimpleNamespace

import discord
import pytest
from discord import app_commands

import main  # noqa: F401  (imported for its side effect: every logger exists)
from src.commands.setup import SyncPromptView
from src.core.command import Command, CommandHelpInfo
from src.services.command_errors import report
from src.services.logger import context
from src.services.observability import (CANCELLED, DENIED, ERROR, OK, correlated,
                                        correlation_id, mark, observed)
from src.services.store_client import StoreError
from tests.integration_harness import (COMMAND_ARGUMENTS, GUILD, FakeGuild, FakeInteraction,
                                       button, captured_logs)


def finished(logs, command: str) -> dict:
    """The single closing line for one command."""
    return logs.one("Commands", f"/{command} finished")


# --- the correlation id -----------------------------------------------------

def test_the_id_is_stable_for_one_interaction_and_distinct_between_two():
    """Stability is what lets the failure path recompute it rather than store it."""
    first, second = FakeInteraction(), FakeInteraction()
    assert correlation_id(first) == correlation_id(first)
    assert correlation_id(first) != correlation_id(second)


def test_an_interaction_carrying_no_id_still_gets_one():
    """The unit tests build interactions directly; none of them has a snowflake."""
    bare = SimpleNamespace()
    assert correlation_id(bare) == correlation_id(bare)
    assert correlation_id(bare) != correlation_id(SimpleNamespace())


def test_the_id_is_derived_from_discords_own_interaction_id():
    assert correlation_id(SimpleNamespace(id=255)) == "ff"


def test_correlating_an_interaction_missing_everything_does_not_raise():
    """This runs on the failure path, where an interaction can be a wreck."""
    with correlated(SimpleNamespace(), "setup"):
        assert dict(context())["command"] == "setup"


# --- the closing line -------------------------------------------------------

async def test_a_command_closes_with_one_line_saying_how_it_went(ui):
    with captured_logs() as logs:
        await ui.drive("status")
    line = finished(logs, "status")
    assert line["level"] == "INFO"
    assert line["command"] == "status"
    assert line["guild"] == GUILD
    assert line["outcome"] == OK
    assert isinstance(line["latency_ms"], int) and line["latency_ms"] >= 0


async def test_the_closing_line_names_the_user_by_id_and_not_by_name(ui):
    """A display name is user-controlled content and identifies a person; an id does neither."""
    with captured_logs() as logs:
        pressed = await ui.drive("status")
    assert finished(logs, "status")["user"] == str(pressed.user.id)
    assert pressed.user.display_name not in logs.raw


async def test_a_command_is_announced_before_it_runs_as_well(ui):
    """Only a started line survives a command that hangs or takes the process down."""
    with captured_logs() as logs:
        await ui.drive("ping")
    assert logs.one("Commands", "/ping started")["level"] == "DEBUG"


async def test_the_started_line_is_debug_so_production_logs_one_line_per_command(ui):
    with captured_logs(level=logging.INFO) as logs:
        await ui.drive("ping")
    assert [line["message"] for line in logs.lines("Commands")] == ["observed - /ping finished"]


@pytest.mark.parametrize("name", sorted(COMMAND_ARGUMENTS))
async def test_every_registered_command_reports_how_it_ended(ui, name):
    """A callback that forgets its observed() block is invisible to an operator."""
    with captured_logs() as logs:
        await ui.drive(name)
    assert finished(logs, name)["outcome"] == OK


def test_the_registry_is_fully_covered(ui):
    registered = {command.name for command in ui.bot.tree.get_commands()}
    assert registered == set(COMMAND_ARGUMENTS)


async def test_disable_is_not_reported_as_the_setup_it_is_registered_beside(ui):
    with captured_logs() as logs:
        await ui.drive("disable")
    assert finished(logs, "disable")["command"] == "disable"
    assert not logs.lines("Commands", "/setup")


# --- outcomes ---------------------------------------------------------------

async def test_a_refused_command_is_recorded_as_denied(ui):
    """/sync is guild-only, so running it in a DM never reaches the command."""
    with captured_logs() as logs:
        await ui.drive("sync", dm=True)
    assert finished(logs, "sync")["outcome"] == DENIED
    refusal = logs.one("Command", "/sync refused")
    assert refusal["correlation_id"] == finished(logs, "sync")["correlation_id"]


async def test_a_refusal_does_not_quote_the_member_who_was_refused(ui):
    """Who it was is on the line as an id; their display name is not log material."""
    with captured_logs() as logs:
        pressed = await ui.drive("sync", guild=FakeGuild(me=None),
                                app_permissions=discord.Permissions.none())
    assert finished(logs, "sync")["outcome"] == DENIED
    assert pressed.user.display_name not in logs.raw


async def test_a_command_that_raises_is_recorded_as_an_error_and_still_raises(ui, store):
    """Swallowing it would leave the user on "the application did not respond"."""
    store.outage = StoreError("store said no")
    with captured_logs() as logs, pytest.raises(StoreError):
        await ui.drive("status")
    assert finished(logs, "status")["outcome"] == ERROR


async def test_a_cancelled_command_is_not_counted_as_a_failure(ui):
    """Shutdown cancelling a command in flight is not a defect to go looking for."""
    with captured_logs() as logs:
        with pytest.raises(asyncio.CancelledError):
            async with observed(FakeInteraction(), "sync"):
                raise asyncio.CancelledError
    assert finished(logs, "sync")["outcome"] == CANCELLED


async def test_an_outcome_marked_outside_a_command_is_dropped_rather_than_raising():
    """Command.guard marks a refusal, and is callable on its own in the unit tests."""
    mark(DENIED)


async def test_a_failure_before_the_callback_still_gets_an_outcome(ui):
    """discord.py raises on a bad option or a stale registration before the body.

    Nothing in the command was ever entered, so the only place that can close
    the access log is the failure path. Without this, a count of failures taken
    from the outcome field would miss exactly the failures a bad deploy causes.
    """
    pressed = FakeInteraction()
    pressed.command = SimpleNamespace(name="repped")
    with captured_logs() as logs:
        await report(pressed, app_commands.CommandSignatureMismatch(
            ui.bot.tree.get_command("repped")), subject="/repped")
    line = logs.one("Commands")
    assert line["outcome"] == ERROR
    assert line["command"] == "repped"
    # Never a latency: nothing timed it, and a zero would read as "instant".
    assert "latency_ms" not in line


async def test_a_failure_inside_a_command_is_not_counted_twice(ui, store):
    """The command's own block already closes it, and knows how long it took."""
    store.outage = StoreError("store said no")
    pressed = FakeInteraction()
    with captured_logs() as logs:
        try:
            await ui.run("status", interaction=pressed)
        except StoreError as error:
            await report(pressed, error, subject="/status")
    closing = [line for line in logs.lines("Commands") if "outcome" in line]
    assert len(closing) == 1, closing
    assert "latency_ms" in closing[0]


# --- everything one invocation emits ----------------------------------------

async def test_every_line_one_command_emits_carries_the_same_id(ui, store):
    """The whole point: separate modules' lines, reassembled into one invocation.

    Asserted over every captured line rather than over the ones that happen to
    have an id, because a line emitted during the invocation with no id is the
    failure this is looking for.
    """
    store.outage = StoreError("store said no")
    pressed = FakeInteraction()
    with captured_logs() as logs:
        try:
            await ui.run("status", interaction=pressed)
        except StoreError as error:
            # Exactly what CommandTree.on_error does with what a callback raised.
            await report(pressed, error, subject="/status")

    emitted = logs.lines()
    assert {line["logger"] for line in emitted} >= {"Commands", "CommandErrors"}
    assert [line for line in emitted if "correlation_id" not in line] == []
    assert {line["correlation_id"] for line in emitted} == {correlation_id(pressed)}


async def test_the_failure_path_recomputes_the_id_the_command_already_logged(ui):
    """on_error runs after observed() has exited, so it has no context to inherit."""
    pressed = FakeInteraction()
    with captured_logs() as logs:
        async with observed(pressed, "status"):
            pass
        await report(pressed, StoreError("store said no"), subject="/status")
    assert (logs.one("CommandErrors", "/status")["correlation_id"]
            == finished(logs, "status")["correlation_id"])


async def test_background_work_a_press_starts_inherits_its_correlation(ui, store):
    """A sync the setup prompt kicked off is part of the story of that press.

    asyncio copies the context when it creates a task, so spawn() needs no
    plumbing for this: the sync simply keeps what the press had bound. The
    press is driven in its own task because that is where discord.py runs it,
    and because that is what keeps its attached fields out of the rest of this
    test.
    """
    store.seed_guild(GUILD)
    press = FakeInteraction()
    sent = await ui.drive("setup")
    item = button(sent.view, "Sync now")

    async def pressed() -> None:
        # The order View._scheduled_task uses: check, then callback.
        assert await sent.view.interaction_check(press)
        await item.callback(press)

    with captured_logs() as logs:
        await asyncio.create_task(pressed())
        await asyncio.sleep(0)

    spawned = logs.lines("TagSyncService", "full_sync")
    assert spawned, "the press started no sync to correlate"
    for line in spawned:
        assert line["command"] == "setup"
        assert line["correlation_id"] == correlation_id(press)
        assert line["guild"] == GUILD


async def test_spawned_work_does_not_keep_the_id_of_whoever_started_it(ui, store):
    """A sync walks every member, so none of its lines is about one person.

    Inheriting the user would attribute thousands of "granted the role to
    <member>" lines to the admin who pressed the button, and make a filter on
    that field answer the wrong question.
    """
    store.seed_guild(GUILD)
    press = FakeInteraction()

    with captured_logs() as logs:
        view = (await ui.drive("setup")).view
        item = button(view, "Sync now")

        async def pressed() -> None:
            assert await view.interaction_check(press)
            await item.callback(press)

        await asyncio.create_task(pressed())
        await asyncio.sleep(0)

    spawned = logs.lines("TagSyncService", "full_sync")
    assert spawned, "the press started no sync to correlate"
    assert all("user" not in line for line in spawned), spawned
    # The press's own lines still name the user; only the spawned work does not.
    assert logs.one("Commands", "/setup finished")["user"] == str(press.user.id)


async def test_a_button_press_is_correlated_and_names_the_command_it_came_from(ui):
    """A component interaction carries no command, so the view has to supply it."""
    view = SyncPromptView(GUILD)
    pressed = FakeInteraction()
    # What discord.py really hands a component callback: the press knows which
    # message it came from, but not which command sent that message.
    pressed.command = None
    with captured_logs() as logs:
        assert await view.interaction_check(pressed)
        await report(pressed, StoreError("store said no"), subject="/setup (Sync now)")
    line = logs.one("CommandErrors")
    assert line["command"] == "setup"
    assert line["correlation_id"] == correlation_id(pressed)


# --- latency ----------------------------------------------------------------

async def test_latency_measures_the_time_the_command_actually_took(ui):
    class Slow(Command):
        help_info = CommandHelpInfo(name="slow", description="d", usage="u")

        def register(self, tree) -> None:  # pragma: no cover - never registered
            raise NotImplementedError

    with captured_logs() as logs:
        async with Slow().observed(FakeInteraction()):
            await asyncio.sleep(0.02)
    assert finished(logs, "slow")["latency_ms"] >= 15
