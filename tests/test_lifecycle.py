"""Graceful shutdown on SIGINT and SIGTERM.

Several tests deliver a real SIGTERM, so the handler is exercised as installed
on the process rather than merely as constructed. Every one of them asserts
that the handler was claimed first: delivering SIGTERM at its default
disposition would terminate the test runner itself, and a CI job that vanishes
without a failure report is the worst possible way to learn about a regression
in install_handlers.
"""

import asyncio
import os
import signal
from unittest.mock import Mock

import pytest

from src.services import lifecycle, tasks
from src.services.lifecycle import serve


class FakeGateway:
    """Stands in for discord.Client: start() runs until the connection ends."""

    def __init__(self, *, fails=None):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.token = None
        self.cancelled = False
        self._fails = fails

    async def start(self, token):
        self.token = token
        self.started.set()
        if self._fails is not None:
            raise self._fails
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


@pytest.fixture(autouse=True)
def no_leftover_tasks():
    tasks._running.clear()
    tasks.reopen()
    yield
    tasks._running.clear()
    tasks.reopen()


async def run_until_started(bot, **kwargs):
    """Start serve() in the background and wait for the gateway to be up.

    Also refuses to hand back a serve() that has not taken over SIGTERM, so a
    regression in install_handlers fails this assertion instead of letting a
    later os.kill() reach the default disposition and kill pytest.
    """
    serving = asyncio.create_task(serve(bot, "test-token", **kwargs))
    await asyncio.wait_for(bot.started.wait(), 1)
    assert sigterm_is_claimed(), "serve() did not install a SIGTERM handler"
    return serving


def sigterm_is_claimed():
    """True when this process has a handler installed for SIGTERM.

    asyncio installs its own C-level handler and keeps the Python-visible
    disposition at SIG_DFL, so the loop's internal registry is what tells us
    whether a delivered SIGTERM would be caught or would kill the process.
    """
    loop = asyncio.get_event_loop()
    return signal.SIGTERM in getattr(loop, "_signal_handlers", {})


# --------------------------------------------------------------------------- #
# Signal handling
# --------------------------------------------------------------------------- #


async def test_a_real_sigterm_reaches_the_handler():
    """The handler is installed on the process, not just constructed."""
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed = lifecycle.install_handlers(loop, stopping)
    # Only deliver the signal if we really took it over; otherwise the default
    # disposition would kill the test runner.
    assert signal.SIGTERM in installed
    try:
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(stopping.wait(), 2)
    finally:
        lifecycle.remove_handlers(loop, installed)


async def test_both_shutdown_signals_are_claimed_and_released():
    loop = Mock()
    stopping = asyncio.Event()
    assert lifecycle.install_handlers(loop, stopping) == [signal.SIGINT, signal.SIGTERM]
    assert [call.args[0] for call in loop.add_signal_handler.call_args_list] == \
        [signal.SIGINT, signal.SIGTERM]

    lifecycle.remove_handlers(loop, [signal.SIGINT, signal.SIGTERM])
    assert [call.args[0] for call in loop.remove_signal_handler.call_args_list] == \
        [signal.SIGINT, signal.SIGTERM]


@pytest.mark.parametrize("refusal", [NotImplementedError, RuntimeError, ValueError])
async def test_a_platform_without_signal_handlers_still_runs(refusal, monkeypatch):
    """Windows and non-main-thread loops cannot take signals; warn, do not crash."""
    logged = Mock()
    monkeypatch.setattr(lifecycle, "log", logged)
    loop = Mock(add_signal_handler=Mock(side_effect=refusal("unsupported")))

    assert lifecycle.install_handlers(loop, asyncio.Event()) == []
    assert logged.warning.call_count == len(lifecycle.SHUTDOWN_SIGNALS)


async def test_removing_a_handler_the_platform_rejects_is_survivable():
    loop = Mock(remove_signal_handler=Mock(side_effect=NotImplementedError))
    lifecycle.remove_handlers(loop, [signal.SIGTERM])  # must not raise


async def test_a_repeated_signal_does_not_restart_the_shutdown(monkeypatch):
    logged = Mock()
    monkeypatch.setattr(lifecycle, "log", logged)
    stopping = asyncio.Event()

    lifecycle._requested(signal.SIGTERM, stopping)
    assert stopping.is_set()
    lifecycle._requested(signal.SIGINT, stopping)

    # Still shutting down, with the second request noted rather than acted on.
    assert stopping.is_set()
    assert "again" in str(logged.warning.call_args)


# --------------------------------------------------------------------------- #
# serve()
# --------------------------------------------------------------------------- #


async def test_a_signal_closes_the_gateway():
    bot = FakeGateway()
    serving = await run_until_started(bot)

    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(serving, 2)

    assert bot.token == "test-token"
    assert bot.cancelled, "the gateway task must be stopped, not left running"


async def test_pending_work_finishes_before_the_connection_closes():
    """The whole point: an in-flight sync is not cut off by the signal."""
    finished = []
    writing, keep_writing = asyncio.Event(), asyncio.Event()

    async def sync_a_guild():
        writing.set()
        await keep_writing.wait()
        finished.append("sync")

    bot = FakeGateway()
    serving = await run_until_started(bot)
    tasks.spawn(sync_a_guild(), name="startup-sync")
    await asyncio.wait_for(writing.wait(), 1)

    os.kill(os.getpid(), signal.SIGTERM)
    # Shutdown is now waiting on the sync, so the gateway is still open.
    await asyncio.sleep(0.05)
    assert not serving.done() and not bot.cancelled

    keep_writing.set()
    await asyncio.wait_for(serving, 2)
    assert finished == ["sync"] and bot.cancelled


async def test_work_that_overruns_the_grace_period_is_cancelled():
    """A stuck task must not hold the process open past its grace period."""
    stuck = asyncio.Event()

    async def never_finishes():
        stuck.set()
        await asyncio.Event().wait()

    bot = FakeGateway()
    serving = await run_until_started(bot, drain_timeout=0.05)
    task = tasks.spawn(never_finishes(), name="wedged-sync")
    await asyncio.wait_for(stuck.wait(), 1)

    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(serving, 2)

    assert task.cancelled()
    assert tasks.pending() == 0


async def test_a_connection_that_ends_by_itself_reports_why():
    """A refused intent or a bad token must still reach main's handlers."""
    bot = FakeGateway(fails=RuntimeError("gateway closed 4014"))
    with pytest.raises(RuntimeError, match="4014"):
        await serve(bot, "test-token")


async def test_a_failed_connection_does_not_claim_a_graceful_stop():
    """Failing to connect is not a shutdown: pending work keeps its gate open."""
    bot = FakeGateway(fails=RuntimeError("connection refused"))
    with pytest.raises(RuntimeError):
        await serve(bot, "test-token")
    assert tasks._closing is False


async def test_a_connection_that_dies_during_the_drain_is_logged(monkeypatch):
    """Nothing awaits the gateway by then, so the error would otherwise vanish."""
    logged = Mock()
    monkeypatch.setattr(lifecycle, "log", logged)
    dropped = asyncio.Event()

    class DiesMidDrain(FakeGateway):
        async def start(self, token):
            self.started.set()
            await dropped.wait()
            raise RuntimeError("gateway closed 1006 mid-drain")

    # Dropping the connection from inside drain() is what makes the ordering
    # deterministic. Doing it any earlier would be the "ended on its own" path,
    # which is a different test.
    real_drain = tasks.drain

    async def drop_the_connection_then_drain(timeout):
        dropped.set()
        await asyncio.sleep(0.02)  # let the gateway task actually raise
        return await real_drain(timeout)

    monkeypatch.setattr(tasks, "drain", drop_the_connection_then_drain)

    bot = DiesMidDrain()
    serving = await run_until_started(bot)
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(serving, 2)

    assert "1006 mid-drain" in str(logged.error.call_args)


async def test_an_error_the_caller_already_sees_is_not_logged_twice(monkeypatch):
    logged = Mock()
    monkeypatch.setattr(lifecycle, "log", logged)
    bot = FakeGateway(fails=RuntimeError("login failed"))

    with pytest.raises(RuntimeError, match="login failed"):
        await serve(bot, "test-token")

    # main() reports this one; a second line would imply two failures.
    assert logged.error.call_count == 0


async def test_the_next_connection_accepts_work_again():
    """The limited-mode retry must not inherit the first attempt's closed gate."""
    first = FakeGateway()
    serving = await run_until_started(first)
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(serving, 2)
    assert tasks._closing is True

    second = FakeGateway()
    serving = await run_until_started(second)
    try:
        assert tasks._closing is False
        assert tasks.spawn(asyncio.sleep(0), name="post-restart") is not None
    finally:
        second.release.set()
        await asyncio.wait_for(serving, 2)


async def test_the_signals_are_released_when_serve_returns():
    """Left installed, a handler would resolve a dead Event and swallow Ctrl-C."""
    loop = asyncio.get_running_loop()
    removed = []
    original = loop.remove_signal_handler
    bot = FakeGateway()

    def track(sig):
        removed.append(sig)
        return original(sig)

    loop.remove_signal_handler = track
    try:
        serving = await run_until_started(bot)
        bot.release.set()
        await asyncio.wait_for(serving, 2)
    finally:
        loop.remove_signal_handler = original
    assert removed == [signal.SIGINT, signal.SIGTERM]


async def test_a_connection_closing_on_its_own_is_not_a_drain():
    """Discord dropping the connection should not cancel background work."""
    bot = FakeGateway()
    serving = await run_until_started(bot)
    bot.release.set()
    await asyncio.wait_for(serving, 2)
    assert tasks._closing is False


async def test_serve_leaves_closing_the_bot_to_its_caller():
    """close() has to happen after the drain, so serve() must not do it."""
    closed = []
    bot = FakeGateway()
    bot.close = lambda: closed.append(True)

    serving = await run_until_started(bot)
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(serving, 2)

    assert closed == [], "serve() closed the bot the caller's async with owns"


async def test_start_drives_the_gateway_through_serve(monkeypatch):
    """main.start runs serve() inside `async with bot`, so close() comes last."""
    import main
    from src.services.environment import Config

    order = []

    class Client:
        async def __aenter__(self):
            order.append("open")
            return self

        async def __aexit__(self, *exc):
            order.append("close")

    client = Client()
    monkeypatch.setattr(main, "build_bot", lambda config, members: client)

    async def fake_serve(served, token):
        order.append("serve")
        assert served is client and token == "x" * 60

    monkeypatch.setattr(main, "serve", fake_serve)
    await main.start(Config(discord_token="x" * 60, environment="test",
                            sync_on_start=False, sync_delay_ms=0), members=True)

    assert order == ["open", "serve", "close"], "the drain must precede close()"
