"""One correlation id per interaction, and one outcome line per command.

A single `/sync` can produce log lines from five modules: the command, Command
itself when it refuses the invocation, GuildConfig, StoreClient (which may
retry a few times before it gives up) and command_errors when something
raises. Read back as a flat stream carrying every guild at once, those lines
cannot be reassembled into "what happened when someone ran /sync", which is
the only question an operator ever actually has.

So everything emitted while handling one interaction carries the same
correlation id, and every invocation closes with exactly one line naming the
command, the guild, the user and how it ended. That line comes from observed()
and carries a latency; the one case it cannot cover is a failure raised before
any command body existed, which record_untimed_failure() closes out instead.
The fields are attached ambiently rather than passed down, because the modules
that need correlating are the deep ones: threading a request id through
StoreClient.request would be the same work and would only cover the call sites
that remembered.

The id comes from Discord's own interaction id rather than from a fresh random
value, so the failure path can recompute it after the command's context has
been torn down and still agree with the lines the command already emitted.
"""

import asyncio
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

import discord

from src.services.logger import attach, bound, context, get_logger

log = get_logger("Commands")

#: Ran and answered the user.
OK = "ok"
#: Refused before running: the wrong channel, missing bot permissions, or not a
#: developer. Expected, and the user was told why.
DENIED = "denied"
#: Raised. command_errors.report has already logged what and told the user.
ERROR = "error"
#: Still running when shutdown cancelled it, which is not a defect and should
#: not be counted as one.
CANCELLED = "cancelled"


@dataclass
class CommandRun:
    """How one invocation ended, filled in while it is still running."""

    outcome: str = OK


_RUN: ContextVar[CommandRun | None] = ContextVar("command_run", default=None)

#: The correlation id of the interaction observed() has timed on this task, set
#: and deliberately never reset: discord.py calls on_error after the callback
#: has returned, so by then observed() has already closed its own scope and
#: there would be nothing left to tell the failure path that the invocation was
#: accounted for. Each app command invocation and each component press gets its
#: own asyncio task (CommandTree._from_interaction and View._dispatch_item both
#: create one), and a context variable set inside a task cannot be seen from
#: outside it, so a value left here cannot reach an unrelated interaction.
_TIMED: ContextVar[str | None] = ContextVar("timed_interaction", default=None)


def correlation_id(interaction: discord.Interaction) -> str:
    """A short id for one interaction, identical every time it is derived.

    Discord's interaction id is unique per interaction and is already the key
    Discord itself would use to talk about this request, so this needs neither
    a registry nor randomness: command_errors.report can recompute the id on
    the failure path, after the command's own context has been reset, and get
    the one that is already on the lines the command emitted.

    Hex only to keep the field short enough to skim. An interaction carrying no
    id (one constructed directly, as the unit tests do) falls back to its
    object identity, which is still unique among the interactions alive at the
    time.
    """
    raw = getattr(interaction, "id", None)
    return format(raw if isinstance(raw, int) else id(interaction), "x")


def _snowflake(value: object) -> str | None:
    """A Discord id as a string, or None when there is nothing to record."""
    return None if value is None else str(value)


def _identity(interaction: discord.Interaction) -> tuple[str, str | None, str | None]:
    """The correlation id, guild and user for one interaction.

    Every read is defensive. Correlating a command must never be the reason
    that command fails, and this runs on the failure path too, where an
    interaction can be missing whatever the failure was about.
    """
    return (correlation_id(interaction),
            _snowflake(getattr(interaction, "guild_id", None)),
            _snowflake(getattr(getattr(interaction, "user", None), "id", None)))


@contextmanager
def correlated(interaction: discord.Interaction, command: str | None = None) -> Iterator[None]:
    """Tag every line emitted inside the block with this interaction's fields."""
    identifier, guild, user = _identity(interaction)
    with bound(correlation_id=identifier, command=command, guild=guild, user=user):
        yield


def correlate(interaction: discord.Interaction, command: str | None = None) -> None:
    """As correlated(), where there is no block to close. See logger.attach."""
    identifier, guild, user = _identity(interaction)
    attach(correlation_id=identifier, command=command, guild=guild, user=user)


def mark(outcome: str) -> None:
    """Record how the invocation in progress ended.

    A no-op outside observed(), so that Command.guard can set an outcome
    without having to be handed the run, and still be callable on its own.
    """
    run = _RUN.get()
    if run is not None:
        run.outcome = outcome


def record_untimed_failure(interaction: discord.Interaction) -> None:
    """Close the access log for a failure no observed() block ever saw.

    Two real cases reach command_errors.report without a command body having
    run. discord.py raises before the callback exists when an option cannot be
    transformed into what the command asked for, or when someone's client is
    working from a stale registration (both are already classified in
    command_errors); and a component press has no command callback to wrap at
    all. Left alone, those failures would produce a line saying what went
    wrong but no line carrying an outcome, so a failure count taken from the
    outcome field would quietly be short by exactly the kind of failure a bad
    deployment causes.

    There is no latency on this line. Nothing started a clock, and a zero
    would read as a command that was instant rather than one that never ran.

    Which command it was comes from the ambient context rather than from the
    interaction, because on the press path the only thing that knows is the
    view, which bound it on the way in.
    """
    if _TIMED.get() == correlation_id(interaction):
        # observed() already timed this invocation and closed its own line.
        # Emitting here as well would count one failure twice.
        return
    log.info("record_untimed_failure - /%s failed before it could be timed",
             context().get("command", "?"), extra={"outcome": ERROR})


@asynccontextmanager
async def observed(interaction: discord.Interaction, command: str) -> AsyncIterator[CommandRun]:
    """Correlate everything one invocation emits, then log how it ended.

    The command name is passed in rather than read off the interaction because
    one Command object can register more than one slash command (SetupCommand
    registers both /setup and /disable), and because discord.py leaves
    Interaction.command set to None for a component press.

    The closing line is always INFO. It is an access-log line, one per
    invocation that got this far, and whatever actually went wrong has already
    been logged at its own level by Command.guard or command_errors.report;
    promoting this one as well would double-count every failure. An exception
    is timed, recorded and re-raised untouched, because what answers the user
    is discord.py's dispatcher calling on_error, and swallowing it here would
    leave the person who ran the command looking at "the application did not
    respond".
    """
    run = CommandRun()
    # Outside the correlated() scope on purpose: this has to still be readable
    # once the block has closed, because that is when on_error runs.
    _TIMED.set(correlation_id(interaction))
    with correlated(interaction, command):
        token = _RUN.set(run)
        started = time.perf_counter()
        log.debug("observed - /%s started", command)
        try:
            yield run
        except asyncio.CancelledError:
            run.outcome = CANCELLED
            raise
        except BaseException:
            run.outcome = ERROR
            raise
        finally:
            _RUN.reset(token)
            log.info("observed - /%s finished", command, extra={
                "outcome": run.outcome,
                "latency_ms": round((time.perf_counter() - started) * 1000),
            })
