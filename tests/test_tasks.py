"""Supervision of fire-and-forget background tasks.

asyncio keeps only a weak reference to a running task, so anything started with
a bare create_task can be collected mid-run and its exception never surfaces.
These cover the strong reference and the error reporting that replace it.
"""

import asyncio
from unittest.mock import Mock

import pytest

from src.services import tasks


@pytest.fixture(autouse=True)
def no_leftover_tasks():
    tasks._running.clear()
    yield
    tasks._running.clear()


async def settle():
    """Let a finished task's done-callback run."""
    for _ in range(3):
        await asyncio.sleep(0)


async def test_a_running_task_is_held_then_released():
    started, release = asyncio.Event(), asyncio.Event()

    async def work():
        started.set()
        await release.wait()

    task = tasks.spawn(work(), name="held")
    await asyncio.wait_for(started.wait(), 1)
    assert tasks.pending() == 1 and task in tasks._running

    release.set()
    await task
    await settle()
    assert tasks.pending() == 0


async def test_a_failing_task_is_logged_rather_than_lost(monkeypatch):
    logged = Mock()
    monkeypatch.setattr(tasks, "log", logged)

    async def boom():
        raise RuntimeError("background failure")

    task = tasks.spawn(boom(), name="boom")
    await asyncio.gather(task, return_exceptions=True)
    await settle()

    assert tasks.pending() == 0
    # Retrieving the exception is also what stops asyncio's own
    # "exception was never retrieved" warning at collection time.
    assert isinstance(task.exception(), RuntimeError)
    assert "boom" in str(logged.error.call_args)


async def test_a_cancelled_task_is_noted_without_an_error(monkeypatch):
    logged = Mock()
    monkeypatch.setattr(tasks, "log", logged)

    async def forever():
        await asyncio.Event().wait()

    task = tasks.spawn(forever(), name="forever")
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await settle()

    assert tasks.pending() == 0
    logged.error.assert_not_called()
    assert "forever" in str(logged.info.call_args)


async def test_each_task_is_tracked_independently():
    release = asyncio.Event()

    async def work():
        await release.wait()

    spawned = [tasks.spawn(work(), name=f"job-{i}") for i in range(3)]
    await asyncio.sleep(0)
    assert tasks.pending() == 3

    release.set()
    await asyncio.gather(*spawned)
    await settle()
    assert tasks.pending() == 0
