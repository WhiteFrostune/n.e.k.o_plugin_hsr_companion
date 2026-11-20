"""Bounded in-memory timings for the observer and its OCR adapter."""
import time
from collections import deque
from contextlib import contextmanager


def record(owner, stage, **details):
    events = owner.__dict__.setdefault("_vision_events", deque(maxlen=48))
    events.append({"stage": stage, "at": time.monotonic(), **details})


def record_relic_stage(owner, stage, analysis, job, *, outcome="returned", **details):
    if not job:
        return
    rows = (analysis.get("screen_facts") or {}).get("facts") or []
    record(owner, stage, **job, outcome=outcome,
           main_stats=sum(f.get("kind") == "main_stat" for f in rows),
           sub_stats=sum(f.get("kind") == "sub_stat" for f in rows), **details)


@contextmanager
def measure(owner, stage):
    started = time.monotonic()
    record(owner, stage, outcome="started")
    error = None
    try:
        yield
    except Exception as exc:
        error = type(exc).__name__
        raise
    finally:
        record(owner, stage, outcome="error" if error else "returned",
               seconds=round(time.monotonic() - started, 3), error_type=error)


def public_trace(owner):
    now = time.monotonic()
    events = []
    for source in (owner, getattr(owner, "_vision", None)):
        trace = getattr(source, "_vision_events", None)
        if isinstance(trace, deque):
            events.extend(trace.copy())
    # All writers use fixed labels, counts, durations and exception class names.
    return [{"age_seconds": round(max(0., now - event["at"]), 3),
             **{key: value for key, value in event.items() if key != "at"}}
            for event in sorted(events, key=lambda event: event["at"])[-48:]]


def frame_comparison_stats(owner):
    count, seconds, maximum = getattr(owner, "_frame_comparison_stats", (0, 0., 0.))
    return {"calls": count, "total_seconds": seconds, "max_seconds": maximum}
