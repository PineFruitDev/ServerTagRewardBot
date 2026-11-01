"""Graceful shutdown on SIGINT and SIGTERM.

A container stop, a `systemctl restart` or a host redeploy arrives as SIGTERM,
and its default disposition kills the process outright. Nothing is corrupted by
that (every write is a committed store request), but the gateway connection is
dropped without a close frame, the pooled HTTPS session to tagbot-store is
abandoned, and a sync in flight stops partway through a guild's members, so the
next start has to rediscover what the last one was in the middle of.

serve() replaces that with one cooperative path for both signals: stop
accepting new background work, let what is already running finish inside a
grace period, then let the caller close the gateway and the store session on
the way out. Closing is deliberately left to the caller's `async with`, because
it has to happen after the drain: the work being waited on still needs both.
"""

import asyncio
import signal
from typing import Protocol

from src.services import tasks
from src.services.logger import get_logger

log = get_logger("Lifecycle")

#: SIGINT is Ctrl-C at a terminal. SIGTERM is what Docker, systemd and most
#: process supervisors send first, with SIGKILL following if we take too long.
SHUTDOWN_SIGNALS: tuple[signal.Signals, ...] = (signal.SIGINT, signal.SIGTERM)

#: Long enough for an in-flight member sync to land its store writes, short
#: enough to leave room for the close itself inside a supervisor's own patience.
#: Docker allows 10s by default before SIGKILL, so draining for the full window
#: would risk being killed partway through the close this exists to perform.
DRAIN_TIMEOUT = 5.0


class Connection(Protocol):
    """The part of the Discord client serve() drives."""

    async def start(self, token: str) -> None: ...


async def serve(bot: Connection, token: str, *, drain_timeout: float = DRAIN_TIMEOUT) -> None:
    """Run the gateway until it ends by itself or a shutdown signal arrives.

    Re-raises whatever ended the connection, so the caller can still tell a
    refused privileged intent from a clean stop.
    """
    loop = asyncio.get_running_loop()
    stopping = asyncio.Event()
    # A previous connection may have drained; this one accepts work again.
    tasks.reopen()
    installed = install_handlers(loop, stopping)

    gateway = asyncio.create_task(bot.start(token), name="gateway")
    signalled = asyncio.create_task(stopping.wait(), name="shutdown-signal")
    raised_to_caller = False
    try:
        await asyncio.wait({gateway, signalled}, return_when=asyncio.FIRST_COMPLETED)
        if gateway.done():
            # Ended on its own: a login failure, a refused intent, or a close
            # the caller needs to see. Surface it rather than reporting a
            # graceful stop we did not perform.
            raised_to_caller = True
            await gateway
            return
        cancelled = await tasks.drain(drain_timeout)
        log.info("serve - Drained background work (%d cancelled); closing the connection", cancelled)
    finally:
        # Cancel in every exit path, including the signal one: bot.start() runs
        # until the connection closes, and nothing else will stop it.
        signalled.cancel()
        gateway.cancel()
        await asyncio.gather(gateway, signalled, return_exceptions=True)
        if not raised_to_caller:
            _report_a_late_failure(gateway)
        remove_handlers(loop, installed)


def _report_a_late_failure(gateway: asyncio.Task[None]) -> None:
    """Log a connection that died during the drain rather than losing it.

    Nothing is waiting on the gateway task by this point, so an exception it
    raised inside the grace period would otherwise be swallowed by the gather
    above and reported as a clean shutdown.
    """
    if gateway.cancelled():
        return
    failure = gateway.exception()
    if failure is not None:
        log.error("serve - The connection failed while shutting down: %s",
                  failure, exc_info=failure)


def install_handlers(loop: asyncio.AbstractEventLoop,
                     stopping: asyncio.Event) -> list[signal.Signals]:
    """Route the shutdown signals to `stopping`. Returns the ones installed."""
    installed = []
    for sig in SHUTDOWN_SIGNALS:
        try:
            loop.add_signal_handler(sig, _requested, sig, stopping)
        except (NotImplementedError, RuntimeError, ValueError) as exc:
            # Windows has no add_signal_handler, and neither does a loop running
            # off the main thread. The bot still works; it just cannot promise a
            # clean stop, which is worth one line rather than a crash.
            log.warning("install_handlers - Cannot handle %s here (%s); shutdown on that "
                        "signal will not be graceful", sig.name, type(exc).__name__)
            continue
        installed.append(sig)
    return installed


def remove_handlers(loop: asyncio.AbstractEventLoop, installed: list[signal.Signals]) -> None:
    """Hand the signals back, so a later one is not absorbed by a dead Event."""
    for sig in installed:
        try:
            loop.remove_signal_handler(sig)
        except (NotImplementedError, RuntimeError, ValueError):
            pass


def _requested(sig: signal.Signals, stopping: asyncio.Event) -> None:
    """Signal handler: record the request and return immediately.

    Runs on the event loop rather than in a real signal context, but it still
    only sets an Event. The waiting is done by serve(), which can await.
    """
    if stopping.is_set():
        # An impatient operator, or a supervisor escalating. Keep draining:
        # the alternative is exactly the half-finished stop being avoided.
        log.warning("_requested - %s received again; already shutting down", sig.name)
        return
    log.info("_requested - %s received; shutting down gracefully", sig.name)
    stopping.set()
