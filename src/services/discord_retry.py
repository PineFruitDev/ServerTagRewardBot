"""Bounded retry for Discord REST calls, layered on top of what the library does.

## Why this is a thin layer and not a retry loop of its own

discord.py already retries. `HTTPClient.request` loops up to five times and,
inside that loop, sleeps the `retry_after` a 429 asked for and sleeps a short
escalating delay on 500, 502, 504 and 524. Wrapping every call in an eager
retry would therefore *double* the backoff, which is why it was not done
earlier. Nothing here changes that: this layer only ever runs once the library
has already given up and raised, so the two never back off over each other.

What reaches a caller, and so what this layer is for:

- a 429 whose five internal attempts all came back rate limited, raised as an
  `HTTPException` with the `Retry-After` header still on the response;
- a 429 with no `Via` header, which the library treats as a Cloudflare block
  and raises immediately, with no sleep and no retry at all;
- a 5xx that outlived all five attempts, raised as `DiscordServerError`;
- `RateLimited`, which is raised instead of sleeping when a wait exceeds the
  client's `max_ratelimit_timeout`. `Ratelimit.acquire` can raise it
  *pre-emptively, before the request is sent*, which is the one case where a
  retry is unconditionally safe because nothing was transmitted.

Without `max_ratelimit_timeout` the library sleeps whatever `retry_after` it is
handed, with no ceiling, so a long global rate limit parks a sync or a deferred
interaction indefinitely. `MAX_RATELIMIT_TIMEOUT` below is passed to the client
to convert that unbounded sleep into a `RateLimited` this layer can decide
about. The library floors the value at 30s.

## Bounds

Three, because "retry until it works" is how a rate limit becomes an outage:

- `ATTEMPTS` caps how many times a call is made at all;
- `MAX_WAIT_SECONDS` caps any single wait. A longer request is not slept off,
  it fails fast, because the caller is usually holding a deferred interaction
  that Discord expires after 15 minutes;
- `WAIT_BUDGET_SECONDS` caps the waits added together, so the worst case is a
  known number rather than attempts times the cap.

## What is deliberately not retried here

Interaction replies. The initial response to an interaction has a three second
deadline and its token is single-use, so sleeping and resending earns a 40060
("interaction has already been acknowledged") rather than a delivered message.
Those paths report instead, via `discord_errors.user_message`.

Transport errors (a reset connection, a DNS blip) are also left alone: the
library has its own `OSError` retry for those, and widening this layer to
cover them would reintroduce the double-backoff this module exists to avoid.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from src.services.backoff import jittered_delay
from src.services.discord_errors import API_FAILURES, describe, is_transient, retry_after
from src.services.logger import get_logger

log = get_logger("DiscordRetry")

T = TypeVar("T")

#: Total times a call is attempted, first try included. Two retries is enough
#: to ride out a bucket reset without turning one slow call into a long stall.
ATTEMPTS = 3

#: Base for the jittered exponential backoff used when Discord did not say how
#: long to wait. The suite sets this to 0 so the logic is tested without the
#: wall clock; the dedicated retry tests set it explicitly.
BACKOFF_SECONDS = 0.5

#: Longest single wait that is worth sleeping rather than failing fast.
MAX_WAIT_SECONDS = 30.0

#: Longest the waits may add up to across all attempts of one call.
WAIT_BUDGET_SECONDS = 45.0

#: Handed to the Discord client so an unbounded internal sleep becomes a
#: RateLimited this module can bound. The library raises the value to 30 if it
#: is given less.
MAX_RATELIMIT_TIMEOUT = 30.0


async def call(what: str, make_call: Callable[[], Awaitable[T]], *,
               context: str = "", attempts: int | None = None) -> T:
    """Run a Discord REST call, retrying bounded times on a transient failure.

    `make_call` is called afresh for each attempt, since a coroutine cannot be
    awaited twice. `what` and `context` only label the logs.

    Re-raises the last failure when the attempts or the wait budget run out, so
    a caller still decides what a definitive failure means: there is no sentinel
    return value to mistake for success. Non-transient failures (403, 404, 400)
    are re-raised on the first attempt without a wait.
    """
    limit = ATTEMPTS if attempts is None else max(1, attempts)
    label = f"{what} [{context}]" if context else what
    spent = 0.0

    for attempt in range(1, limit + 1):
        try:
            return await make_call()
        except API_FAILURES as exc:
            # Anything Discord will refuse again for the same reason: no wait
            # would help, and retrying a 403 per member is the bug this repo
            # already fixed once.
            if not is_transient(exc):
                raise
            if attempt == limit:
                log.warning("call - %s gave up after %d attempt(s) (%s)",
                            label, limit, describe(exc))
                raise

            requested = retry_after(exc)
            if requested is not None:
                delay, source = requested, "Retry-After"
            else:
                delay, source = jittered_delay(attempt, BACKOFF_SECONDS), "backoff"

            if delay > MAX_WAIT_SECONDS:
                log.error("call - %s asked to wait %.1fs, over the %.0fs cap; giving up (%s)",
                          label, delay, MAX_WAIT_SECONDS, describe(exc))
                raise
            if spent + delay > WAIT_BUDGET_SECONDS:
                log.error("call - %s would exceed the %.0fs retry budget; giving up (%s)",
                          label, WAIT_BUDGET_SECONDS, describe(exc))
                raise

            spent += delay
            log.warning("call - %s failed (%s), retry %d/%d in %.2fs (%s)",
                        label, describe(exc), attempt, limit - 1, delay, source)
            await asyncio.sleep(delay)

    # Unreachable: the loop either returns or raises on its final attempt. Kept
    # because mypy cannot prove that, and an assertion beats an implicit None.
    raise AssertionError(  # pragma: no cover - unreachable, satisfies the return type
        f"retry loop for {label} ended without a result")
