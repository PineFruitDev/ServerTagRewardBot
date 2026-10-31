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


async def test_client_errors_are_not_retried():
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        return web.json_response({"error": "unauthorized"}, status=401)

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
