"""Supervision for fire-and-forget background tasks.

asyncio keeps only a weak reference to a running task, so a task nobody else
holds can be garbage collected mid-run (see the asyncio.create_task docs).
spawn() parks the task in a module-level set until it finishes and logs the
exception if it raises, so a background sync can neither vanish silently nor
lose its error.
"""

import asyncio
from collections.abc import Coroutine
from typing import Any

from src.services.logger import get_logger

log = get_logger("Tasks")

_running: set[asyncio.Task[Any]] = set()


def spawn(coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task[Any]:
    """Run coro in the background, holding a reference until it completes."""
    task = asyncio.create_task(coro, name=name)
    _running.add(task)
    task.add_done_callback(_finish)
    return task


def pending() -> int:
    """How many supervised tasks are still running."""
    return len(_running)


def _finish(task: asyncio.Task[Any]) -> None:
    """Release the reference and surface anything the task raised."""
    _running.discard(task)
    if task.cancelled():
        log.info("_finish - %s was cancelled", task.get_name())
        return
    error = task.exception()
    if error is not None:
        log.error("_finish - %s failed: %s", task.get_name(), error, exc_info=error)
