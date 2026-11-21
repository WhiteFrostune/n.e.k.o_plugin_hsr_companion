"""Bounded in-memory timings for the observer and its OCR adapter."""
import asyncio
import hashlib
import re
import time
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

from .prepare_trace import request_id as prepare_request_id

_IDS = ContextVar("hsr_vision_trace_ids", default=None)
_ID_NUMBERS = ("generation", "epoch", "serial", "job_id", "frame_seq", "verification_frame_seq")


def public_correlation(value):
    """Opaque IDs/counts only; never accept a name, query, path, or game value."""
    value = value if isinstance(value, dict) else {}
    result = {k: value[k] for k in _ID_NUMBERS
              if type(value.get(k)) is int and 0 <= value[k] <= 2**53}
    if isinstance(value.get("request_id"), str) and re.fullmatch(r"[a-f0-9]{32}", value["request_id"]):
        result["request_id"] = value["request_id"]
    return result


def correlation():
    return public_correlation(_IDS.get())


def bind_trace(**values):
    """Bind an existing producer ID, not a new observation or scheduling token."""
    _IDS.set({**correlation(), **public_correlation(values)})


def traced_operation(stage):
    """Measure an existing await chain; no task, retry, timeout, or extra IO."""
    def decorate(func):
        @wraps(func)
        async def wrapped(owner, *args, **kwargs):
            ids = correlation()
            active = getattr(owner, "_active_refresh_trace", None) or {}
            request = prepare_request_id(owner) or (active.get("correlation") or {}).get("request_id")
            if stage == "manual_refresh_wait":
                stamp = (getattr(owner, "_manual_refresh_status", None) or {}).get("requested_at")
                request = hashlib.sha256(stamp.encode()).hexdigest()[:32] if isinstance(stamp, str) else None
            if request is not None:
                ids.update(public_correlation({"request_id": request}))
            for key, attribute in (("generation", "_answer_query_generation"),
                                   ("epoch", "_observation_epoch"), ("serial", "_observation_serial")):
                ids.setdefault(key, getattr(owner, attribute, 0))
            with measure(owner, stage, **public_correlation(ids)):
                return await func(owner, *args, **kwargs)
        return wrapped
    return decorate


def record(owner, stage, **details):
    try:
        events = owner.__dict__.setdefault("_vision_events", deque(maxlen=48))
        events.append({"stage": stage, "at": time.monotonic(), **correlation(), **details})
    except Exception:
        pass  # Optional diagnostics must not replace a result or its exception.


def record_candidate(owner, stage, analysis, **details):
    """Count pipeline output; never retain parsed text, values, or pictures."""
    if not isinstance(analysis, dict):
        return
    screen = analysis.get("screen_facts")
    facts = screen.get("facts", []) if isinstance(screen, dict) else []
    facts = [f for f in facts if isinstance(f, dict)] if isinstance(facts, list) else []
    characters = analysis.get("characters")
    record(owner, stage, fact_count=len(facts),
           character_count=len(characters) if isinstance(characters, list) else 0,
           stat_count=sum(f.get("kind") == "stat" for f in facts),
           main_stats=sum(f.get("kind") == "main_stat" for f in facts),
           sub_stats=sum(f.get("kind") == "sub_stat" for f in facts), **details)


def record_relic_stage(owner, stage, analysis, job, *, outcome="returned", **details):
    if not job:
        return
    rows = (analysis.get("screen_facts") or {}).get("facts") or []
    record(owner, stage, **job, outcome=outcome,
           main_stats=sum(f.get("kind") == "main_stat" for f in rows),
           sub_stats=sum(f.get("kind") == "sub_stat" for f in rows), **details)


@contextmanager
def measure(owner, stage, **details):
    token = _IDS.set({**correlation(), **public_correlation(details)})
    started = time.monotonic()
    record(owner, stage, outcome="started", **details)
    error = None
    try:
        yield
    except (Exception, asyncio.CancelledError) as exc:
        error = type(exc).__name__
        raise
    finally:
        record(owner, stage, outcome="error" if error else "returned",
               seconds=round(time.monotonic() - started, 3), error_type=error, **details)
        _IDS.reset(token)


def public_trace(owner):
    now = time.monotonic()
    events = []
    for source in (owner, getattr(owner, "_vision", None)):
        trace = getattr(source, "_vision_events", None)
        if isinstance(trace, deque):
            events.extend(trace.copy())
    # All writers use fixed labels, counts, durations and exception class names.
    return [{"age_seconds": round(max(0., now - event["at"]), 3),
             **{key: value for key, value in event.items() if key not in {"at", "request_id", *_ID_NUMBERS}},
             **public_correlation(event)}
            for event in sorted(events, key=lambda event: event["at"])[-48:]]


def frame_comparison_stats(owner):
    count, seconds, maximum = getattr(owner, "_frame_comparison_stats", (0, 0., 0.))
    return {"calls": count, "total_seconds": seconds, "max_seconds": maximum}
