"""Bounded retry and backoff on every Discord REST path.

Two halves. The first pins the retry helper itself against a fake clock: what
is retried, what is refused, how long it waits and when it stops waiting. The
second walks each call path the bot actually has (role add, role remove, member
list, user refetch, command tree sync) and checks the retry is wired in and
that a retried role write is still applied exactly once.

Sleeps are asserted, never spent. `discord_retry.asyncio` is the asyncio
module, so patching `sleep` on it is global for the test; nothing else here
awaits a timer, and it keeps the recorded delays exact.

The suite-wide fixture in conftest sets BACKOFF_SECONDS to 0, so every test
that cares about a computed delay sets it explicitly.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from src.core.bot import Bot
from src.services import discord_retry
from src.services.environment import Environment
from src.services.guild_config import GuildConfig
from src.services.streaks import StreakStore
from src.services import tag_sync as tag_sync_module
from src.services.tag_sync import TagSyncService

UNKNOWN_ROLE = 10011
UNKNOWN_MEMBER = 10007


def http_error(status, code=0, retry_after=None):
    """An HTTPException carrying a response, optionally with a Retry-After.

    discord.py builds these from the real response, so the header is reachable
    through `.response.headers` exactly as it is here.
    """
    headers = {} if retry_after is None else {"Retry-After": str(retry_after)}
    body = {"code": code, "message": "nope"} if code else "nope"
    response = SimpleNamespace(status=status, reason="nope", headers=headers)
    return discord.HTTPException(response, body)


@pytest.fixture
def sleep(monkeypatch):
    """Record every wait the retry layer asks for without serving it."""
    recorded = AsyncMock()
    monkeypatch.setattr(discord_retry.asyncio, "sleep", recorded)
    return recorded


def waits(sleep):
    return [c.args[0] for c in sleep.await_args_list]


# --------------------------------------------------------------------------- #
# The retry helper
# --------------------------------------------------------------------------- #


async def test_a_call_that_works_is_made_once_and_never_waits(sleep):
    call = AsyncMock(return_value="done")
    assert await discord_retry.call("thing", call) == "done"
    call.assert_awaited_once()
    sleep.assert_not_awaited()


async def test_a_transient_failure_is_retried_and_the_result_returned(sleep):
    call = AsyncMock(side_effect=[http_error(503), "done"])
    assert await discord_retry.call("thing", call) == "done"
    assert call.await_count == 2


@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504])
async def test_every_transient_status_is_retried(sleep, status):
    call = AsyncMock(side_effect=[http_error(status), "done"])
    assert await discord_retry.call("thing", call) == "done"
    assert call.await_count == 2


@pytest.mark.parametrize("status,code", [
    (400, 0), (401, 0), (403, 0), (404, UNKNOWN_ROLE), (404, UNKNOWN_MEMBER), (405, 0),
])
async def test_a_failure_discord_will_repeat_is_not_retried(sleep, status, code):
    """Retrying a 403 per member is the bug this repo already fixed once."""
    error = http_error(status, code)
    call = AsyncMock(side_effect=error)
    with pytest.raises(discord.HTTPException) as raised:
        await discord_retry.call("thing", call)
    assert raised.value is error
    call.assert_awaited_once()
    sleep.assert_not_awaited()


async def test_the_last_failure_is_raised_once_the_attempts_run_out(sleep):
    error = http_error(503)
    call = AsyncMock(side_effect=error)
    with pytest.raises(discord.HTTPException) as raised:
        await discord_retry.call("thing", call, attempts=3)
    assert raised.value is error
    assert call.await_count == 3
    # Two waits for three attempts: nothing sleeps after the final failure.
    assert len(waits(sleep)) == 2


async def test_a_fresh_call_is_built_for_every_attempt(sleep):
    """An awaited coroutine cannot be awaited again, so this must be a factory."""
    attempts = []

    async def succeed_on_third():
        attempts.append(1)
        if len(attempts) < 3:
            raise http_error(503)
        return "done"

    assert await discord_retry.call("thing", succeed_on_third, attempts=3) == "done"
    assert len(attempts) == 3


async def test_retry_after_is_honoured_over_the_computed_backoff(sleep, monkeypatch):
    monkeypatch.setattr(discord_retry, "BACKOFF_SECONDS", 0.5)
    call = AsyncMock(side_effect=[http_error(429, retry_after=7), "done"])
    assert await discord_retry.call("thing", call) == "done"
    assert waits(sleep) == [7.0]


async def test_a_retry_after_date_is_honoured(sleep):
    """RFC 9110 allows an HTTP-date, and some proxies rewrite the header to one."""
    from email.utils import format_datetime
    from datetime import datetime, timedelta, timezone

    when = datetime.now(timezone.utc) + timedelta(seconds=5)
    call = AsyncMock(side_effect=[http_error(429, retry_after=format_datetime(when)), "done"])
    assert await discord_retry.call("thing", call) == "done"
    # Whole-second header resolution, so allow the second it was truncated to.
    assert 3.0 <= waits(sleep)[0] <= 6.0


async def test_an_unreadable_retry_after_falls_back_to_backoff(sleep, monkeypatch):
    """Read as zero it would retry instantly into the rate limit it just hit."""
    monkeypatch.setattr(discord_retry, "BACKOFF_SECONDS", 0.5)
    monkeypatch.setattr("src.services.backoff.random.random", lambda: 0.5)
    call = AsyncMock(side_effect=[http_error(429, retry_after="not-a-number"), "done"])
    assert await discord_retry.call("thing", call) == "done"
    assert waits(sleep) == [0.5]


async def test_a_response_without_headers_still_backs_off(sleep, monkeypatch):
    """HTTPException only promises `.status`; the response behind it varies."""
    monkeypatch.setattr(discord_retry, "BACKOFF_SECONDS", 0.5)
    monkeypatch.setattr("src.services.backoff.random.random", lambda: 0.5)
    bare = discord.HTTPException(SimpleNamespace(status=503, reason="nope"), "nope")
    call = AsyncMock(side_effect=[bare, "done"])
    assert await discord_retry.call("thing", call) == "done"
    assert waits(sleep) == [0.5]


async def test_the_backoff_grows_and_is_jittered(sleep, monkeypatch):
    monkeypatch.setattr(discord_retry, "BACKOFF_SECONDS", 0.5)
    monkeypatch.setattr("src.services.backoff.random.random", lambda: 0.5)
    call = AsyncMock(side_effect=http_error(503))
    with pytest.raises(discord.HTTPException):
        await discord_retry.call("thing", call, attempts=4)
    # base * 2**(n-1) * (0.5 + jitter), with the jitter pinned to 0.5.
    assert waits(sleep) == [0.5, 1.0, 2.0]


async def test_a_wait_longer_than_the_cap_fails_fast_instead_of_sleeping(sleep):
    """Holding a deferred interaction past Discord's expiry helps nobody."""
    over = discord_retry.MAX_WAIT_SECONDS + 1
    call = AsyncMock(side_effect=[http_error(429, retry_after=over), "done"])
    with pytest.raises(discord.HTTPException):
        await discord_retry.call("thing", call)
    call.assert_awaited_once()
    sleep.assert_not_awaited()


async def test_the_waits_cannot_add_up_past_the_budget(sleep, monkeypatch):
    """Bounded in total, not just per attempt."""
    monkeypatch.setattr(discord_retry, "MAX_WAIT_SECONDS", 30.0)
    monkeypatch.setattr(discord_retry, "WAIT_BUDGET_SECONDS", 25.0)
    call = AsyncMock(side_effect=http_error(429, retry_after=10))
    with pytest.raises(discord.HTTPException):
        await discord_retry.call("thing", call, attempts=5)
    # 10 + 10 fits; the third would reach 30 and is refused, stopping early
    # rather than using all five attempts.
    assert waits(sleep) == [10.0, 10.0]
    assert call.await_count == 3


async def test_a_rate_limit_the_library_refused_to_sleep_is_retried(sleep):
    """RateLimited is a sibling of HTTPException, not a subclass."""
    call = AsyncMock(side_effect=[discord.RateLimited(4.0), "done"])
    assert await discord_retry.call("thing", call) == "done"
    assert waits(sleep) == [4.0]


async def test_a_rate_limited_wait_past_the_cap_is_not_slept_off(sleep):
    call = AsyncMock(side_effect=discord.RateLimited(discord_retry.MAX_WAIT_SECONDS + 1))
    with pytest.raises(discord.RateLimited):
        await discord_retry.call("thing", call)
    call.assert_awaited_once()
    sleep.assert_not_awaited()


async def test_cancellation_is_not_mistaken_for_a_transient_failure(sleep):
    """A shutdown must stop the call, not retry it twice more."""
    call = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await discord_retry.call("thing", call)
    call.assert_awaited_once()
    sleep.assert_not_awaited()


async def test_an_unexpected_error_is_not_retried(sleep):
    call = AsyncMock(side_effect=ValueError("bug"))
    with pytest.raises(ValueError):
        await discord_retry.call("thing", call)
    call.assert_awaited_once()


async def test_a_retry_is_logged_with_the_status_and_the_delay(sleep, caplog):
    call = AsyncMock(side_effect=[http_error(503, retry_after=2), "done"])
    with caplog.at_level("WARNING"):
        await discord_retry.call("get_members", call, context="99")
    assert "HTTP 503" in caplog.text
    assert "get_members [99]" in caplog.text


async def test_giving_up_is_logged_with_the_cause(sleep, caplog):
    call = AsyncMock(side_effect=http_error(503))
    with caplog.at_level("WARNING"):
        with pytest.raises(discord.HTTPException):
            await discord_retry.call("add_role", call, context="99", attempts=2)
    assert "gave up after 2 attempt(s)" in caplog.text
    assert "HTTP 503" in caplog.text


# --------------------------------------------------------------------------- #
# Each Discord API call path in the bot
# --------------------------------------------------------------------------- #


@pytest.fixture
def sync(monkeypatch):
    """TagSyncService over a stubbed HTTP layer, as in test_tag_sync."""
    http = SimpleNamespace(**{name: AsyncMock() for name in (
        "add_role", "remove_role", "get_members", "get_user",
    )})
    http.get_members.return_value = []
    client = SimpleNamespace(http=http, event=Mock())
    config = SimpleNamespace(get_role=AsyncMock(return_value="10"), all_guilds=AsyncMock())
    streaks = SimpleNamespace(mark_many=AsyncMock())
    monkeypatch.setattr(GuildConfig, "_instance", config)
    monkeypatch.setattr(StreakStore, "_instance", streaks)
    monkeypatch.setattr(Environment, "_config", SimpleNamespace(sync_delay_ms=0))
    return SimpleNamespace(service=TagSyncService(client), http=http,
                           config=config, streaks=streaks)


def member(uid="42", guild="1", enabled=True):
    return {"id": uid, "primary_guild": {
        "identity_guild_id": guild, "identity_enabled": enabled,
    }}


@pytest.mark.parametrize("error", [http_error(429, retry_after=1), http_error(503),
                                   discord.RateLimited(1.0)])
async def test_a_rate_limited_role_grant_is_retried_but_applied_once(sync, sleep, error):
    """The regression this guards: counting a grant per HTTP attempt.

    Two attempts reach Discord, but the member is granted the role once, so the
    reported total matches reality rather than the retry count. Both calls are
    idempotent at Discord's end, so the retry cannot double-apply the role
    itself either.
    """
    sync.http.add_role.side_effect = [error, None]
    assert await sync.service.apply_role("1", "10", "42", True) is True
    assert sync.http.add_role.await_count == 2
    stats = sync.service.stats_for("1")
    assert (stats.roles_added, stats.roles_removed) == (1, 0)


async def test_a_rate_limited_role_removal_is_retried_but_applied_once(sync, sleep):
    sync.http.remove_role.side_effect = [http_error(429, retry_after=1), None]
    assert await sync.service.apply_role("1", "10", "42", False) is True
    assert sync.http.remove_role.await_count == 2
    stats = sync.service.stats_for("1")
    assert (stats.roles_added, stats.roles_removed) == (0, 1)


async def test_a_role_grant_that_stays_rate_limited_counts_nothing(sync, sleep):
    sync.http.add_role.side_effect = http_error(429, retry_after=1)
    assert await sync.service.apply_role("1", "10", "42", True) is False
    assert sync.http.add_role.await_count == discord_retry.ATTEMPTS
    stats = sync.service.stats_for("1")
    assert (stats.roles_added, stats.roles_removed) == (0, 0)
    # A rate limit is not a guild-wide block: it passes on its own.
    assert stats.role_blocked is None


async def test_a_rate_limit_the_library_handed_over_does_not_escape_the_gateway(sync, sleep):
    """RateLimited is not an HTTPException, so `except HTTPException` misses it.

    _handle_raw is called directly: the blanket except in on_socket_raw_receive
    would otherwise hide an escape as a logged traceback.
    """
    sync.http.add_role.side_effect = discord.RateLimited(1.0)
    frame = json.dumps({"t": "GUILD_MEMBER_UPDATE", "d": {"guild_id": "1", "user": member()}})
    await sync.service._handle_raw(frame)
    assert sync.http.add_role.await_count == discord_retry.ATTEMPTS
    assert sync.service.stats_for("1").roles_added == 0


async def test_a_rate_limited_member_page_is_retried_before_the_sync_is_lost(sync, sleep):
    """Losing a page abandons the rest of the guild, the priciest failure here."""
    sync.http.get_members.side_effect = [
        http_error(429, retry_after=1),
        [{"user": member("7"), "roles": []}],
        [],
    ]
    stats = await sync.service.full_sync("1")
    assert stats.members_checked == 1
    assert stats.last_sync_at is not None


async def test_a_member_page_that_stays_down_still_fails_the_sync(sync, sleep):
    sync.http.get_members.side_effect = http_error(503)
    with pytest.raises(discord.HTTPException):
        await sync.service.full_sync("1")
    assert sync.http.get_members.await_count == discord_retry.ATTEMPTS
    # An unfinished sync must not claim a successful run.
    assert sync.service.stats_for("1").last_sync_at is None
    assert not sync.service.is_syncing("1")


# --- a sync must not retry its way through a whole guild during an outage ----


async def test_an_outage_stops_the_sync_instead_of_retrying_every_member(sync, sleep):
    """The cost of adding retry, bounded.

    Each write already spends its own attempts, so proving a guild-wide outage
    one member at a time is now several calls per member. A run of failures is
    an outage, which blocks_whole_guild cannot see because each 503 is
    individually transient.
    """
    sync.http.get_members.side_effect = [
        [{"user": member(str(i)), "roles": []} for i in range(1, 501)], [],
    ]
    sync.http.add_role.side_effect = http_error(503)
    stats = await sync.service.full_sync("1")

    limit = tag_sync_module._MAX_CONSECUTIVE_ROLE_FAILURES
    assert stats.stalled_after == limit
    assert stats.members_checked == limit
    # Bounded: the threshold times the per-call attempts, not 500 members' worth.
    assert sync.http.add_role.await_count == limit * discord_retry.ATTEMPTS
    # A transient run is not a permissions problem, so it must not be reported
    # as one, and the lock is released either way.
    assert stats.role_blocked is None
    assert not sync.service.is_syncing("1")


async def test_occasional_failures_do_not_stop_a_sync(sync, sleep):
    """Only a run of them counts; the counter resets on every success."""
    members = [{"user": member(str(i)), "roles": []} for i in range(1, 21)]
    sync.http.get_members.side_effect = [members, []]
    # Fail every other member: never more than one in a row.
    sync.http.add_role.side_effect = [
        http_error(503) if i % 2 else None for i in range(len(members) * discord_retry.ATTEMPTS)
    ]
    stats = await sync.service.full_sync("1")
    assert stats.stalled_after is None
    assert stats.members_checked == 20
    assert stats.last_sync_at is not None


async def test_a_stalled_sync_is_retested_from_scratch_next_time(sync, sleep):
    """An outage passes, so the next run must not inherit the verdict."""
    sync.http.get_members.side_effect = [
        [{"user": member(str(i)), "roles": []} for i in range(1, 51)], [],
    ]
    sync.http.add_role.side_effect = http_error(503)
    assert (await sync.service.full_sync("1")).stalled_after is not None

    sync.http.get_members.side_effect = [[{"user": member("1"), "roles": []}], []]
    sync.http.add_role.side_effect = None
    stats = await sync.service.full_sync("1")
    assert stats.stalled_after is None
    assert (stats.members_checked, stats.roles_added) == (1, 1)


async def test_a_rate_limited_user_refetch_is_retried(sync, sleep):
    """The member list omits primary_guild, so this fetch decides the reward."""
    sync.http.get_members.side_effect = [[{"user": {"id": "7"}, "roles": []}], []]
    sync.http.get_user.side_effect = [http_error(429, retry_after=1), member("7")]
    stats = await sync.service.full_sync("1")
    assert sync.http.get_user.await_count == 2
    assert (stats.members_checked, stats.currently_repping) == (1, 1)
    sync.http.add_role.assert_awaited_once_with(1, 7, 10)


async def test_a_refetch_that_stays_rate_limited_skips_only_that_member(sync, sleep):
    sync.http.get_members.side_effect = [
        [{"user": {"id": "7"}, "roles": []}, {"user": member("8"), "roles": []}], [],
    ]
    sync.http.get_user.side_effect = http_error(429)
    stats = await sync.service.full_sync("1")
    assert sync.http.get_user.await_count == discord_retry.ATTEMPTS
    # The second member had its tag inline, so it is still reconciled.
    sync.http.add_role.assert_awaited_once_with(1, 8, 10)
    assert stats.members_checked == 2


async def test_a_rate_limited_command_sync_is_retried(sleep):
    """Global command registration is one of Discord's tightest buckets."""
    tree = SimpleNamespace(sync=AsyncMock(side_effect=[
        http_error(429, retry_after=2), ["a", "b"],
    ]))
    assert await Bot.sync_commands(SimpleNamespace(tree=tree)) == 2
    assert tree.sync.await_count == 2
    assert waits(sleep) == [2.0]


async def test_a_command_sync_that_stays_rate_limited_raises(sleep):
    tree = SimpleNamespace(sync=AsyncMock(side_effect=http_error(429, retry_after=1)))
    with pytest.raises(discord.HTTPException):
        await Bot.sync_commands(SimpleNamespace(tree=tree))
    assert tree.sync.await_count == discord_retry.ATTEMPTS


async def test_a_rate_limit_is_reported_to_the_admin_not_as_a_crash(sleep):
    """RateLimited reaching the tree handler used to print "an error occurred"."""
    send = AsyncMock()
    interaction = SimpleNamespace(
        command=SimpleNamespace(name="sync"),
        response=SimpleNamespace(is_done=lambda: False, send_message=send),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    await Bot._on_tree_error(None, interaction, discord.RateLimited(12.0))
    message = send.await_args.args[0]
    assert "rate limiting" in message
    assert "An error occurred" not in message


@pytest.mark.parametrize("members", [True, False])
def test_the_client_is_built_with_a_ceiling_on_its_internal_sleep(members):
    """Without this, discord.py sleeps whatever retry_after it is handed.

    A long global rate limit would then park a sync or a deferred interaction
    indefinitely, inside a single call, where no bound of ours applies. The
    ceiling converts that into a RateLimited the retry layer can bound. The
    library floors the value at 30s, so that is what the HTTP layer reports.
    """
    import main
    from src.services.environment import Config

    config = Config(discord_token="x" * 60, environment="test", sync_on_start=False,
                    sync_delay_ms=0, members_intent=members)
    bot = main.build_bot(config, members=members)
    assert bot.http.max_ratelimit_timeout == 30.0


async def test_a_rate_limited_error_reply_does_not_raise_again(sleep):
    """Reporting a failure must not become a second failure."""
    blow_up = AsyncMock(side_effect=discord.RateLimited(5.0))
    interaction = SimpleNamespace(
        command=SimpleNamespace(name="status"),
        response=SimpleNamespace(is_done=lambda: False, send_message=blow_up),
        followup=SimpleNamespace(send=blow_up),
    )
    await Bot._on_tree_error(None, interaction, RuntimeError("original failure"))
    # Not retried: the interaction token is single-use and expires.
    blow_up.assert_awaited_once()
