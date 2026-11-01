"""Single-line JSON logging with credential redaction at the output boundary."""

import copy
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone

_SECRET_KEY = re.compile(r"token|secret|password|credential|api.?key", re.I)
_CREDENTIAL = re.compile(
    r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?(?:bearer\s+|bot\s+)?"
    r"|(?:token|secret|password|api[_-]?key)[\"']?\s*[:=]\s*[\"']?)"
    r"[^\s,;\"'}&]+"
)
_URL = re.compile(r"https?://[^\s<>\"']+", re.I)


def _redact(value: str) -> str:
    # Read at emission time so credentials loaded after logger creation are covered.
    secrets = sorted(
        (v for k, v in os.environ.items() if v and _SECRET_KEY.search(k)),
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
            record.args = tuple(
                type(arg).__name__ if isinstance(arg, BaseException) else arg
                for arg in record.args
            )
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
