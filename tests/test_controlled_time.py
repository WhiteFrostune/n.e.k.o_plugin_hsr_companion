"""The test scheduler must not turn a missed logical deadline into success."""
import asyncio
import time
from types import SimpleNamespace

import pytest
from plugin.plugins.hsr_companion.tests.controlled_time import ManualClock, ScheduledAsyncio


def test_clock_patching_is_module_local(monkeypatch):
    original = time.monotonic
    module = SimpleNamespace(time=time, asyncio=asyncio)
    clock = ManualClock()
    clock.install(monkeypatch, module, drive_sleep=True)
    assert time.monotonic is original
    assert asyncio.sleep is not module.asyncio.sleep

    async def run():
        await module.asyncio.sleep(.25)
    asyncio.run(run())
    assert clock.monotonic() == 1000.25


@pytest.mark.parametrize("elapsed,expired", [(7.149, False), (7.15, True), (8., True)])
def test_original_deadline_still_rejects_late_work(elapsed, expired):
    clock = ManualClock()
    scheduler = ScheduledAsyncio(clock)

    async def result():
        clock.advance(elapsed)
        return "verified"

    async def run():
        if expired:
            with pytest.raises(TimeoutError):
                await scheduler.wait_for(result(), timeout=7.15)
        else:
            assert await scheduler.wait_for(result(), timeout=7.15) == "verified"
    asyncio.run(run())


def test_deadline_cancels_pending_task_and_drains_it():
    clock = ManualClock()
    scheduler = ScheduledAsyncio(clock)
    stopped = []

    async def pending():
        try:
            clock.advance(1)
            await asyncio.Event().wait()
        finally:
            stopped.append(True)

    async def run():
        with pytest.raises(TimeoutError):
            await scheduler.wait_for(pending(), timeout=1)
    asyncio.run(run())
    assert stopped == [True]
