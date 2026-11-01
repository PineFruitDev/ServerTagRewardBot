"""Classifying Discord REST failures.

The point of this module is that a 403 and a 503 need opposite responses, so
every test here pins a distinction rather than just "an error happened".
"""

from types import SimpleNamespace

import discord
import pytest

from src.services.discord_errors import (
    MISSING_PERMISSIONS,
    UNKNOWN_ROLE,
    blocks_whole_guild,
    describe,
    is_transient,
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
