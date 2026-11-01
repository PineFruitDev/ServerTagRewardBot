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
    tasks.reopen()
    yield
    tasks._running.clear()
    tasks.reopen()


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


# --------------------------------------------------------------------------- #
# Draining at shutdown. A sync writes streaks and assigns roles one member at
# a time, so these cover letting one land rather than cutting it in half.
# --------------------------------------------------------------------------- #


async def test_draining_nothing_is_immediate():
    assert await tasks.drain(timeout=5) == 0


async def test_draining_waits_for_work_in_flight():
    done = []
    started, release = asyncio.Event(), asyncio.Event()

    async def work():
        started.set()
        await release.wait()
        done.append("finished")

    tasks.spawn(work(), name="startup-sync")
    await asyncio.wait_for(started.wait(), 1)

    draining = asyncio.create_task(tasks.drain(timeout=5))
    await asyncio.sleep(0)
    assert not draining.done(), "drain returned while a task was still running"

    release.set()
    assert await asyncio.wait_for(draining, 1) == 0
    assert done == ["finished"]


async def test_draining_cancels_work_that_overruns_the_grace_period(monkeypatch):
    logged = Mock()
    monkeypatch.setattr(tasks, "log", logged)

    async def wedged():
        await asyncio.Event().wait()

    task = tasks.spawn(wedged(), name="wedged-sync")
    await asyncio.sleep(0)

    assert await tasks.drain(timeout=0.05) == 1
    assert task.cancelled()
    await settle()
    assert tasks.pending() == 0
    assert "wedged-sync" in str(logged.warning.call_args)


async def test_draining_reports_only_the_tasks_it_had_to_cancel():
    release = asyncio.Event()

    async def quick():
        await asyncio.sleep(0)

    async def slow():
        await release.wait()

    tasks.spawn(quick(), name="quick")
    tasks.spawn(slow(), name="slow")
    await asyncio.sleep(0)

    assert await tasks.drain(timeout=0.05) == 1


async def test_new_work_is_declined_once_draining_starts(monkeypatch):
    logged = Mock()
    monkeypatch.setattr(tasks, "log", logged)
    await tasks.drain(timeout=0)

    # A /setup landing mid-shutdown must not start a sync we would cancel.
    assert tasks.spawn(asyncio.sleep(0), name="late-setup-sync") is None
    assert tasks.pending() == 0
    assert "late-setup-sync" in str(logged.warning.call_args)


async def test_declining_work_does_not_leave_an_unawaited_coroutine(recwarn):
    """A dropped coroutine would otherwise warn, which is the only trace of it."""
    await tasks.drain(timeout=0)

    async def work():
        pass  # pragma: no cover - declined before it can run

    coro = work()
    assert tasks.spawn(coro, name="declined") is None
    assert not [w for w in recwarn if "never awaited" in str(w.message)]


async def test_work_is_accepted_again_after_reopening():
    await tasks.drain(timeout=0)
    tasks.reopen()

    task = tasks.spawn(asyncio.sleep(0), name="after-reopen")
    assert task is not None
    await task
