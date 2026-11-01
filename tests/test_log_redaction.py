"""Proof that the bot's two credentials never reach the logs.

test_logger.py checks the redaction rules one at a time against hand-built
records. This file does the opposite: it drives the real commands, the real
failure path, the real store client and startup validation with both
credentials set in the environment, captures everything the bot's own handlers
write, and asserts the credentials are not in it.

Two independent rules keep them out, and the distinction matters for reading
these tests. JsonFormatter reduces an exception argument to its type name, so
a store error quoting the header it was rejected for never reaches the message
at all; redaction then catches whatever a call site formats in as a string.
The first rule is why most of the tests below would still pass with redaction
disabled: they are regression tests, and they fail the day a new call site
starts formatting a credential into a line. The tests that exercise redaction
itself are named for it, and
test_a_credential_in_an_exception_is_stopped_twice_over pins both rules at
once so neither can rot unnoticed behind the other.

The check is deliberately not `TOKEN not in output`. Redaction replaces whole
values, so a call site that logged a masked prefix ("token: synthetic.bo...")
would satisfy that assertion while still handing out enough of a credential to
correlate a leak. So every window of _WINDOW consecutive characters is
searched for instead, which fails on a fragment as well as on the whole thing.
"""

import logging
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

import main  # noqa: F401  (imported for its side effect: every logger exists)
from src.services.command_errors import report
from src.services.environment import Environment
from src.services.logger import configure_library_logging, get_logger
from src.services.store_client import StoreClient, StoreError, StoreUnavailable
from tests.integration_harness import COMMAND_ARGUMENTS, FakeInteraction, captured_logs

#: Stand-ins for the two credentials. Long enough and dotted enough to satisfy
#: the startup checks in src/services/environment.py, free of long repeated
#: runs so that a window of one cannot coincide with ordinary log text, and
#: deliberately NOT shaped like a real Discord bot token: a credible-looking
#: one trips GitHub's secret scanning on every push, and the shape is not what
#: any of this exercises. Keep them obviously synthetic if you change them.
TOKEN = "synthetic.bot-token.never-valid.zq7w3n5k1h9f4d2s8a6g0j5t3y1u"
STORE_KEY = "synthetic-store-key-never-valid-p8m2c6x4v0b7n3q9w5e1r"
STORE_URL = "https://tagbot-store.example.workers.dev"

#: How much of a credential counts as a leak. Short enough that a masked
#: prefix fails, long enough not to collide with unrelated log text.
_WINDOW = 10


def leaks(output: str, *secrets: str) -> list[str]:
    """Every run of _WINDOW characters of any secret that survived into output."""
    return [window for secret in secrets
            for window in (secret[at:at + _WINDOW] for at in range(len(secret) - _WINDOW + 1))
            if window in output]


@pytest.fixture
def credentials(monkeypatch):
    """Both credentials in the environment, as a running deployment has them."""
    monkeypatch.setenv("DISCORD_TOKEN", TOKEN)
    monkeypatch.setenv("TAGBOT_STORE_KEY", STORE_KEY)
    monkeypatch.setenv("TAGBOT_STORE_URL", STORE_URL)
    return SimpleNamespace(token=TOKEN, key=STORE_KEY, url=STORE_URL)


def test_the_leak_detector_catches_a_fragment():
    """Without this, every assertion below could be passing for the wrong reason."""
    assert not leaks("nothing to see", TOKEN, STORE_KEY)
    assert leaks(f"token: {TOKEN[:12]}...", TOKEN)
    assert leaks(f"store rejected {STORE_KEY}", STORE_KEY)


@pytest.mark.parametrize("name", sorted(COMMAND_ARGUMENTS))
async def test_no_command_puts_a_credential_in_its_logs(ui, credentials, name):
    with captured_logs() as logs:
        await ui.drive(name)
    assert not leaks(logs.raw, credentials.token, credentials.key)


@pytest.mark.parametrize("name", sorted(COMMAND_ARGUMENTS))
async def test_a_failing_command_leaks_nothing_to_the_logs_or_the_user(ui, store, credentials,
                                                                       name):
    """The likeliest leak: a store error quoting the header it was rejected for."""
    store.outage = StoreError(
        f"GET {credentials.url}/guilds -> HTTP 401 "
        f"(sent Authorization: Bearer {credentials.key})")
    pressed = FakeInteraction()
    with captured_logs() as logs:
        try:
            await ui.run(name, *COMMAND_ARGUMENTS[name](), interaction=pressed)
        except StoreError as error:
            # Exactly what CommandTree.on_error does with what a callback raised.
            await report(pressed, error, subject=f"/{name}")

    assert not leaks(logs.raw, credentials.token, credentials.key)
    replies = [str(message) for message in
               pressed.response.messages + pressed.followup.messages]
    assert not leaks(" ".join(replies), credentials.token, credentials.key, credentials.url)


async def test_a_careless_call_site_cannot_leak_the_token(credentials):
    """Redaction is at the output boundary precisely so this cannot be a bug."""
    log = get_logger("Commands")
    with captured_logs() as logs:
        log.error("saved %s", credentials.token)
        log.info("auth header is Bearer %s", credentials.token)
    assert not leaks(logs.raw, credentials.token)
    assert "[REDACTED]" in logs.raw


async def test_a_credential_in_an_exception_is_stopped_twice_over(credentials):
    """Both rules at once, so neither can rot unnoticed behind the other.

    Passing the exception is what the code really does, and the type-name rule
    keeps the key out without redaction being involved. Stringifying it first
    is one careless edit away, gets past that rule, and is caught by redaction.
    """
    error = StoreError(f"HTTP 401 (sent Authorization: Bearer {credentials.key})")
    log = get_logger("StoreClient")
    with captured_logs() as logs:
        log.error("request - failed (%s)", error)
        log.error("request - failed (%s)", str(error))
    assert not leaks(logs.raw, credentials.key)
    assert "StoreError" in logs.raw, "the exception's type name should survive"
    assert "[REDACTED]" in logs.raw, "the stringified copy should have been redacted"


async def test_a_credential_smuggled_into_a_structured_field_is_redacted(credentials):
    """A field is the newest way into the output, and no more trusted than a message."""
    with captured_logs() as logs:
        get_logger("Commands").info(
            "ran", extra={"command": credentials.token, "outcome": credentials.key})
    assert not leaks(logs.raw, credentials.token, credentials.key)


async def test_the_store_client_leaks_nothing_while_retrying(credentials):
    """Its retry and giving-up lines describe requests that carried the key.

    Driven against a real local server rather than a mock, so the key really
    does travel as an Authorization header and a real aiohttp response really
    is what the logged lines are describing.
    """
    async def handler(request):
        assert request.headers["Authorization"] == f"Bearer {credentials.key}"
        return web.json_response({"error": "busy"}, status=503)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    server = TestServer(app)
    await server.start_server()
    client = StoreClient(str(server.make_url("")), credentials.key, attempts=3, backoff=0.001)
    try:
        with captured_logs() as logs, pytest.raises(StoreUnavailable):
            await client.get("/guilds/900000000000000001")
    finally:
        await client.close()
        await server.close()
    assert logs.lines("StoreClient", "retry"), "the client retried without saying so"
    assert logs.lines("StoreClient", "gave up"), "the client gave up without saying so"
    assert not leaks(logs.raw, credentials.token, credentials.key)


def test_startup_validation_confirms_the_credentials_without_reproducing_them(credentials):
    """A startup transcript is the log most likely to be pasted somewhere public."""
    with captured_logs() as logs:
        Environment.validate()
    assert logs.lines("Environment", "format accepted")
    assert not leaks(logs.raw, credentials.token, credentials.key)


@pytest.fixture
def library_loggers():
    """Restore the dependency loggers: they are global process state."""
    names = ("discord", "aiohttp")
    saved = [(logging.getLogger(name), logging.getLogger(name).handlers[:],
              logging.getLogger(name).level, logging.getLogger(name).propagate)
             for name in names]
    yield names
    for logger, handlers, level, propagate in saved:
        logger.handlers[:] = handlers
        logger.setLevel(level)
        logger.propagate = propagate


def test_a_dependency_cannot_leak_the_token_either(credentials, library_loggers):
    """discord.py puts the bot token in a header and the route in the message."""
    configure_library_logging()
    with captured_logs() as logs:
        logging.getLogger("discord.http").warning(
            "We are being rate limited. %s %s responded with 429.",
            "PUT", f"{credentials.url}/members?token={credentials.key}")
    assert logs.lines("discord.http", "429")
    assert not leaks(logs.raw, credentials.token, credentials.key, credentials.url)


async def test_a_background_sync_leaks_nothing_while_the_store_is_refusing(bot, credentials):
    """The one path with no interaction to answer, so the log is all there is."""
    bot.store.outage = StoreError(f"HTTP 401: bad key {credentials.key}")
    with captured_logs() as logs:
        await bot.equip("700000000000000001")
    assert not leaks(logs.raw, credentials.token, credentials.key)
