"""Shared retry arithmetic: reading Retry-After, and guessing when it is absent.

Two callers need exactly this and nothing more: the tagbot-store client, whose
Worker answers a 429 with a numeric Retry-After, and the Discord REST paths,
where the same header rides along on a 429 the library gave up on. Keeping the
parsing in one place means a malformed header is treated identically on both
sides, which matters because the failure modes are opposite: read as zero it
retries instantly into a rate limit, and read as None it waits a sensible
guess instead.

Nothing here sleeps or knows about HTTP clients, so it is directly testable
against a fixed clock.
"""

import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


def parse_retry_after(raw: str | None) -> float | None:
    """Seconds a Retry-After header asks for, or None when it did not say.

    RFC 9110 allows either a number of seconds or an HTTP-date, and both are
    seen in practice: Discord and Cloudflare send the numeric form, while some
    proxies rewrite it to a date. Anything unparseable returns None so the
    caller falls back to a computed backoff, rather than 0.0, which would turn
    a rate limit into an instant retry.

    A date already in the past clamps to 0.0: the wait it described has
    elapsed, so there is nothing left to serve.
    """
    if not raw:
        return None
    raw = raw.strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def jittered_delay(attempt: int, base: float) -> float:
    """Exponential backoff for a 1-based attempt number, with jitter.

    The jitter spreads the retries of everything that failed at the same
    moment. Without it, a guild-wide failure (or a restart) lines every
    pending call up on the same tick and the retry arrives as one more spike
    at an API that is already struggling.

    A base of 0 disables the wait entirely, which is how the test suite keeps
    the retry logic under test without spending the wall clock on it.
    """
    return base * (2 ** max(0, attempt - 1)) * (0.5 + random.random())
