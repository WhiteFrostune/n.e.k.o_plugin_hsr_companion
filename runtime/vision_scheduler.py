"""One fair async admission boundary above the native serial OCR adapter.

The producer owns recognition AND its final literal validation. Consumers
queue instead of racing the adapter's short busy timeout. Cancellation cannot
release a still-running native thread into a second inference call.
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager


@asynccontextmanager
async def vision_turn(owner, role):
    lock = getattr(owner, "_vision_schedule_lock", None)
    if lock is None:
        owner._vision_schedule_lock = lock = asyncio.Lock()
    queued_at = time.monotonic()
    owner._vision_schedule_waiters = getattr(owner, "_vision_schedule_waiters", 0) + 1
    acquired = False
    try:
        await lock.acquire()  # asyncio.Lock admission is FIFO, including queries.
        acquired = True
        owner._vision_schedule_role = role
        from .live_refresh import trace_refresh
        trace_refresh(owner, "vision_turn_acquired", role=role,
                      queue_seconds=round(time.monotonic() - queued_at, 3))
        # wait_for/to_thread cancellation cannot kill native inference. Drain
        # the actual retained thread task, not its cancelled caller wrapper.
        for name in ("_draining_vision_worker", "_content_revalidation_worker"):
            worker = getattr(owner, name, None)
            if worker is not None:
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():
                        raise
                except Exception:
                    pass  # Its original caller owns error reporting.
                if getattr(owner, name, None) is worker and worker.done():
                    setattr(owner, name, None)
        yield
    finally:
        owner._vision_schedule_waiters -= 1
        if acquired:
            owner._vision_schedule_role = "idle"
            lock.release()


async def analyze_image(owner, payload):
    """Legacy user-selected screenshots share admission, not a second gate."""
    async with vision_turn(owner, "manual"):
        worker = asyncio.create_task(asyncio.to_thread(owner._vision.analyze, payload))
        try:
            return await asyncio.shield(worker)
        finally:
            if not worker.done():
                owner._draining_vision_worker = worker
