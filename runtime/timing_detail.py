"""One bounded request timeline, independent of the legacy 48-event trace.

Timing only: no arguments, OCR text, images, exceptions' messages, IO or gates.
A native span already running when the question arrives is adopted, so its
remaining time can be separated from its total time and later lock waiting.
"""
import asyncio
import itertools
import re
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

MAX_EVENTS = 256
_LOCK = threading.RLock()
_SEQUENCE = itertools.count(1)
_CAPTURE = ContextVar("hsr_request_timing", default=None)
_PARENT = ContextVar("hsr_timing_parent", default=None)
_META = ContextVar("hsr_timing_meta", default=None)
STAGES = frozenset({
    "query_observation", "query_lock", "query_settings", "query_wait", "analysis_worker", "region_read",
    "cache_review", "region_ocr", "ocr_backend_setup", "ocr_call", "native_lock_wait", "checkpoint_review",
    "scheduler_wait", "scheduler_drain", "scheduler_hold", "region_packet", "region_capture",
    "region_crop_atlas", "region_page", "region_name_confirmation", "region_name_assignment",
    "region_controls", "region_guards", "region_snapshot_review", "region_receipt", "region_exit",
    "query_exit", "tool_exit", "handoff_demand", "handoff_signal", "handoff_completed", "review_work", "review_enqueue",
})
_ROLES = {"producer", "query", "delivery", "manual", "idle"}
_OPERATIONS = {"extract_text_with_boxes", "recognize_line", "read_character_detail", "analyze_live",
               "analyze", "analyze_scene", "analyze_navigation", "read_region", "verify_text_regions"}
_IDS = {"generation", "epoch", "serial", "job_id", "frame_seq", "verification_frame_seq"}


def _details(values):
    result = {k: v for k, v in values.items() if k in _IDS and type(v) is int and 0 <= v <= 2**53}
    value = values.get("request_id")
    if isinstance(value, str) and re.fullmatch(r"[a-f0-9]{32}", value):
        result["operation_request_id"] = value
    if values.get("role") in _ROLES:
        result["role"] = values["role"]
    if values.get("operation") in _OPERATIONS:
        result["operation"] = values["operation"]
    return result


class _Source:
    def __init__(self):
        self.active = {}
        self.sink = None


def _source(owner):
    if owner is None:
        return None
    state = getattr(owner, "_timing_detail_state", None)
    if not isinstance(state, _Source):
        state = _Source()
        owner._timing_detail_state = state
    return state


class _Timeline:
    def __init__(self, request_id):
        self.request_id = request_id if isinstance(request_id, str) and re.fullmatch(r"[a-f0-9]{32}", request_id) else None
        self.start = time.perf_counter()
        self.end = None
        self.events = []
        self.dropped = 0

    def add(self, event, when):
        if len(self.events) >= MAX_EVENTS:
            self.dropped += 1
            return
        self.events.append({"after_ms": round((when-self.start)*1000, 3), **event})

    def adopt(self, span):
        if self not in span["sinks"]:
            span["sinks"].append(self)
            self.add({"stage": span["stage"], "event": "started", "span": span["id"],
                      "parent": span["parent"], "adopted": span["start"] < self.start,
                      **span["details"]}, span["start"])


@contextmanager
def capture(owner, request_id=None):
    """Attach to the owner and adapter; never start/cancel/await native work."""
    sink = None
    sources = []
    token = None
    try:
        with _LOCK:
            sink = _Timeline(request_id)
            for target in (owner, getattr(owner, "_vision", None)):
                if target is None:
                    continue
                source = _source(target)
                sources.append((source, source.sink))
                source.sink = sink
                for span in source.active.values():
                    sink.adopt(span)
            owner._last_query_timing = sink
            token = _CAPTURE.set(sink)
    except Exception:
        pass  # Optional diagnostics never replace the real call.
    try:
        yield
    finally:
        if token is not None:
            _CAPTURE.reset(token)
        with _LOCK:
            if sink is not None:
                sink.end = time.perf_counter()
            for source, previous in sources:
                if source.sink is sink:
                    source.sink = previous


@contextmanager
def phase(stage, owner=None, **details):
    entry = None
    token = None
    meta_token = None
    source = None
    try:
        if stage in STAGES:
            with _LOCK:
                source = _source(owner)
                entry = {"id": next(_SEQUENCE), "stage": stage, "parent": _PARENT.get(),
                         "start": time.perf_counter(), "details": {**(_META.get() or {}), **_details(details)}, "sinks": []}
                if source is not None:
                    source.active[entry["id"]] = entry
                for sink in (_CAPTURE.get(), source.sink if source else None):
                    if sink is not None:
                        sink.adopt(entry)
                token = _PARENT.set(entry["id"])
                meta_token = _META.set(entry["details"])
    except Exception:
        if entry is not None and source is not None:
            source.active.pop(entry["id"], None)
        entry = None
    outcome = "returned"
    try:
        yield
    except BaseException as exc:
        outcome = "cancelled" if isinstance(exc, asyncio.CancelledError) else "timeout" if isinstance(exc, TimeoutError) else "error"
        raise
    finally:
        if token is not None:
            _PARENT.reset(token)
        if meta_token is not None:
            _META.reset(meta_token)
        if entry is not None:
            try:
                with _LOCK:
                    ended = time.perf_counter()
                    for sink in entry["sinks"]:
                        sink.add({"stage": stage, "event": outcome, "span": entry["id"],
                                  "parent": entry["parent"], "duration_ms": round((ended-entry["start"])*1000, 3),
                                  **entry["details"]}, ended)
                    if source is not None:
                        source.active.pop(entry["id"], None)
            except Exception:
                pass


def mark(owner, stage, **details):
    with phase(stage, owner, **details):
        pass


def timed(stage):
    def decorate(function):
        @wraps(function)
        async def call(owner, *args, **kwargs):
            with phase(stage, owner):
                return await function(owner, *args, **kwargs)
        return call
    return decorate


def timed_callback(stage, owner):
    """Carry only diagnostic context into an existing queued callback."""
    sink = _CAPTURE.get()
    def decorate(function):
        @wraps(function)
        def call(*args, **kwargs):
            token = _CAPTURE.set(sink)
            try:
                with phase(stage, owner):
                    return function(*args, **kwargs)
            finally:
                _CAPTURE.reset(token)
        return call
    return decorate


def snapshot(owner):
    with _LOCK:
        sink = getattr(owner, "_last_query_timing", None)
        if not isinstance(sink, _Timeline):
            return {"state": "not_recorded", "events": []}
        return {"state": "active" if sink.end is None else "finished", "request_id": sink.request_id,
                "duration_ms": round(((sink.end or time.perf_counter())-sink.start)*1000, 3),
                "events": [dict(event) for event in sink.events], "dropped_events": sink.dropped,
                "event_limit": MAX_EVENTS}
