"""Offline coverage of the store smoke test script.

scripts/smoke_test.py is run by hand against a real tagbot-store, so nothing
here contacts a live store: a loopback aiohttp app stands in for the Worker.
The write path is deliberately not faked, because a second Python
implementation of the store would drift from the Worker and vouch for nothing;
it stays covered by worker/test/store.test.mjs against real D1. What is pinned
here is the read-only path, the pass/fail reporting, and the promise in the
module docstring that the key is never printed.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "smoke_test.py"
KEY = "s" * 48


@pytest.fixture
def smoke():
    spec = importlib.util.spec_from_file_location("smoke_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def store(monkeypatch):
    """A loopback stand-in for tagbot-store's read-only endpoints."""
    state = {"guilds": [("1", "10")], "requests": []}

    async def handler(request):
        state["requests"].append(request.path)
        if request.headers.get("Authorization") != f"Bearer {KEY}":
            return web.json_response({"error": "unauthorized"}, status=401)
        if request.path == "/health":
            return web.json_response({"ok": True})
        if request.path == "/guilds":
            return web.json_response({"guilds": state["guilds"]})
        return web.json_response({"error": "not found"}, status=404)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setenv("TAGBOT_STORE_URL", str(server.make_url("")).rstrip("/"))
    monkeypatch.setenv("TAGBOT_STORE_KEY", KEY)
    try:
        yield state
    finally:
        await server.close()


def test_check_passes_quietly(smoke, capsys):
    smoke.check(True, "a true thing")
    assert capsys.readouterr().out == "PASS a true thing\n"


def test_check_aborts_on_failure(smoke, capsys):
    """A failed assertion must stop the run rather than continue reporting."""
    with pytest.raises(SystemExit) as exc:
        smoke.check(False, "a false thing")
    assert exc.value.code == 1
    assert capsys.readouterr().out == "FAIL a false thing\n"


async def test_read_only_run_checks_auth_health_and_listing(smoke, store, capsys):
    await smoke.main(write=False)

    out = capsys.readouterr().out
    assert "PASS health with bearer key" in out
    assert "PASS wrong key rejected (401)" in out
    assert "PASS list guilds (1 configured)" in out
    assert "FAIL" not in out
    # Read-only means exactly that: no writes were attempted.
    assert set(store["requests"]) == {"/health", "/guilds"}


async def test_read_only_run_stops_before_the_write_checks(smoke, store, capsys):
    """Without --write the script must not reach set_role/disable/mark."""
    await smoke.main(write=False)
    assert "set_role" not in capsys.readouterr().out
    assert not any(path.startswith("/streaks") for path in store["requests"])


async def test_run_never_prints_the_store_key(smoke, store, capsys):
    await smoke.main(write=False)
    assert KEY not in capsys.readouterr().out


async def test_an_unhealthy_store_fails_the_run(smoke, monkeypatch, capsys):
    async def handler(request):
        return web.json_response({"ok": False})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setenv("TAGBOT_STORE_URL", str(server.make_url("")).rstrip("/"))
    monkeypatch.setenv("TAGBOT_STORE_KEY", KEY)
    try:
        with pytest.raises(SystemExit) as exc:
            await smoke.main(write=False)
        assert exc.value.code == 1
        assert "FAIL health with bearer key" in capsys.readouterr().out
    finally:
        await server.close()


async def test_a_store_that_accepts_any_key_fails_the_run(smoke, monkeypatch, capsys):
    """The wrong-key check must fail loudly if auth stops being enforced."""
    async def handler(request):
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setenv("TAGBOT_STORE_URL", str(server.make_url("")).rstrip("/"))
    monkeypatch.setenv("TAGBOT_STORE_KEY", KEY)
    try:
        with pytest.raises(SystemExit) as exc:
            await smoke.main(write=False)
        assert exc.value.code == 1
        assert "FAIL wrong key rejected" in capsys.readouterr().out
    finally:
        await server.close()


async def test_missing_configuration_is_reported(smoke, monkeypatch):
    """Running with no TAGBOT_STORE_* set should explain itself, not traceback."""
    monkeypatch.delenv("TAGBOT_STORE_URL", raising=False)
    monkeypatch.delenv("TAGBOT_STORE_KEY", raising=False)
    from src.services.store_client import StoreError

    with pytest.raises(StoreError, match="TAGBOT_STORE_URL"):
        await smoke.main(write=False)


def test_script_compiles_under_the_current_interpreter():
    assert subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)], capture_output=True).returncode == 0
