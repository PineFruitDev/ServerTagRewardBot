import io
import json
import logging

import pytest

from src.services.logger import JsonFormatter, get_logger


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
