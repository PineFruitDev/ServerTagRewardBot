"""Classifying Discord REST failures.

The point of this module is that a 403 and a 503 need opposite responses, so
every test here pins a distinction rather than just "an error happened".
"""

from types import SimpleNamespace

import discord
import pytest

from src.services.discord_errors import (
    API_FAILURES,
    MISSING_PERMISSIONS,
    UNKNOWN_ROLE,
    blocks_whole_guild,
    describe,
    is_transient,
    retry_after,
    user_message,
)

UNKNOWN_MEMBER = 10007


def http_error(status, code=0, message="Discord said no"):
    """An HTTPException shaped the way discord.py builds one from a response."""
    body = {"code": code, "message": message} if code else message
    return discord.HTTPException(SimpleNamespace(status=status, reason="reason"), body)


@pytest.mark.parametrize("status,code,expected", [
    (403, 0, "HTTP 403"),
    (403, MISSING_PERMISSIONS, "HTTP 403 (Discord error 50013)"),
    (404, UNKNOWN_ROLE, "HTTP 404 (Discord error 10011)"),
    (429, 0, "HTTP 429"),
    (503, 0, "HTTP 503"),
])
def test_describe_keeps_the_status_and_code(status, code, expected):
    assert describe(http_error(status, code)) == expected


def test_describe_does_not_echo_the_response_body():
    """The status is useful; Discord's prose is response content, so it stays out."""
    exc = http_error(403, MISSING_PERMISSIONS, message="Missing Permissions")
    assert describe(exc) == "HTTP 403 (Discord error 50013)"
    assert "Missing Permissions" not in describe(exc)


@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504])
def test_transient_statuses_are_worth_retrying(status):
    assert is_transient(http_error(status)) is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 405])
def test_client_errors_are_not_worth_retrying(status):
    assert is_transient(http_error(status)) is False


@pytest.mark.parametrize("status,code,expected", [
    # No permission to manage the role: identical for every other member.
    (403, 0, True),
    (403, MISSING_PERMISSIONS, True),
    # The role itself is gone, so no member can be given it.
    (404, UNKNOWN_ROLE, True),
    # This member left mid-sync. The rest of the guild is fine.
    (404, UNKNOWN_MEMBER, False),
    (404, 0, False),
    # Transient: the next member may well succeed.
    (429, 0, False),
    (500, 0, False),
    (503, 0, False),
])
def test_only_guild_wide_failures_stop_a_sync(status, code, expected):
    assert blocks_whole_guild(http_error(status, code)) is expected


@pytest.mark.parametrize("status,code,fragment", [
    (403, MISSING_PERMISSIONS, "Manage Roles"),
    (404, UNKNOWN_ROLE, "/setup"),
    (429, 0, "rate limiting"),
    (503, 0, "having trouble"),
    (400, 0, "logs"),
])
def test_user_message_is_actionable_for_the_case(status, code, fragment):
    assert fragment in user_message(http_error(status, code))


def test_user_message_never_forwards_discord_prose_to_the_user():
    exc = http_error(400, 50035, message="Invalid Form Body: secret-internal-detail")
    assert "secret-internal-detail" not in user_message(exc)


# --- RateLimited, which is not an HTTPException ------------------------------
#
# discord.py raises it instead of sleeping when a wait exceeds the client's
# max_ratelimit_timeout, and Ratelimit.acquire can raise it before the request
# is even sent. Every helper here has to accept it, because `except
# HTTPException` does not.


def test_rate_limited_is_not_an_http_exception():
    """The whole reason API_FAILURES exists. If this ever changes, simplify."""
    assert not issubclass(discord.RateLimited, discord.HTTPException)
    assert discord.RateLimited in API_FAILURES


def test_describe_reports_the_wait_a_rate_limit_asked_for():
    assert describe(discord.RateLimited(12.5)) == "rate limited, 12.5s requested"


def test_a_rate_limit_is_always_worth_retrying():
    assert is_transient(discord.RateLimited(1.0)) is True


def test_a_rate_limit_never_stops_a_whole_sync():
    """Unlike a 403, it passes on its own, so the remaining members are fine."""
    assert blocks_whole_guild(discord.RateLimited(1.0)) is False


def test_a_rate_limit_reads_the_same_to_the_admin_either_way():
    assert user_message(discord.RateLimited(1.0)) == user_message(http_error(429))


# --- how long to wait -------------------------------------------------------


def test_retry_after_is_taken_from_a_rate_limit_directly():
    assert retry_after(discord.RateLimited(8.0)) == 8.0


def test_a_negative_rate_limit_wait_is_clamped():
    assert retry_after(discord.RateLimited(-5.0)) == 0.0


def test_retry_after_is_read_from_the_response_header():
    """The library gave up on this 429, but its response still carries the header."""
    exc = discord.HTTPException(
        SimpleNamespace(status=429, reason="reason", headers={"Retry-After": "3.5"}), "nope")
    assert retry_after(exc) == 3.5


@pytest.mark.parametrize("headers", [{}, {"Retry-After": ""}, {"Retry-After": "soon"}])
def test_no_usable_header_means_no_answer_rather_than_zero(headers):
    """None leaves the caller to back off; 0.0 would retry straight into the limit."""
    exc = discord.HTTPException(
        SimpleNamespace(status=429, reason="reason", headers=headers), "nope")
    assert retry_after(exc) is None


def test_a_response_with_no_headers_at_all_is_tolerated():
    """HTTPException only promises `.status`; the backend supplies the rest."""
    assert retry_after(http_error(503)) is None


@pytest.mark.parametrize("headers", ["Retry-After: 5", 5, object()])
def test_headers_that_are_not_a_mapping_are_tolerated(headers):
    """Deciding how long to wait must not itself raise on an odd response."""
    exc = discord.HTTPException(
        SimpleNamespace(status=429, reason="reason", headers=headers), "nope")
    assert retry_after(exc) is None
