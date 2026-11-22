"""Bounded observation counters. These values never grant answer permission."""

from collections import deque
from datetime import datetime, timezone
from math import isfinite

from .revocation_trace import REASONS as REVOCATION_REASONS

PASS_LIMIT = 8
RESET_LIMIT = 12
PATROL_DECISIONS = {"full", "skip", "wait"}
PATROL_REASONS = {
    "unconfirmed",
    "capture_unavailable",
    "coverage_unavailable",
    "awaiting_full_commit",
    "awaiting_full_anchor",
    "capture_changed",
    "content_changed",
    "demand",
    "confirming_frames",
    "stable_content",
}
RESET_REASONS = {
    "runtime_reset",
    "observation_revoked",
    "scene_only_transition",
    "empty_character_list",
    "verified_non_menu",
    "missing_frame_limit",
    "slot_missing",
    "slot_replaced",
} | REVOCATION_REASONS


def _number(value):
    if type(value) in (int, float) and isfinite(value) and value >= 0:
        return round(value, 3)
    return None


def _utc():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def count_reset(events, reason, *, slots=0, stable=0):
    if slots or stable:
        events.append(
            {
                "at": _utc(),
                "reason": reason if reason in RESET_REASONS else "observation_revoked",
                "slots_cleared": slots,
                "confirmed_cleared": stable,
            }
        )


def _metrics(owner):
    data = getattr(owner, "_recognition_metrics", None)
    if data is None:
        data = owner._recognition_metrics = {
            "recent_fulls": deque(maxlen=PASS_LIMIT),
            "discarded_for_query": 0,
            "discarded_at_30s_gate": 0,
        }
    return data


def record_late_discard(owner):
    _metrics(owner)["discarded_at_30s_gate"] += 1


def record_full(owner, *, job_id, state, analysis=None, checkpoint=None):
    """Keep actual pass timings and count only full jobs that were discarded."""
    data = _metrics(owner)
    if state == "yielded":
        data["discarded_for_query"] += 1
    recent = data["recent_fulls"]
    latest = recent[-1] if recent else None
    if latest is None or latest["job_id"] != job_id:
        latest = {"job_id": _number(job_id), "processing_seconds": None}
        recent.append(latest)
    if isinstance(analysis, dict):
        latest["processing_seconds"] = _number((analysis.get("recognition") or {}).get("processing_seconds"))
    latest.update(state=state, checkpoint=checkpoint, at=_utc())


def _commit_age(owner):
    raw = getattr(owner, "_committed_observation", None) or {}
    try:
        observed = datetime.fromisoformat(raw.get("observed_at", ""))
        if observed.tzinfo is None:
            return None
        return _number((datetime.now(timezone.utc) - observed).total_seconds())
    except (TypeError, ValueError):
        return None


def observation_health(owner, evidence):
    patrol = getattr(owner, "_patrol_status", None) or {}
    data = getattr(owner, "_recognition_metrics", None) or {}
    identity = getattr(getattr(owner, "_runtime", None), "identity_diagnostics", None)
    return {
        "patrol": {
            "decision": (patrol["decision"] if patrol.get("decision") in PATROL_DECISIONS else None),
            "reason": (patrol["reason"] if patrol.get("reason") in PATROL_REASONS else None),
            "fallback": patrol.get("fallback") is True,
            "checks": _number(patrol.get("checks")) or 0,
            "suppressed": _number(patrol.get("suppressed")) or 0,
        },
        "recent_fulls": [dict(row) for row in data.get("recent_fulls", ())],
        "discarded_for_query": data.get("discarded_for_query", 0),
        "discarded_at_30s_gate": data.get("discarded_at_30s_gate", 0),
        "identity": identity() if callable(identity) else {},
        "current_age_seconds": _number(evidence.get("age_seconds")),
        "committed_age_seconds": _commit_age(owner),
    }
