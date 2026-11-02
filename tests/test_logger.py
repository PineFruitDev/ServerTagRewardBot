import asyncio
import io
import json
import logging
from collections import UserDict
from types import MappingProxyType

import pytest

from src.services.logger import (FIELDS, RESERVED, JsonFormatter, attach, bound,
                                 configure_library_logging, context, get_logger,
                                 without_user)


def record(message: str = "ran", level: int = logging.INFO, **fields) -> logging.LogRecord:
    """A record carrying structured fields, the way `extra=` would set them."""
    built = logging.LogRecord("test", level, "", 0, message, (), None)
    for name, value in fields.items():
        setattr(built, name, value)
    return built


@pytest.mark.parametrize("logger_name", ["Bot", "ScheduledJob", "StoreClient"])
def test_json_failure_redacts_credentials(monkeypatch, logger_name):
    monkeypatch.setenv("STORE_TOKEN", "synthetic-sensitive-value")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.Logger(logger_name)
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
    assert entry["logger"] == logger_name
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


@pytest.mark.parametrize("name", ["TAGBOT_STORE_KEY", "DISCORD_TOKEN", "SOME_API_KEY", "KEY"])
def test_credential_variables_are_redacted_by_name(monkeypatch, name):
    """The store's bearer secret lives in TAGBOT_STORE_KEY, which "api.?key" missed."""
    monkeypatch.setenv(name, "synthetic-credential-value")
    record = logging.LogRecord("test", logging.ERROR, "", 0,
                               "store rejected synthetic-credential-value", (), None)
    assert "synthetic-credential-value" not in JsonFormatter().format(record)


def test_an_ordinary_setting_is_not_redacted(monkeypatch):
    """Over-redaction would hide the values an operator needs to debug with."""
    monkeypatch.setenv("SYNC_DELAY_MS", "350")
    record = logging.LogRecord("test", logging.INFO, "", 0, "delay is 350ms", (), None)
    assert "350" in JsonFormatter().format(record)


def test_a_tiny_credential_value_does_not_rewrite_every_line(monkeypatch):
    """SORT_KEY=id must not turn every "id" in the logs into [REDACTED]."""
    monkeypatch.setenv("SORT_KEY", "id")
    record = logging.LogRecord("test", logging.INFO, "", 0,
                               "granted role to member 1234 (valid id)", (), None)
    assert "valid id" in JsonFormatter().format(record)


def test_a_short_value_is_still_redacted_when_labelled(monkeypatch):
    """The substring guard does not weaken the "token=x" pattern."""
    monkeypatch.setenv("API_KEY", "abc")
    record = logging.LogRecord("test", logging.ERROR, "", 0, "sent token=abc", (), None)
    assert "abc" not in JsonFormatter().format(record)


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


@pytest.mark.parametrize("wrap", [dict, MappingProxyType, UserDict], ids=["dict", "proxy", "userdict"])
def test_mapping_arguments_are_scrubbed_like_positional_ones(wrap):
    """logging turns a lone mapping into record.args, bypassing the tuple path.

    No call site formats this way today, but the formatter's promise not to
    serialize an exception message cannot depend on which %-style the caller
    picked, or the first `log.error("%(exc)s", {...})` quietly breaks it. Any
    Mapping counts, not just dict, for the same reason.
    """
    args = wrap({"exc": RuntimeError("unlabelled-private-body")})
    record = logging.LogRecord("StoreClient", logging.ERROR, "", 0,
                               "request failed (%(exc)s)", (args,), None)
    assert record.args is args, "logging no longer unwraps a lone mapping"

    output = JsonFormatter().format(record)

    assert "unlabelled-private-body" not in output
    assert "RuntimeError" in output
    # The caller's own mapping must not be mutated; it may be reused or inspected.
    assert isinstance(args["exc"], RuntimeError)


def test_a_message_with_no_arguments_formats_cleanly():
    """record.args is None for log.info("..."), the most common call shape."""
    record = logging.LogRecord("Main", logging.INFO, "", 0, "Shutdown complete", None, None)

    entry = json.loads(JsonFormatter().format(record))

    assert entry["message"] == "Shutdown complete"
    assert entry["level"] == "INFO"
# --- structured fields ------------------------------------------------------

def test_an_unlisted_extra_is_never_serialized():
    """The allowlist is the defence; `extra=` is how a response body gets in."""
    entry = json.loads(JsonFormatter().format(
        record("saved", command="setup", response_body="private-response-body")))
    assert entry["command"] == "setup"
    assert "response_body" not in entry
    assert "private-response-body" not in json.dumps(entry)


def test_no_field_can_shadow_a_key_the_formatter_writes():
    """A field named "message" would overwrite the line it was meant to annotate."""
    assert not set(FIELDS) & set(RESERVED)


def test_a_field_value_is_redacted_like_a_message(monkeypatch):
    """A field is not a trusted channel: the same redaction runs over it."""
    monkeypatch.setenv("DISCORD_TOKEN", "synthetic-token-in-a-field")
    output = JsonFormatter().format(record("ran", command="help synthetic-token-in-a-field"))
    assert "synthetic-token-in-a-field" not in output
    assert "[REDACTED]" in json.loads(output)["command"]


@pytest.mark.parametrize("latency", [42, 12.5])
def test_a_latency_stays_a_number(latency):
    """Stringifying it would make every downstream aggregation parse it back."""
    assert json.loads(JsonFormatter().format(record(latency_ms=latency)))["latency_ms"] == latency


def test_a_field_value_that_is_neither_text_nor_a_number_is_stringified():
    """json.dumps must not be the step that fails and costs the whole line."""
    class Snowflake:
        def __str__(self) -> str:
            return "900000000000000001"

    assert json.loads(JsonFormatter().format(
        record(guild=Snowflake())))["guild"] == "900000000000000001"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_a_number_json_cannot_represent_is_quoted_rather_than_emitted_bare(value):
    """json.dumps writes NaN and Infinity bare, and neither is valid JSON.

    One such value would make the whole line unparseable for every reader
    downstream, which is a worse outcome than an ugly string.
    """
    entry = json.loads(JsonFormatter().format(record(latency_ms=value)))
    assert isinstance(entry["latency_ms"], str)


def test_a_boolean_is_not_treated_as_a_measurement():
    """bool is an int in Python, but `"outcome": true` is not a reading of anything."""
    assert json.loads(JsonFormatter().format(record(outcome=True)))["outcome"] == "True"


# --- dropping the user for detached work ------------------------------------

def test_without_user_keeps_everything_except_the_user():
    with bound(correlation_id="abc123", command="setup", guild="900", user="700"):
        with without_user():
            held = dict(context())
        assert dict(context())["user"] == "700"
    assert held == {"correlation_id": "abc123", "command": "setup", "guild": "900"}


def test_without_user_is_harmless_when_no_user_is_bound():
    with bound(correlation_id="abc123"), without_user():
        assert dict(context()) == {"correlation_id": "abc123"}


def test_a_field_left_unset_is_absent_rather_than_null():
    """A null for every field the line does not have is noise in every line."""
    entry = json.loads(JsonFormatter().format(record(command="ping")))
    assert set(entry) == {"timestamp", "level", "logger", "message", "command"}


# --- ambient context --------------------------------------------------------

def test_bound_fields_land_on_a_line_that_knows_nothing_about_them():
    """The point of binding: StoreClient logs a retry and it is still correlated."""
    with bound(correlation_id="abc123", command="sync", guild="900", user="700"):
        entry = json.loads(JsonFormatter().format(record("retry 1/3")))
    assert entry["correlation_id"] == "abc123"
    assert (entry["command"], entry["guild"], entry["user"]) == ("sync", "900", "700")


def test_an_inner_scope_adds_to_the_outer_one_instead_of_replacing_it():
    with bound(correlation_id="abc123", guild="900"):
        with bound(command="disable"):
            entry = json.loads(JsonFormatter().format(record()))
    assert entry["correlation_id"] == "abc123"
    assert entry["command"] == "disable"


def test_a_field_offered_as_none_does_not_erase_what_is_already_bound():
    """A caller that knows only some of the fields must not clear the rest."""
    with bound(correlation_id="abc123", command="setup"):
        with bound(command=None):
            assert dict(context()) == {"correlation_id": "abc123", "command": "setup"}


def test_a_record_extra_wins_over_the_same_field_in_the_context():
    with bound(command="sync"):
        entry = json.loads(JsonFormatter().format(record(command="disable")))
    assert entry["command"] == "disable"


def test_the_context_is_gone_after_the_block():
    with bound(correlation_id="abc123"):
        pass
    assert dict(context()) == {}


def test_the_context_survives_an_exception_inside_the_block():
    with pytest.raises(RuntimeError), bound(correlation_id="abc123"):
        raise RuntimeError("boom")
    assert dict(context()) == {}


async def test_an_attached_field_cannot_outlive_the_task_that_set_it():
    """What makes attach() safe on a surface with no block to close."""
    async def press() -> dict:
        attach(correlation_id="abc123", command="setup")
        return dict(context())

    assert (await asyncio.create_task(press()))["correlation_id"] == "abc123"
    assert dict(context()) == {}


async def test_a_bound_field_is_inherited_by_work_spawned_inside_the_block():
    """A sync a command kicked off is findable by that command's own id."""
    async def background() -> dict:
        await asyncio.sleep(0)
        return dict(context())

    with bound(correlation_id="abc123", command="setup"):
        task = asyncio.create_task(background())
    assert (await task)["correlation_id"] == "abc123"


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
