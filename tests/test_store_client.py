"""StoreClient: auth header, retries with backoff, timeouts, and error mapping."""

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from src.services.store_client import StoreClient, StoreError, StoreUnavailable

KEY = "k" * 48


async def _serve(handler):
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    server = TestServer(app)
    await server.start_server()
    return server


def _client(server, **kw):
    kw.setdefault("backoff", 0.001)
    return StoreClient(str(server.make_url("")), KEY, **kw)


async def test_sends_bearer_and_returns_json():
    seen = {}

    async def handler(request):
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = await request.json()
        return web.json_response({"ok": True})

    server = await _serve(handler)
    client = _client(server)
    try:
        assert await client.put("/guilds/1", {"role_id": "2"}) == {"ok": True}
        assert seen == {"auth": f"Bearer {KEY}", "body": {"role_id": "2"}}
    finally:
        await client.close()
        await server.close()


async def test_retries_transient_errors_then_succeeds():
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return web.json_response({"error": "busy"}, status=503)
        return web.json_response({"role_id": "9"})

    server = await _serve(handler)
    client = _client(server, attempts=4)
    try:
        assert await client.get("/guilds/1") == {"role_id": "9"}
        assert calls["n"] == 3
    finally:
        await client.close()
        await server.close()


async def test_gives_up_with_store_unavailable():
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        return web.json_response({"error": "down"}, status=502)

    server = await _serve(handler)
    client = _client(server, attempts=3)
    try:
        with pytest.raises(StoreUnavailable):
            await client.get("/guilds")
        assert calls["n"] == 3
    finally:
        await client.close()
        await server.close()


@pytest.mark.parametrize("status", [400, 401, 403, 404])
async def test_client_errors_are_not_retried(status):
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        return web.json_response({"error": "unauthorized"}, status=status)

    server = await _serve(handler)
    client = _client(server, attempts=4)
    try:
        with pytest.raises(StoreError) as info:
            await client.get("/guilds")
        assert not isinstance(info.value, StoreUnavailable)
        assert "unauthorized" in str(info.value)
        assert KEY not in str(info.value)
        assert calls["n"] == 1
    finally:
        await client.close()
        await server.close()


async def test_timeout_becomes_store_unavailable():
    async def handler(request):
        await asyncio.sleep(1)
        return web.json_response({})

    server = await _serve(handler)
    client = _client(server, attempts=2, timeout=0.1)
    try:
        with pytest.raises(StoreUnavailable):
            await client.get("/health")
    finally:
        await client.close()
        await server.close()


async def test_unreachable_host_becomes_store_unavailable():
    client = StoreClient("http://127.0.0.1:9", KEY, attempts=2, backoff=0.001, timeout=0.5)
    try:
        with pytest.raises(StoreUnavailable):
            await client.get("/health")
    finally:
        await client.close()


def test_requires_url_and_key(monkeypatch):
    monkeypatch.delenv("TAGBOT_STORE_URL", raising=False)
    monkeypatch.delenv("TAGBOT_STORE_KEY", raising=False)
    with pytest.raises(StoreError):
        StoreClient()


@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504])
async def test_retryable_statuses_use_bounded_exponential_backoff(monkeypatch, status):
    from unittest.mock import AsyncMock

    sleep = AsyncMock()
    monkeypatch.setattr("src.services.store_client.asyncio.sleep", sleep)
    monkeypatch.setattr("src.services.store_client.random.random", lambda: 0.5)
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        return web.json_response({}, status=status)

    server = await _serve(handler)
    client = _client(server, attempts=3, backoff=0.25)
    try:
        with pytest.raises(StoreUnavailable):
            await client.get("/health")
        assert calls == 3
        assert [c.args[0] for c in sleep.await_args_list] == [0.25, 0.5]
    finally:
        await client.close()
        await server.close()


async def _retry_after_run(header, monkeypatch, status=429, attempts=2):
    """Serve one `status` carrying `header`, then a 200.

    Returns (recorded sleep mock, client, server); the caller closes both.
    """
    from unittest.mock import AsyncMock

    sleep = AsyncMock()
    monkeypatch.setattr("src.services.store_client.asyncio.sleep", sleep)
    monkeypatch.setattr("src.services.store_client.random.random", lambda: 0.5)
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return web.json_response({}, status=status, headers={"Retry-After": header})
        return web.json_response({"ok": True})

    server = await _serve(handler)
    return sleep, _client(server, attempts=attempts, backoff=0.25), server


@pytest.mark.parametrize("header,expected", [
    # The numeric form Cloudflare sends on a 429.
    ("2", 2.0),
    ("0", 0.0),
    ("7.5", 7.5),
    (" 3 ", 3.0),
])
async def test_retry_after_seconds_is_honoured_over_the_guess(monkeypatch, header, expected):
    sleep, client, server = await _retry_after_run(header, monkeypatch)
    try:
        assert await client.get("/health") == {"ok": True}
        # 0.25 would be the computed backoff; the store's own figure wins.
        assert [c.args[0] for c in sleep.await_args_list] == [expected]
    finally:
        await client.close()
        await server.close()


async def test_retry_after_http_date_is_honoured(monkeypatch):
    """RFC 9110 allows an HTTP-date instead of a count of seconds."""
    from email.utils import format_datetime
    from datetime import datetime, timedelta, timezone

    when = datetime.now(timezone.utc) + timedelta(seconds=5)
    sleep, client, server = await _retry_after_run(format_datetime(when), monkeypatch)
    try:
        assert await client.get("/health") == {"ok": True}
        waited = sleep.await_args_list[0].args[0]
        # Computed against the clock, so allow for the round trip.
        assert 2.0 <= waited <= 6.0
    finally:
        await client.close()
        await server.close()


@pytest.mark.parametrize("header", ["soon", "", "-", "Tue, 99 Xxx 2026 99:99:99 GMT"])
async def test_unparseable_retry_after_falls_back_to_backoff(monkeypatch, header):
    """An unreadable header must not become a zero wait that retries instantly."""
    sleep, client, server = await _retry_after_run(header, monkeypatch)
    try:
        assert await client.get("/health") == {"ok": True}
        assert [c.args[0] for c in sleep.await_args_list] == [0.25]
    finally:
        await client.close()
        await server.close()


async def test_a_retry_after_past_the_cap_fails_fast_instead_of_hanging(monkeypatch):
    """Sitting on a deferred interaction for minutes is worse than failing."""
    sleep, client, server = await _retry_after_run("600", monkeypatch)
    try:
        with pytest.raises(StoreUnavailable, match="600s wait"):
            await client.get("/health")
        sleep.assert_not_awaited()
    finally:
        await client.close()
        await server.close()


async def test_retry_after_only_governs_the_response_that_sent_it(monkeypatch):
    """A later attempt without the header goes back to the computed backoff."""
    from unittest.mock import AsyncMock

    sleep = AsyncMock()
    monkeypatch.setattr("src.services.store_client.asyncio.sleep", sleep)
    monkeypatch.setattr("src.services.store_client.random.random", lambda: 0.5)
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return web.json_response({}, status=429, headers={"Retry-After": "3"})
        return web.json_response({}, status=503)

    server = await _serve(handler)
    client = _client(server, attempts=3, backoff=0.25)
    try:
        with pytest.raises(StoreUnavailable):
            await client.get("/health")
        # First wait came from the header, second from exponential backoff.
        assert [c.args[0] for c in sleep.await_args_list] == [3.0, 0.5]
    finally:
        await client.close()
        await server.close()


@pytest.mark.parametrize("body", ["null", "[]", '"denied"', "{", "not-json"])
async def test_unexpected_error_body_still_maps_to_store_error(body):
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        return web.Response(text=body, status=403, content_type="application/json")

    server = await _serve(handler)
    client = _client(server)
    try:
        with pytest.raises(StoreError, match="HTTP 403"):
            await client.get("/guilds")
        assert calls == 1
    finally:
        await client.close()
        await server.close()


async def test_malformed_success_json_is_retried_then_reported_unavailable():
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        return web.Response(text="{", content_type="application/json")

    server = await _serve(handler)
    client = _client(server, attempts=2)
    try:
        with pytest.raises(StoreUnavailable):
            await client.get("/health")
        assert calls == 2
    finally:
        await client.close()
        await server.close()


async def test_close_is_idempotent_and_client_can_reopen():
    async def handler(request):
        return web.json_response({"ok": True})

    server = await _serve(handler)
    client = _client(server)
    try:
        await client.close()  # A lazy session may not have been created yet.
        assert await client.post("/health") == {"ok": True}
        session = client._session
        await client.close()
        await client.close()
        assert session.closed
        assert await client.get("/health") == {"ok": True}
        assert client._session is not session
    finally:
        await client.close()
        await server.close()
