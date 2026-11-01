import io
import json
import logging

import pytest

from src.services.logger import JsonFormatter, configure_library_logging, get_logger


@pytest.mark.parametrize("context", ["Bot", "ScheduledJob", "StoreClient"])
def test_json_failure_redacts_credentials(monkeypatch, context):
    monkeypatch.setenv("STORE_TOKEN", "synthetic-sensitive-value")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.Logger(context)
    logger.addHandler(handler)
    try:
        raise RuntimeError("private exception payload")
    except RuntimeError:
        logger.exception(
            "failed %s password=hunter-test https://host/path?signature=private\nnext",
            "synthetic-sensitive-value",
            extra={"credentials": "private extra"},
        )
    raw = stream.getvalue()
    assert len(raw.splitlines()) == 1
    entry = json.loads(raw)
    assert entry["logger"] == context
    assert entry["level"] == "ERROR"
    assert entry["exception_type"] == "RuntimeError"
    for secret in ["synthetic-sensitive-value", "hunter-test", "signature", "private"]:
        assert secret not in raw
    assert "[REDACTED]" in entry["message"]


def test_header_and_json_secret_redaction():
    record = logging.LogRecord("test", logging.WARNING, "", 0,
                               'Authorization: Bearer test-auth {"token": "test-token"}', (), None)
    output = JsonFormatter().format(record)
    assert "test-auth" not in output
    assert "test-token" not in output


@pytest.mark.parametrize("environment,level", [("development", logging.DEBUG), ("production", logging.INFO)])
def test_logger_setup(monkeypatch, environment, level):
    monkeypatch.setenv("ENVIRONMENT", environment)
    name = "test-json-" + environment
    logger = get_logger(name)
    assert get_logger(name) is logger
    assert len(logger.handlers) == 1
    assert isinstance(logger.handlers[0].formatter, JsonFormatter)
    assert logger.level == level
    assert not logger.propagate
    logger.handlers.clear()


def test_exception_argument_does_not_disclose_response_body():
    record = logging.LogRecord("StoreClient", logging.ERROR, "", 0,
                               "request failed (%s)", (RuntimeError("unlabelled-private-body"),), None)
    output = JsonFormatter().format(record)
    assert "unlabelled-private-body" not in output
    assert "RuntimeError" in output
    assert isinstance(record.args[0], RuntimeError)


@pytest.fixture
def library_loggers():
    """Restore the dependency loggers: they are global process state."""
    names = ("discord", "aiohttp")
    saved = [(logging.getLogger(n), logging.getLogger(n).handlers[:],
              logging.getLogger(n).level, logging.getLogger(n).propagate) for n in names]
    yield names
    for logger, handlers, level, propagate in saved:
        logger.handlers[:] = handlers
        logger.setLevel(level)
        logger.propagate = propagate


def test_library_loggers_get_the_redacting_json_handler(library_loggers):
    """Without this, discord.py's own records bypass the formatter entirely."""
    configure_library_logging()
    for name in library_loggers:
        logger = logging.getLogger(name)
        assert any(isinstance(h.formatter, JsonFormatter) for h in logger.handlers)
        assert logger.level == logging.WARNING
        assert not logger.propagate


def test_configuring_library_logging_twice_does_not_duplicate_handlers(library_loggers):
    configure_library_logging()
    counts = [len(logging.getLogger(n).handlers) for n in library_loggers]
    configure_library_logging()
    assert [len(logging.getLogger(n).handlers) for n in library_loggers] == counts


def test_a_library_rate_limit_warning_is_structured_and_redacted(library_loggers, monkeypatch):
    """The exact record discord.py emits when Discord rate limits the bot."""
    monkeypatch.setenv("DISCORD_TOKEN", "synthetic-bot-token-value")
    configure_library_logging()
    logger = logging.getLogger("discord.http")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logging.getLogger("discord").handlers[:] = [handler]

    logger.warning(
        "We are being rate limited. %s %s responded with 429. Retrying in %.2f seconds.",
        "PUT",
        "https://discord.com/api/v10/guilds/1/members/2/roles/3?token=synthetic-bot-token-value",
        4.5,
    )

    raw = stream.getvalue()
    assert len(raw.splitlines()) == 1
    entry = json.loads(raw)
    assert entry["level"] == "WARNING"
    # The operator still learns it is a 429 and how long the wait is...
    assert "429" in entry["message"] and "4.50 seconds" in entry["message"]
    # ...without the URL, which is where a library is most likely to leak one.
    assert "synthetic-bot-token-value" not in raw
    assert "discord.com" not in raw


def test_library_logs_below_the_level_are_dropped(library_loggers):
    """discord.py's DEBUG gateway chatter must not drown the application logs."""
    configure_library_logging()
    assert not logging.getLogger("discord").isEnabledFor(logging.DEBUG)
    assert logging.getLogger("discord").isEnabledFor(logging.WARNING)
