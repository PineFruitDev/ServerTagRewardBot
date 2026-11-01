"""One failure path for everything a slash command can raise.

Discord gives an interaction a few seconds to be answered, so an unhandled
exception costs more than a log line: the person who ran the command is left
looking at Discord's own "the application did not respond", with no idea
whether their action took effect.

The two interaction surfaces fail differently if left alone. An app command
callback reaches CommandTree.on_error, which this bot implements. A button on
that command's own reply reaches discord.ui.View.on_error instead, whose
default implementation logs the exception and returns, so the press is never
answered at all. Both surfaces go through report() so a failed button cannot
go quiet, and so the two cannot drift apart on what they tell people.

Nothing here puts an exception's message, repr or traceback into a reply. A
store URL, a bearer token quoted back inside an aiohttp error, or a Discord
response body would otherwise land in a channel the whole server can read.
The log line is narrowed the same way: JsonFormatter replaces exception
arguments with their type name and keeps no traceback, and describe() carries
only an HTTP status and Discord's own numeric code, so a single line survives
redaction while still saying enough to act on.
"""

import logging
from pathlib import Path
from typing import NamedTuple

import discord
from discord import app_commands

from src.services.discord_errors import describe, user_message
from src.services.logger import get_logger
from src.services.store_client import StoreError, StoreUnavailable

log = get_logger("CommandErrors")

#: Repository root, used to keep absolute host paths out of the logs.
_ROOT = Path(__file__).resolve().parents[2]

STORE_UNAVAILABLE_MESSAGE = (
    "⚠️ I can't reach my storage right now, so I couldn't do that. "
    "Nothing was lost; please try again in a minute."
)
STORE_REJECTED_MESSAGE = (
    "⚠️ My storage rejected that request. The bot owner has been notified in the logs."
)
STALE_COMMAND_MESSAGE = (
    "⏳ That command has changed since Discord last refreshed its list. "
    "Give it a minute, or restart Discord, then try again."
)
BAD_OPTION_MESSAGE = (
    "❌ I couldn't read one of the options you gave that command. "
    "Pick it from Discord's suggestions and try again."
)
UNEXPECTED_MESSAGE = "❌ An error occurred while executing this command."


class Failure(NamedTuple):
    """How one exception should be reported, in both directions."""

    #: Secret-free summary of what went wrong, for the log line.
    cause: str
    #: What to tell the person who ran the command, in terms they can act on.
    message: str
    #: How loudly to log it. A stale command list on someone's client is not an
    #: incident; a store outage or a bug in this bot is.
    level: int = logging.ERROR
    #: True when there is a defect here to fix, which is the only case where
    #: the raising location is worth recording.
    unexpected: bool = False


def unwrap(error: Exception) -> Exception:
    """The exception the command actually raised.

    discord.py wraps anything a callback raises in CommandInvokeError and hangs
    the cause off .original. Classifying the wrapper instead would make every
    store outage and permission problem look like an unexpected bug.
    """
    return getattr(error, "original", error)


def classify(error: Exception) -> Failure:
    """Decide what a failure means, for the log and for the user."""
    if isinstance(error, StoreUnavailable):
        return Failure("could not reach the store", STORE_UNAVAILABLE_MESSAGE)

    if isinstance(error, StoreError):
        return Failure("was rejected by the store", STORE_REJECTED_MESSAGE)

    if isinstance(error, discord.HTTPException):
        # The status is what separates a fixable setup problem (the reward role
        # sitting above the bot's own) from an outage or a rate limit, and those
        # want opposite responses, so neither the log line nor the reply
        # flattens them into "an API error".
        return Failure(f"failed against the Discord API: {describe(error)}", user_message(error))

    if isinstance(error, app_commands.CommandNotFound | app_commands.CommandSignatureMismatch):
        # Someone's client is offering a command this build no longer defines
        # the same way. Nothing is broken and nothing needs fixing here; they
        # are working from a stale list.
        return Failure("was invoked from a stale command list", STALE_COMMAND_MESSAGE,
                       level=logging.WARNING)

    if isinstance(error, app_commands.TransformerError):
        # An option Discord sent could not be resolved into what the command
        # asked for, usually a member or role that is no longer visible.
        return Failure("was given an option it could not resolve", BAD_OPTION_MESSAGE,
                       level=logging.WARNING)

    return Failure("raised an unexpected error", UNEXPECTED_MESSAGE, unexpected=True)


def origin(error: Exception) -> str:
    """Where an error was raised, formatted as " at file:line in function".

    Empty when the exception carries no traceback, which is the case for one
    that was constructed but never raised.

    JsonFormatter deliberately keeps no traceback, exception message or source
    line, so an unexpected error would otherwise be logged as a bare type name,
    which is not enough to find the defect. A path and a line number carry no
    credentials, unlike the source line and the frame's locals, which are still
    never recorded.
    """
    raised_at = error.__traceback__
    if raised_at is None:
        return ""
    while raised_at.tb_next is not None:
        raised_at = raised_at.tb_next
    code = raised_at.tb_frame.f_code
    path = Path(code.co_filename)
    try:
        where: Path | str = path.resolve().relative_to(_ROOT)
    except ValueError:
        # Raised inside a dependency or the stdlib, where the absolute path
        # would only expose the host's filesystem layout.
        where = path.name
    return f" at {where}:{raised_at.tb_lineno} in {code.co_name}"


async def tell(interaction: discord.Interaction, message: str) -> bool:
    """Deliver message ephemerally, whatever state the interaction is in.

    Ephemeral because a failure is addressed to the one person who triggered it
    and says nothing the rest of the channel needs.

    An interaction that was already answered, or deferred, has to be followed
    up rather than responded to. Either call can still fail: the token expires,
    the channel can be gone, and is_done() can disagree with Discord about
    whether a response landed. This is the last step of the failure path and
    its caller is discord.py's own dispatcher, so it reports rather than
    raises, returning False when the user could not be reached.
    """
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except (discord.DiscordException, OSError):
        # DiscordException covers both an HTTP refusal and InteractionResponded,
        # which is a ClientException and would escape a narrower catch. OSError
        # covers failing to reach Discord at all: aiohttp's connection errors
        # and TimeoutError are both OSError subclasses.
        return False
    return True


async def report(interaction: discord.Interaction, error: Exception, *, subject: str) -> None:
    """Log one line for a failure and tell the user, in that order.

    The log comes first and unconditionally. Replying needs the network and an
    interaction token that may already have expired, and a failure there must
    not cost the only record of what went wrong.
    """
    original = unwrap(error)
    failure = classify(original)
    log.log(failure.level, "report - %s %s%s: %s", subject, failure.cause,
            origin(original) if failure.unexpected else "", original)
    if not await tell(interaction, failure.message):
        # Worth its own line: an operator otherwise cannot tell a failure that
        # was handled from one the user never heard about.
        log.warning("report - %s failed and could not be reported to the user", subject)
