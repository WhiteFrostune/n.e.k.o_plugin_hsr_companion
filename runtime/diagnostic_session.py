"""F1 memory-only lifecycle observations; never controls observation or delivery."""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
from uuid import uuid4

_LOCK = threading.Lock()
_INSTANCES = 0
_MODULE_PID = os.getpid()
_MODULE_AT = datetime.now(timezone.utc).isoformat()
STAGES = {"enable_request", "initialization", "assistant_connection", "first_observation", "first_commit"}
OUTCOMES = {"started", "accepted", "completed", "failed", "cancelled", "unavailable", "connected"}
ERRORS = {
    "RuntimeError",
    "ValueError",
    "TypeError",
    "OSError",
    "TimeoutError",
    "CancelledError",
    "AttributeError",
    "KeyError",
    "AssertionError",
    "Exception",
}


def utc():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def initialize(owner, *, constructed=False):
    """Best effort, including instances constructed without __init__ in tests."""
    global _INSTANCES
    try:
        state = getattr(owner, "_diagnostic_session", None)
        if state is None:
            with _LOCK:
                _INSTANCES += 1
                state = {
                    "session_id": uuid4().hex,
                    "instance_started_at": utc() if constructed else None,
                    "recording_started_at": utc(),
                    "instance_start_source": "constructor" if constructed else "first_diagnostic_use",
                    "pid": os.getpid(),
                    "instance_number": _INSTANCES,
                    "module_pid": _MODULE_PID,
                    "module_loaded_at": _MODULE_AT,
                    "events": deque(maxlen=64),
                    "attempts": deque(maxlen=4),
                    "context_seq": 0,
                }
                owner._diagnostic_session = state
        return state
    except Exception:
        return None


def begin_enable(owner):
    try:
        state = initialize(owner)
        attempt = {"attempt_id": uuid4().hex, "started_at": utc(), "state": "started", "stages": {}}
        state["attempts"].append(attempt)
        note(owner, "enable_request", "started")
        note(owner, "initialization", "started")
    except Exception:
        pass


def note(owner, stage, outcome, *, error=None):
    """Read diagnostic state only. No names, arguments, result bodies or IO."""
    try:
        state = getattr(owner, "_diagnostic_session", None)
        if not state or not state["attempts"] or stage not in STAGES or outcome not in OUTCOMES:
            return
        attempt = state["attempts"][-1]
        rows = attempt["stages"]
        previous = rows.get(stage, {})
        if previous.get("outcome") in {"completed", "connected"}:
            return
        now = time.monotonic()
        start = previous.get("_start", now)
        row = {
            "stage": stage,
            "started_at": previous.get("started_at", utc()),
            "at": utc(),
            "outcome": outcome,
            "elapsed_ms": round(max(0, now - start) * 1000, 3),
            "_start": start,
        }
        if error is not None:
            kind = type(error).__name__
            row.update(
                exception_type=kind if kind in ERRORS else "unmapped_code",
                error_code="cancelled" if kind == "CancelledError" else "stage_error",
            )
        rows[stage] = row
        state["events"].append({"attempt_id": attempt["attempt_id"], **{k: v for k, v in row.items() if k != "_start"}})
        if stage == "enable_request":
            attempt["state"] = outcome
    except Exception:
        pass


def safe_note(owner, *args, **kwargs):
    # Also contains a broken/replaced recorder, not just malformed samples.
    try:
        note(owner, *args, **kwargs)
    except Exception:
        pass


@contextmanager
def step(owner, stage):
    safe_note(owner, stage, "started")
    result = {"outcome": "completed"}
    try:
        yield result
    except BaseException as exc:
        safe_note(owner, stage, "cancelled" if type(exc).__name__ == "CancelledError" else "failed", error=exc)
        raise
    else:
        safe_note(owner, stage, result["outcome"])


def observe_enable(function):
    @wraps(function)
    async def call(owner, *args, **kwargs):
        try:
            begin_enable(owner)
        except Exception:
            pass
        try:
            result = await function(owner, *args, **kwargs)
        except BaseException as exc:
            safe_note(owner, "initialization", "failed", error=exc)
            safe_note(
                owner, "enable_request", "cancelled" if type(exc).__name__ == "CancelledError" else "failed", error=exc
            )
            raise
        safe_note(owner, "enable_request", "failed" if type(result).__name__ == "Err" else "accepted")
        return result

    return call


def snapshot(owner):
    try:
        state = initialize(owner)
        return {
            "session_id": state["session_id"],
            "instance_started_at": state["instance_started_at"],
            "recording_started_at": state["recording_started_at"],
            "instance_start_source": state["instance_start_source"],
            "pid": state["pid"],
            "same_process_instance_rebuilds": state["instance_number"] - 1,
            "instance_count_scope": "module_lifetime",
            "process_started_at": None,
            "process_start_source": "not_observed",
            "module_loaded_at": state["module_loaded_at"],
            "cross_process_restarts": None,
            "restart_coverage": "coverage_unknown",
            "attempts": [
                {
                    **{k: v for k, v in a.items() if k != "stages"},
                    "stages": [{k: v for k, v in r.items() if k != "_start"} for r in a["stages"].values()],
                }
                for a in state["attempts"]
            ],
            "recent_events": list(state["events"]),
        }
    except Exception:
        return {"state": "not_recorded", "restart_coverage": "coverage_unknown"}


def context_marker(owner):
    try:
        state = initialize(owner)
        state["context_seq"] += 1
        return {"session_id": state["session_id"], "context_seq": state["context_seq"], "at": utc()}
    except Exception:
        return {}
