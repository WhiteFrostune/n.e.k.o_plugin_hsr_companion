"""Test-only clocks: advance scenario time, not time spent on a loaded CPU.

Module-local proxies leave stdlib time/asyncio and other tests untouched.
Native workers remain real threads. Real time is used only for deadlock guards,
never to decide whether a product deadline or scenario assertion succeeds.
"""
import asyncio
import threading
import time
from types import SimpleNamespace


class ManualClock:
    def __init__(self, start=1000.):
        self._now = start
        self._lock = threading.Lock()

    def monotonic(self):
        with self._lock:
            return self._now

    def advance(self, seconds):
        if seconds < 0:
            raise ValueError("scenario time cannot run backwards")
        with self._lock:
            self._now += seconds

    def install(self, monkeypatch, *modules, drive_sleep=False, expire_events=False):
        for module in modules:
            monkeypatch.setattr(module, "time", SimpleNamespace(
                monotonic=self.monotonic, time=time.time))
            if hasattr(module, "asyncio"):
                monkeypatch.setattr(module, "asyncio", ScheduledAsyncio(
                    self, drive_sleep=drive_sleep, expire_events=expire_events))


class ScheduledAsyncio:
    def __init__(self, clock, *, drive_sleep=False, expire_events=False):
        self.clock = clock
        self.drive_sleep = drive_sleep
        self.expire_events = expire_events

    def __getattr__(self, name):
        return getattr(asyncio, name)

    async def sleep(self, seconds):
        if self.drive_sleep:
            self.clock.advance(seconds)
            await asyncio.sleep(0)
        else:
            # Polling yields a turn; OCR costs are advanced by the backend.
            await asyncio.sleep(0)

    async def wait_for(self, awaitable, timeout):
        if timeout is None:
            return await awaitable
        deadline = self.clock.monotonic() + timeout
        task = asyncio.ensure_future(awaitable)
        is_event = getattr(awaitable, "cr_code", None) is asyncio.Event.wait.__code__
        try:
            # Let already available results run before scheduling an idle wait.
            await asyncio.sleep(0)
            if self.expire_events and is_event and not task.done():
                self.clock.advance(timeout)
            while not task.done():
                if self.clock.monotonic() >= deadline:
                    raise TimeoutError
                # Real scheduler/executor can make progress without consuming
                # the scenario's logical request budget.
                await asyncio.wait({task}, timeout=.001)
            if self.clock.monotonic() >= deadline:
                raise TimeoutError
            return task.result()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


async def deadlock_guard(awaitable):
    """Infrastructure guard only; the scenario asserts its original deadline."""
    return await asyncio.wait_for(awaitable, timeout=30)
