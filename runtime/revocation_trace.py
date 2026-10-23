"""Bounded lifecycle receipts, no game text, images, paths or chat."""
from __future__ import annotations

import hashlib
import re
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone

REASONS = frozenset({"explicit_revocation", "plugin_restarted", "observation_stopped", "game_closed",
    "capture_paused", "legacy_desktop_capture", "capture_changed_during_analysis", "new_page_title",
    "content_or_page_changed", "content_or_page_changed_during_analysis", "page_changed_during_analysis",
    "unconfirmed_page_after_analysis", "visual_snapshot_needs_refresh", "content_changed", "page_changed"})

_cause = ContextVar("hsr_revocation_cause", default=None)
ORIGINS = {"frame_change", "scene_preview", "_publish_scene_preview", "invalidate_frame_change", "watch_latest_frame",
           "_guarded_analysis", "_observer_loop", "_stop_observation_now", "startup", "shutdown", "unmapped"}
BASES = {"page_control_pixels", "coarse_pixels_only", "page_classification", "window_identity", "lifecycle", "unspecified"}


@contextmanager
def cause(*, origin, basis, scope="observation", controls=0, candidate=False):
    token = _cause.set({"origin": origin, "basis": basis, "scope": scope,
                        "control_count": controls, "candidate_present": candidate})
    try:
        yield
    finally:
        _cause.reset(token)


def record(owner, reason, before):
    frame = sys._getframe(1)
    callers = []
    while frame is not None and len(callers) < 6:
        name = frame.f_code.co_name
        if name not in {"revoke", "_revoke_current_observation", "_invalidate_current_observation"}:
            callers.append(name if name in {"_publish_scene_preview", "invalidate_frame_change", "watch_latest_frame",
                "_guarded_analysis", "_observer_loop", "_stop_observation_now", "startup", "shutdown"} else "unmapped")
            break
        frame = frame.f_back
    del frame
    details = _cause.get() or {"origin": (callers or ["unmapped"])[0],
        "basis": "window_identity" if reason == "capture_changed_during_analysis" else "lifecycle" if reason in {
        "plugin_restarted", "observation_stopped", "game_closed", "capture_paused"} else "unspecified", "scope": "observation"}
    row = {"at": datetime.now(timezone.utc).isoformat(), "reason": reason if reason in REASONS else "unknown",
        "from_epoch": before, "to_epoch": getattr(owner, "_observation_epoch", 0),
        "serial": getattr(owner, "_observation_serial", 0), **details,
        "request_id": (getattr(owner, "_service_request", {}) or {}).get("request_id"),
        "reason_digest": hashlib.sha256(str(reason).encode()).hexdigest() if reason not in REASONS else None}
    owner._revocation_trace = [*getattr(owner, "_revocation_trace", []), *sanitize([row])][-32:]


def sanitize(rows):
    result = []
    for row in list(rows or [])[-32:]:
        if not isinstance(row, dict):
            continue
        item = {k: row.get(k) for k in ("from_epoch", "to_epoch", "serial", "control_count")
                if type(row.get(k)) is int and row[k] >= 0}
        item.update({k: row.get(k) if isinstance(row.get(k), str) and row.get(k) in allowed else "unknown" for k, allowed in (
            ("reason", REASONS), ("origin", ORIGINS), ("basis", BASES), ("scope", {"observation", "page", "field", "object"}))})
        try:
            stamp = datetime.fromisoformat(str(row.get("at")))
            item["at"] = stamp.isoformat() if stamp.tzinfo else None
        except ValueError:
            item["at"] = None
        for key, length in (("request_id", 32), ("reason_digest", 64)):
            item[key] = row.get(key) if re.fullmatch(r"[a-f0-9]{%d}" % length, str(row.get(key))) else None
        if type(row.get("candidate_present")) is bool:
            item["candidate_present"] = row["candidate_present"]
        result.append(item)
    return result


def public_trace(owner):
    return sanitize(getattr(owner, "_revocation_trace", []))
