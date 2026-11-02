"""Single-line JSON logging with credential redaction at the output boundary.

Two things happen here. Every line is serialized as one JSON object with a
fixed shape, so a log search can filter on a field instead of pattern-matching
prose. And every string that goes into that object is run through _redact
first, because the alternative is trusting that no caller anywhere ever
formats a bearer token, a signed URL or a response body into a message.

Beyond the four keys every line carries, a line can carry structured fields
(see FIELDS): the id correlating one interaction's lines, which command was
running, in which guild, for which user, and how it ended. Those come from two
places, an ambient per-task context set by bound()/attach() and the record's
own extras, and they are merged by JsonFormatter at emission time.
"""

import copy
import json
import logging
import math
import os
import re
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone

# Matched against environment variable *names* to decide whose values must
# never appear in output. "api.?key" alone missed TAGBOT_STORE_KEY, the store's
# bearer secret, so any name ending in _KEY counts too.
_SECRET_KEY = re.compile(r"token|secret|password|credential|api.?key|_key$|^key$", re.I)

# Values below this are not redacted by substring match. A credential-named
# variable holding something tiny (SORT_KEY=id, TOKEN=1) would otherwise rewrite
# every log line containing those characters, which corrupts the output it is
# meant to protect. Real credentials are far longer than this, and the
# _CREDENTIAL pattern below still covers a short value written as "token=x".
_MIN_REDACTABLE = 8
_CREDENTIAL = re.compile(
    r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?(?:bearer\s+|bot\s+)?"
    r"|(?:token|secret|password|api[_-]?key)[\"']?\s*[:=]\s*[\"']?)"
    r"[^\s,;\"'}&]+"
)
_URL = re.compile(r"https?://[^\s<>\"']+", re.I)


def _as_type_name(arg: object) -> object:
    """Replace an exception with its class name, leaving anything else alone.

    An exception's str() is whatever the library put in it: a response body, a
    rejected URL, a credential. The type is the part worth logging.
    """
    return type(arg).__name__ if isinstance(arg, BaseException) else arg


def _env_secrets() -> tuple[str, ...]:
    """Credential-named environment values, longest first.

    Read at emission time so credentials loaded after logger creation are
    covered, and ordered longest first so a value that contains a shorter one
    is replaced whole instead of being left with a [REDACTED] fragment inside
    it. Computed once per record and passed down, rather than once per string,
    because a record now carries several fields as well as its message.
    """
    return tuple(sorted(
        (v for k, v in os.environ.items()
         if len(v) >= _MIN_REDACTABLE and _SECRET_KEY.search(k)),
        key=len, reverse=True,
    ))


def _redact(value: str, secrets: tuple[str, ...]) -> str:
    """Strip credentials from one string. `secrets` comes from _env_secrets()."""
    for secret in secrets:
        value = value.replace(secret, "[REDACTED]")
    # URLs can contain passwords, signed query strings, or webhook path tokens.
    value = _URL.sub("[URL REDACTED]", value)
    return _CREDENTIAL.sub(r"\1[REDACTED]", value)


#: Structured fields a line may carry beside its message, written as flat JSON
#: keys so a log search can select on one (`.command == "sync"`) rather than
#: parse prose.
#:
#: This is an allowlist, and that is the point. Serializing whatever a caller
#: hung off `extra=` is exactly how a response body, a request header or a
#: bearer token reaches the logs, so anything not named here is dropped. Adding
#: a name is a decision that its values are safe to keep, and every value still
#: goes through _redact on the way out.
#:
#: The ids are Discord snowflakes, which are public: a guild id is in every
#: invite and a user id is in every mention. Display names are deliberately
#: absent. They are user-controlled content, they identify a person far more
#: than their id does, and they are the obvious way to get arbitrary text into
#: whatever reads these logs.
FIELDS = ("correlation_id", "command", "guild", "user", "outcome", "latency_ms")

#: Keys JsonFormatter writes itself. No field may shadow one; test_logger holds
#: FIELDS to that, so a new field cannot quietly overwrite the message.
RESERVED = ("timestamp", "level", "logger", "message", "exception_type")

_CONTEXT: ContextVar[Mapping[str, str] | None] = ContextVar("log_context", default=None)


def context() -> Mapping[str, str]:
    """The fields every line emitted on this task currently carries."""
    return _CONTEXT.get() or {}


def _merged(correlation_id: str | None, command: str | None,
            guild: str | None, user: str | None) -> dict[str, str]:
    """The current context with the given fields laid over it.

    Merged rather than replaced so an inner scope can name the command without
    discarding the correlation id an outer one set, and a None is dropped
    rather than recorded as a field with no value, so a caller that knows only
    some of the fields does not erase the rest.
    """
    offered = {"correlation_id": correlation_id, "command": command,
               "guild": guild, "user": user}
    return {**context(), **{k: v for k, v in offered.items() if v is not None}}


@contextmanager
def bound(*, correlation_id: str | None = None, command: str | None = None,
          guild: str | None = None, user: str | None = None) -> Iterator[None]:
    """Attach these fields to every line emitted inside the block.

    Named keyword arguments rather than **fields so that mypy rejects a
    misspelled field name at typecheck time instead of it being silently
    dropped from the output of a path nobody has run yet.

    asyncio copies the current context when it creates a task, so background
    work spawned inside the block keeps these fields after the block exits.
    That is deliberate: a sync that a command kicked off is part of that
    command's story and should be findable by the same correlation id.
    """
    token = _CONTEXT.set(_merged(correlation_id, command, guild, user))
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def attach(*, correlation_id: str | None = None, command: str | None = None,
           guild: str | None = None, user: str | None = None) -> None:
    """As bound(), for a surface that offers no place to close a block.

    discord.ui.View runs each component press in its own task and has no
    after-callback hook, so there is nothing to pair a `with` with. Setting a
    context variable inside a task cannot be seen from outside it, so fields
    attached during one press are discarded with that press rather than
    leaking into the next.
    """
    _CONTEXT.set(_merged(correlation_id, command, guild, user))


@contextmanager
def without_user() -> Iterator[None]:
    """Drop the user field for the duration of the block, keeping the rest.

    For work that outlives the interaction that started it. A full sync walks
    every member of a guild, so if it inherited the id of the admin who asked
    for it, every "granted the role to <member>" line would be attributed to
    one person who appears in none of them, and a filter on that field would
    answer the wrong question. The correlation id and the command still say
    where the work came from, which is what makes it findable; the guild is
    kept because spawned work never crosses into another one.
    """
    token = _CONTEXT.set({name: value for name, value in context().items() if name != "user"})
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def _numeric(value: object) -> bool:
    """Whether a field value may be written as a JSON number.

    A real number (a latency, a count) cannot carry a credential and is more
    useful left as one than quoted. Everything else is stringified and
    redacted, which covers two cases that would otherwise cost the whole line:
    an object whose type json.dumps does not know, and a nan or an inf, which
    json.dumps happily writes as bare NaN and Infinity, neither of which is
    valid JSON and neither of which a strict log reader will accept. A bool is
    an int in Python but is not a measurement, so it is stringified too.
    """
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _fields(record: logging.LogRecord) -> dict[str, object]:
    """The allowlisted fields this record carries, from its own extras.

    logging rejects an `extra=` key that collides with one of its own record
    attributes, and none of FIELDS does, so a field read here can only have
    come from a caller that meant to set it.
    """
    present: dict[str, object] = {}
    for name in FIELDS:
        value = getattr(record, name, None)
        if value is not None:
            present[name] = value
    return present


class JsonFormatter(logging.Formatter):
    """Never serialize arbitrary extras, exception messages, locals or source lines."""

    def format(self, record: logging.LogRecord) -> str:
        record = copy.copy(record)
        if isinstance(record.args, tuple):
            record.args = tuple(_as_type_name(arg) for arg in record.args)
        elif isinstance(record.args, Mapping):
            # logging unwraps a lone mapping argument, so %(name)s formatting
            # arrives as a mapping rather than a tuple. It carries exceptions
            # just as easily, so it has to be scrubbed the same way. Mapping,
            # not dict: logging accepts any of them, and MappingProxyType or a
            # UserDict would otherwise slip through unscrubbed.
            record.args = {key: _as_type_name(value) for key, value in record.args.items()}
        secrets = _env_secrets()
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": _redact(record.name, secrets),
            "message": _redact(record.getMessage(), secrets),
        }
        if record.exc_info and record.exc_info[0]:
            payload["exception_type"] = _redact(record.exc_info[0].__name__, secrets)
        for name, value in {**context(), **_fields(record)}.items():
            payload[name] = value if _numeric(value) else _redact(str(value), secrets)
        return json.dumps(payload, ensure_ascii=True)


def _json_handler() -> logging.StreamHandler:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    return handler


def get_logger(context: str) -> logging.Logger:
    """Return one non-propagating application logger with a JSON stdout handler."""
    logger = logging.getLogger(context)
    if not logger.handlers:
        logger.addHandler(_json_handler())
        level = logging.DEBUG if os.getenv("ENVIRONMENT") == "development" else logging.INFO
        logger.setLevel(level)
        logger.propagate = False
    return logger


#: Loggers owned by dependencies rather than by this bot. discord.py reports
#: rate limits, gateway resumes and connection failures on these.
_LIBRARY_LOGGERS = ("discord", "aiohttp")


def configure_library_logging(level: int = logging.WARNING) -> None:
    """Route dependency logs through the same redacting JSON handler.

    get_logger only covers loggers this bot creates. Left alone, discord.py's
    own records (including "we are being rate limited, retrying in N seconds")
    reach logging's last-resort handler instead: unstructured, on stderr, and
    never passed through _redact. Rate limiting and gateway trouble are exactly
    what an operator needs to see, and a dependency is also the most likely
    source of a line carrying a credential-bearing URL, so these loggers get
    the application handler rather than being left to print what they like.

    Safe to call more than once: a logger already holding a JSON handler keeps
    the one it has.
    """
    for name in _LIBRARY_LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(h.formatter, JsonFormatter) for h in logger.handlers):
            logger.addHandler(_json_handler())
        logger.setLevel(level)
        logger.propagate = False
