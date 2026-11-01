"""Supervision for fire-and-forget background tasks.

asyncio keeps only a weak reference to a running task, so a task nobody else
holds can be garbage collected mid-run (see the asyncio.create_task docs).
spawn() parks the task in a module-level set until it finishes and logs the
exception if it raises, so a background sync can neither vanish silently nor
lose its error.

Holding those references is also what makes a clean shutdown possible: drain()
waits for the work in flight instead of letting the process exit through the
middle of it. See src/services/lifecycle.py for who calls it.
"""

import asyncio
from collections.abc import Coroutine
from typing import Any

from src.services.logger import get_logger, without_user

log = get_logger("Tasks")

_running: set[asyncio.Task[Any]] = set()

#: Set by drain(). New work is declined from then on, so a command that lands
#: during shutdown cannot start a multi-minute sync we are about to cancel.
_closing = False


def spawn(coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task[Any] | None:
    """Run coro in the background, holding a reference until it completes.

    Returns None without starting anything once drain() has begun.

    asyncio copies the current context into the new task, which is how work
    started by a command keeps that command's correlation id without anything
    being threaded through. The one field that must not come along is the user:
    this is the boundary where work for one person becomes work across a whole
    guild, and carrying their id would attribute every member the sync touches
    to whoever asked for it.
    """
    if _closing:
        # Closing the coroutine keeps Python from warning that it was never
        # awaited, which would otherwise be the only sign this was dropped.
        coro.close()
        log.warning("spawn - Declined %s: shutting down", name)
        return None
    with without_user():
        task = asyncio.create_task(coro, name=name)
    _running.add(task)
    task.add_done_callback(_finish)
    return task


def pending() -> int:
    """How many supervised tasks are still running."""
    return len(_running)


def reopen() -> None:
    """Accept new work again, undoing drain().

    Called when a connection is (re)established, so the limited-mode retry
    after a refused privileged intent does not inherit a closed gate.
    """
    global _closing
    _closing = False


async def drain(timeout: float) -> int:
    """Stop taking new work and let what is running finish.

    Background syncs write streaks and assign roles one member at a time, so
    cutting one off mid-pass leaves a guild half-synced: correct in the store,
    but needing a later pass to notice. Waiting the grace period out first
    costs a few seconds at shutdown and saves that.

    Returns how many tasks had to be cancelled for overrunning the timeout,
    so the caller can report whether the stop was clean.
    """
    global _closing
    _closing = True

    outstanding = list(_running)
    if not outstanding:
        return 0

    log.info("drain - Waiting up to %.1fs for %d background task(s)", timeout, len(outstanding))
    _, overrunning = await asyncio.wait(outstanding, timeout=timeout)
    if not overrunning:
        log.info("drain - Background work finished cleanly")
        return 0

    for task in overrunning:
        log.warning("drain - %s did not finish within %.1fs; cancelling it", task.get_name(), timeout)
        task.cancel()
    # Let each cancellation actually land before the caller closes the
    # connections these tasks are still holding.
    await asyncio.gather(*overrunning, return_exceptions=True)
    return len(overrunning)


def _finish(task: asyncio.Task[Any]) -> None:
    """Release the reference and surface anything the task raised."""
    _running.discard(task)
    if task.cancelled():
        log.info("_finish - %s was cancelled", task.get_name())
        return
    error = task.exception()
    if error is not None:
        log.error("_finish - %s failed: %s", task.get_name(), error, exc_info=error)
