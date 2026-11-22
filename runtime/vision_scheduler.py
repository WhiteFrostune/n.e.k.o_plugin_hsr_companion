"""One fair async admission boundary above the native serial OCR adapter.

The producer owns recognition AND its final literal validation. Consumers
queue instead of racing the adapter's short busy timeout. Cancellation cannot
release a still-running native thread into a second inference call.
"""
from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from ..adapters.native_text import ObservationSuperseded
from .vision_trace import bind_trace, measure, record

OCR_DRAIN_SECONDS = 1.0
_RETAINED_WORKERS = ("_draining_vision_worker", "_content_revalidation_worker")


@dataclass(eq=False)
class ReviewDemand:
    deadline: float
    active: bool = True
    still_current: Callable[[], bool] | None = None

    def expired(self):
        return (not self.active or time.monotonic() >= self.deadline
                or self.still_current is not None and not self.still_current())


review_demand = ContextVar("ocr_review_demand", default=None)


@contextmanager
def query_demand(owner, deadline, *, regional):
    """Keep OCR reviews tied to their consumer, including the initial reuse check."""
    demand = ReviewDemand(deadline)
    token = review_demand.set(demand)
    if regional:
        owner._foreground_region_demand = demand
    try:
        yield demand
    finally:
        demand.active = False
        review_demand.reset(token)
        if regional and getattr(owner, "_foreground_region_demand", None) is demand:
            owner._foreground_region_demand = None


def foreground_region_waiting(owner):
    demand = getattr(owner, "_foreground_region_demand", None)
    return demand is not None and not demand.expired()


class VisionWorkerBusy(RuntimeError):
    def __init__(self):
        super().__init__("上一轮画面识别尚未退出，暂时无法开始新的识别。")


def retained_work_running(owner):
    return any((task := getattr(owner, name, None)) is not None and not task.done()
               for name in _RETAINED_WORKERS)


def _admission_blocked(owner):
    return (getattr(owner, "_vision_closing", False)
            or (getattr(owner, "_vision_drain_timed_out", False) and retained_work_running(owner)))


def query_yield_requested(owner, request_generation, scope):
    """Yield to an independent region read or a query outside this pass's scope.

    A request that actually needs the full producer may still join it. A scene
    pass covers scene questions, and a navigation pass also covers navigation.
    """
    if foreground_region_waiting(owner):
        return True
    if getattr(owner, "_visual_query_waiters", 0) <= 0:
        return False
    if getattr(owner, "_query_refresh_deadline", 0) <= time.monotonic():
        return False
    if getattr(owner, "_region_query_waiters", 0) > 0:
        # A region query reads one small block and returns it; it does not
        # restart the full pass, so even a full producer yields here.
        return True
    if getattr(owner, "_query_refresh_generation", 0) <= request_generation:
        return False
    if scope == "full":
        return False
    if scope == "scene":
        covered = getattr(owner, "_scene_query_waiters", 0)
    elif scope == "navigation":
        covered = (getattr(owner, "_scene_query_waiters", 0)
                   + getattr(owner, "_navigation_query_waiters", 0))
    else:
        return False
    return getattr(owner, "_visual_query_waiters", 0) > covered


async def drain_analysis_worker(owner):
    """Wait briefly without cancelling or forgetting a running native job."""
    workers = {name: task for name in _RETAINED_WORKERS if (task := getattr(owner, name, None)) is not None}
    if not workers:
        return True
    started = time.monotonic()
    done, pending = await asyncio.wait(workers.values(), timeout=OCR_DRAIN_SECONDS)
    for name, task in workers.items():
        if task in done:
            error = None if task.cancelled() else task.exception()
            if error is not None and not isinstance(error, ObservationSuperseded):
                record(owner, "ocr_drain_error", error_type=type(error).__name__)
            if getattr(owner, name, None) is task:
                setattr(owner, name, None)
    record(owner, "ocr_drain", outcome="timeout" if pending else "returned",
           seconds=round(time.monotonic() - started, 3), pending=len(pending))
    # A worker that finished during the wait could have been replaced by a
    # different retained job. The new handle still owns its backend.
    owner._vision_drain_timed_out = retained_work_running(owner)
    return not owner._vision_drain_timed_out


@asynccontextmanager
async def vision_turn(owner, role):
    if _admission_blocked(owner):
        raise VisionWorkerBusy()
    lock = getattr(owner, "_vision_schedule_lock", None)
    if lock is None:
        owner._vision_schedule_lock = lock = asyncio.Lock()
    queued_at = time.monotonic()
    owner._vision_schedule_waiters = getattr(owner, "_vision_schedule_waiters", 0) + 1
    acquired = False
    try:
        with measure(owner, "scheduler_wait", role=role):
            await lock.acquire()  # asyncio.Lock admission is FIFO, including queries.
        acquired = True
        owner._vision_schedule_role = role
        from .live_refresh import trace_refresh
        queue_seconds = round(time.monotonic() - queued_at, 3)
        trace_refresh(owner, "vision_turn_acquired", role=role, queue_seconds=queue_seconds)
        record(owner, "ocr_admission", role=role, seconds=queue_seconds)
        # wait_for/to_thread cancellation cannot kill native inference. Drain
        # the actual retained thread task, not its cancelled caller wrapper.
        with measure(owner, "scheduler_drain", role=role):
            if (_admission_blocked(owner) or not await drain_analysis_worker(owner)
                    or getattr(owner, "_vision_closing", False)):
                raise VisionWorkerBusy()
        with measure(owner, "scheduler_hold", role=role):
            yield
    finally:
        owner._vision_schedule_waiters -= 1
        if acquired:
            owner._vision_schedule_role = "idle"
            lock.release()


async def analyze_image(owner, payload):
    """Legacy user-selected screenshots share admission, not a second gate."""
    stop_generation = getattr(owner, "_vision_stop_generation", 0)

    def check_current():
        if stop_generation != getattr(owner, "_vision_stop_generation", 0):
            raise RuntimeError("读取期间画面已失效，请重新提交截图。")

    async with vision_turn(owner, "manual"):
        check_current()
        worker = asyncio.create_task(asyncio.to_thread(owner._vision.analyze, payload))
        owner._draining_vision_worker = worker
        try:
            result = await asyncio.shield(worker)
            check_current()
            return result
        finally:
            if worker.done() and getattr(owner, "_draining_vision_worker", None) is worker:
                owner._draining_vision_worker = None
                if not worker.cancelled():
                    worker.exception()


async def run_query_worker(owner, function, *args, deadline, prepare=None, **kwargs):
    """A cancelled waiter never releases or forgets native work still running."""
    stop = threading.Event()
    generation = getattr(owner, "_answer_query_generation", 0)
    epoch = getattr(owner, "_observation_epoch", 0)
    def cancelled():
        return (stop.is_set() or time.monotonic() >= deadline
                or getattr(owner, "_observer_stop_requested", False)
                or generation != getattr(owner, "_answer_query_generation", 0)
                or epoch != getattr(owner, "_observation_epoch", 0))
    async with vision_turn(owner, "query"):
        if cancelled():
            raise ObservationSuperseded()
        if prepare is not None:
            prepared = await prepare()
            if prepared is None:
                return None
            args, kwargs = prepared
            if cancelled():
                raise ObservationSuperseded()
        owner._vision_job_id = getattr(owner, "_vision_job_id", 0) + 1
        bind_trace(job_id=owner._vision_job_id)
        def read():
            with measure(owner, "region_ocr"):
                return function(*args, cancelled=cancelled, **kwargs)
        worker = asyncio.create_task(asyncio.to_thread(read))
        owner._draining_vision_worker = worker
        try:
            return await asyncio.shield(worker)
        finally:
            stop.set()
            if worker.done() and getattr(owner, "_draining_vision_worker", None) is worker:
                owner._draining_vision_worker = None
                if not worker.cancelled():
                    worker.exception()
