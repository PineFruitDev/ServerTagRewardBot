"""Single-line JSON logging with credential redaction at the output boundary."""

import copy
import json
import logging
import os
import re
import sys
from collections.abc import Mapping
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


def _redact(value: str) -> str:
    # Read at emission time so credentials loaded after logger creation are covered.
    secrets = sorted(
        (v for k, v in os.environ.items()
         if len(v) >= _MIN_REDACTABLE and _SECRET_KEY.search(k)),
        key=len, reverse=True,
    )
    for secret in secrets:
        value = value.replace(secret, "[REDACTED]")
    # URLs can contain passwords, signed query strings, or webhook path tokens.
    value = _URL.sub("[URL REDACTED]", value)
    return _CREDENTIAL.sub(r"\1[REDACTED]", value)


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
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": _redact(record.name),
            "message": _redact(record.getMessage()),
        }
        if record.exc_info and record.exc_info[0]:
            payload["exception_type"] = _redact(record.exc_info[0].__name__)
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
