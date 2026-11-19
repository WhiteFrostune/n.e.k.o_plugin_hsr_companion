"""Read only the target character's latest user-utterance timestamp via SDK.

The bus can carry text; only a bounded digest is returned when requested.
This is a submission guard, not a host playback receipt or retraction API.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import threading
import time
import weakref
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor

from plugin.sdk.plugin import Err, Ok

# Some supported SDK versions return an awaitable but perform blocking IPC
# inside it. Isolate the PUBLIC facade, including awaiting its result, from the
# plugin loop. The worker pool is shared; the pending-read state is per bus so
# one owner's stuck transport cannot block another question.
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="hsr-activity")
_states_lock = threading.Lock()
_states = weakref.WeakKeyDictionary()
_STATE_ATTRIBUTE = "_hsr_companion_activity_state"


class _ActivityState:
    __slots__ = ("lock", "inflight", "abandoned_at")

    def __init__(self):
        self.lock = threading.Lock()
        self.inflight = None
        self.abandoned_at = 0.0


def _state_for(bus):
    with _states_lock:
        try:
            state = _states.get(bus)
            if state is None:
                state = _ActivityState()
                _states[bus] = state
            return state
        except TypeError:
            pass
    # Some compatibility buses (e.g. SimpleNamespace) are unhashable and not
    # weak-referenceable. Keying those by id() lets a recycled address inherit
    # a dead bus's stuck read, so keep the state on the bus instead. If the bus
    # cannot store it either, each call reads independently rather than
    # blocking a later question on an unrelated review.
    try:
        state = getattr(bus, _STATE_ATTRIBUTE, None)
        if state is None:
            state = _ActivityState()
            setattr(bus, _STATE_ATTRIBUTE, state)
        return state
    except (AttributeError, TypeError):
        return _ActivityState()


def user_record(record):
    dump = getattr(record, "dump", None)
    value = dump() if callable(dump) else record
    if not isinstance(value, Mapping):
        return None
    # SDK memory records may wrap a host record under `value`, rather than
    # flattening its dump. Normalize this one public wrapper, boundedly.
    if "value" in value and "raw" not in value and "type" not in value:
        inner = value["value"]
        dump = getattr(inner, "dump", None)
        value = dump() if callable(dump) else inner
        if not isinstance(value, Mapping):
            return None
    raw = value.get("raw", value)
    return ({**raw, "_ts": raw.get("_ts", value.get("timestamp"))}
            if isinstance(raw, Mapping) else None)


def user_timestamp(record, *, target: str, now: float) -> float | None:
    raw = user_record(record)
    if raw is None or raw.get("type") != "user_message" or raw.get("lanlan") != target:
        return None
    try:
        stamp = raw.get("_ts")
        if isinstance(stamp, bool):
            return None
        timestamp = float(stamp)
    except (TypeError, ValueError):
        return None
    return timestamp if math.isfinite(timestamp) and 0 <= now - timestamp <= 120 else None


async def latest_user_activity(bus, *, target: str, now: float | None = None, limit: int = 1,
                               transport_timeout: float = .25, wait_timeout: float = .35, include_identity=False):
    get = getattr(getattr(bus, "memory", None), "get", None)
    if not callable(get) or not target:
        return "unsupported", None

    async def read():
        records = get(bucket_id=target, limit=min(8, max(1, limit)), timeout=transport_timeout)
        if inspect.isawaitable(records):
            records = await records
        if isinstance(records, Err):
            raise RuntimeError("Activity read failed")
        if isinstance(records, Ok):
            records = records.value
        if isinstance(records, Mapping):
            records = records.get("history", [])
        records = list(records)
        from .pending_reply import UserActivity, message_digest
        stamps = [(stamp, user_record(record)) for record in records
                  if (stamp := user_timestamp(record, target=target, now=time.time() if now is None else now)) is not None]
        if not stamps:
            return None
        stamp, raw = max(stamps, key=lambda row: row[0])
        digests = {message_digest(r.get("content")) for ts, r in stamps if ts == stamp}
        # Same-time conflicting events are ambiguous, not reusable consent.
        return UserActivity(stamp, next(iter(digests)) if len(digests) == 1 else None) if include_identity else stamp

    def blocking_read():
        return asyncio.run(read())

    state = _state_for(bus)
    with state.lock:
        if state.inflight is not None and not state.inflight.done():
            if state.abandoned_at:
                # The prior caller already timed out; let the next question
                # start its own read on the shared worker pool.
                state.inflight = None
            else:
                return "unavailable", None
        state.inflight = _executor.submit(blocking_read)
        state.abandoned_at = 0.0
        future = state.inflight
    try:
        # Shield leaves this worker occupied until the transport really exits.
        # A timed-out result is never reused for a different query.
        wrapped = asyncio.wrap_future(future)
        wrapped.add_done_callback(lambda task: None if task.cancelled() else task.exception())
        return "available", await asyncio.wait_for(asyncio.shield(wrapped), timeout=wait_timeout)
    except asyncio.CancelledError:
        with state.lock:
            if state.inflight is future and not future.done():
                state.abandoned_at = time.monotonic()
        raise
    except Exception:
        import sys
        import traceback
        traceback.print_exc(file=sys.stderr)
        with state.lock:
            if state.inflight is future and not future.done():
                state.abandoned_at = time.monotonic()
        # Missing/failed optional activity observation must not send blindly.
        return "unavailable", None
