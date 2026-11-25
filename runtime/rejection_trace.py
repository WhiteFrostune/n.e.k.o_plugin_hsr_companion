"""Bounded, memory-only rejection observations. Never an answer/scheduling gate."""

from __future__ import annotations

import copy
import math
import re
import threading
from collections import OrderedDict, deque
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone

LIMIT = 256
REQUEST_LIMIT = 8
_LOCK = threading.RLock()
_CONTEXT = ContextVar("hsr_rejection_context", default=None)
BRANCHES = frozenset(
    {
        f"{prefix}{n:02}"
        for prefix, end in (("Q", 18), ("V", 16), ("P", 5), ("B", 6), ("D", 3), ("F", 22), ("H", 8), ("X", 6))
        for n in range(1, end + 1)
    }
)
STAGES = frozenset(
    {
        "compact",
        "verification",
        "boundary",
        "full",
        "scene",
        "navigation",
        "pending",
        "activity",
        "followup",
        "cache",
        "delivery",
    }
)
OUTCOMES = frozenset(
    {"reject", "withhold", "discard", "defer", "fallback", "not_applicable", "allow", "cancel", "retry", "error"}
)
REASONS = frozenset(
    {
        "unsupported",
        "no_source",
        "not_live",
        "no_analysis",
        "wrong_page",
        "scene_unverified",
        "deadline",
        "no_controls",
        "no_frame",
        "window_changed",
        "controls_changed",
        "epoch_changed",
        "no_result",
        "no_plan",
        "answer_generation_missing",
        "refresh_generation_missing",
        "raw_revision_changed",
        "packet_revision_changed",
        "proof_epoch_changed",
        "proof_revision_changed",
        "refresh_generation_changed",
        "superseded",
        "receipt_rejected",
        "snapshot_rejected",
        "candidate_missing",
        "observation_invalidated",
        "observer_not_observing",
        "missing_proof",
        "invalid_timestamp",
        "stale_not_allowed",
        "expired",
        "no_client",
        "no_pid",
        "capture_failed",
        "native_review_deferred",
        "busy",
        "retained_proof_rejected",
        "insufficient_controls",
        "pixels_changed",
        "invalidated_during_check",
        "new_question",
        "observer_unavailable",
        "source_unconfirmed",
        "pending_unproved",
        "page_changed",
        "revision_unproved",
        "unscoped_revision",
        "page_proof_missing",
        "page_control_challenged",
        "fields_withheld",
        "identity_withheld",
        "generation_changed",
        "fields_missing",
        "frame_stale",
        "preview_pending",
        "revision_changed",
        "deferred",
        "preempted",
        "analysis_superseded",
        "capture_changed_during_analysis",
        "worker_draining",
        "yielded",
        "late_result",
        "incomplete_read",
        "full_scope_missing",
        "commit_revoked",
        "parsed",
        "committed",
        "invalid_relation",
        "request_not_current",
        "activity_unavailable",
        "message_not_current",
        "pending_intercepted",
        "confirm",
        "cancel",
        "new_request",
        "uncertain",
        "unsupported_activity",
        "sdk_error",
        "activity_busy",
        "timeout",
        "activity_error",
        "cancelled",
        "retry_exhausted",
        "relation_hold",
        "request_replaced",
        "activity_retry",
        "user_cancelled",
        "activity_unavailable_after_retries",
        "message_relation_unresolved",
        "paused",
        "discussion_changed",
        "target_changed",
        "observation_changed",
        "followup_failed",
        "message_changed",
        "answer_not_ready",
        "empty_body",
        "empty_projection",
        "message_relation_unresolved",
        "submission_failed",
        "submitted",
        "admitted",
        "not_observable",
        "proof_rejected",
        "observer_or_target_changed",
        "service_incomplete",
        "partial_delivered_missing_fields",
        "no_new_verified_facts",
        "unmapped_code",
    }
)


def _code(value, allowed):
    return value if isinstance(value, str) and value in allowed else "unmapped_code"


Q11_PIXEL_RULES = frozenset({"missing_regions", "region_count", "geometry", "empty_pixels", "mean",
                             "strong_fraction", "cell_strong", "cell_cluster", "not_observed"})
Q11_REVIEW_RESULTS = frozenset({"unavailable", "deadline", "not_scheduled", "superseded", "error",
                              "unreadable", "contradiction", "skipped", "frame_stale", "cancelled", "proof_limit"})


def _q11_detail(value):
    value = value if isinstance(value, dict) else {}
    index = value.get("region_index")
    return {"pixel_rule": _code(value.get("pixel_rule"), Q11_PIXEL_RULES),
            "region_index": index if type(index) is int and 0 <= index < 48 else None,
            "region_purpose": _code(value.get("region_purpose"), {"page_control"}),
            "text_review": _code(value.get("text_review"), Q11_REVIEW_RESULTS)}


def _id(value):
    return value if isinstance(value, str) and re.fullmatch(r"[a-f0-9]{32}", value) else None


def _state(owner):
    state = getattr(owner, "_rejection_trace", None)
    if state is None:
        state = {
            "events": deque(maxlen=LIMIT),
            "requests": OrderedDict(),
            "sequence": 0,
            "overwritten": 0,
            "unmapped_count": 0,
            "recording_errors": 0,
        }
        owner._rejection_trace = state
    return state


@contextmanager
def context(owner, request_id=None):
    # Context setup/cleanup is optional too. Never suppress a business exception.
    token = None
    try:
        token = _CONTEXT.set((owner, _id(request_id)))
    except Exception:
        pass
    try:
        yield
    finally:
        if token is not None:
            try:
                _CONTEXT.reset(token)
            except Exception:
                pass


def _record(owner, branch, stage, outcome, reason, *, request_id=None, related_request_id=None, duration_ms=None, q11=None):
    active = _CONTEXT.get()
    if owner is None and active:
        owner = active[0]
    if owner is None:
        return
    if request_id is None:
        if active and active[0] is owner:
            request_id = active[1]
        if request_id is None:
            from .prepare_trace import request_id as current_request

            request_id = current_request(owner)
    from .vision_trace import correlation

    ids = correlation()
    if request_id is None:
        request_id = ids.get("request_id")
    at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    row = {
        "branch_id": _code(branch, BRANCHES),
        "request_id": _id(request_id),
        "related_request_id": _id(related_request_id),
        "stage": _code(stage, STAGES),
        "outcome": _code(outcome, OUTCOMES),
        "reason": _code(reason, REASONS),
        "first_at": at,
        "last_at": at,
        "count": 1,
        "duration_ms": round(max(0, duration_ms), 3)
        if type(duration_ms) in {int, float} and math.isfinite(duration_ms)
        else None,
    }
    if branch == "Q11" and isinstance(q11, dict):
        row["q11"] = _q11_detail(q11)
    row.update(
        {
            k: ids[k]
            for k in ("job_id", "frame_seq", "verification_frame_seq")
            if type(ids.get(k)) is int and 0 <= ids[k] <= 2**53
        }
    )
    with _LOCK:
        state = _state(owner)
        state["unmapped_count"] += sum(v == "unmapped_code" for v in row.values())
        state["unmapped_count"] += sum(v == "unmapped_code" for v in row.get("q11", {}).values())
        # Only the same request/check/result can merge. Other checks interleaved
        # by polling do not evict the first refusal of this request.
        dependency = (
            "branch_id",
            "request_id",
            "related_request_id",
            "stage",
            "job_id",
            "frame_seq",
            "verification_frame_seq",
            "q11",
        )
        prior = next((e for e in reversed(state["events"]) if all(e.get(k) == row.get(k) for k in dependency)), None)
        if prior is not None and (prior["outcome"], prior["reason"]) != (row["outcome"], row["reason"]):
            prior = None
        if prior is not None:
            prior.update(last_at=at, count=prior["count"] + 1, duration_ms=row["duration_ms"])
            row = prior
        else:
            state["sequence"] += 1
            row["event_seq"] = state["sequence"]
            if len(state["events"]) == LIMIT:
                state["overwritten"] += 1
            state["events"].append(row)
        rid = row["request_id"]
        if rid:
            summaries = state["requests"]
            summary = summaries.setdefault(
                rid,
                {"request_id": rid, "first_at": at, "event_count": 0, "first_rejection": None, "last_rejection": None},
            )
            summaries.move_to_end(rid)
            summary.update(last_at=at, event_count=summary["event_count"] + 1, last_event=copy.deepcopy(row))
            if row["outcome"] != "allow":
                summary["first_rejection"] = summary["first_rejection"] or copy.deepcopy(row)
                summary["last_rejection"] = copy.deepcopy(row)
            while len(summaries) > REQUEST_LIMIT:
                summaries.popitem(last=False)


def emit(owner, branch, stage, outcome, reason, **details):
    """Contains even a broken/replaced recorder; no IO, await or business return."""
    try:
        _record(owner, branch, stage, outcome, reason, **details)
    except Exception:
        try:
            with _LOCK:
                _state(owner)["recording_errors"] += 1
        except Exception:
            pass


def snapshot(owner):
    try:
        with _LOCK:
            state = _state(owner)
            return {
                "event_limit": LIMIT,
                "request_limit": REQUEST_LIMIT,
                "events": copy.deepcopy(list(state["events"])),
                "requests": copy.deepcopy(list(state["requests"].values())),
                **{k: state[k] for k in ("overwritten", "unmapped_count", "recording_errors")},
            }
    except Exception:
        return {"state": "not_recorded", "events": [], "requests": []}


def sanitize(value):
    """Export uses the same closed schema even for malformed diagnostic input."""
    value = value if isinstance(value, dict) else {}

    def number(v):
        return v if type(v) in {int, float} and math.isfinite(v) and 0 <= v <= 2**53 else None

    def stamp(v):
        if not isinstance(v, str) or len(v) > 40:
            return None
        try:
            datetime.fromisoformat(v)
            return v
        except ValueError:
            return None

    def event(e):
        e = e if isinstance(e, dict) else {}
        return {
            "branch_id": _code(e.get("branch_id"), BRANCHES),
            "request_id": _id(e.get("request_id")),
            "related_request_id": _id(e.get("related_request_id")),
            "stage": _code(e.get("stage"), STAGES),
            "outcome": _code(e.get("outcome"), OUTCOMES),
            "reason": _code(e.get("reason"), REASONS),
            **{
                k: number(e.get(k))
                for k in ("count", "duration_ms", "event_seq", "job_id", "frame_seq", "verification_frame_seq")
            },
            **({"q11": _q11_detail(e["q11"])} if e.get("branch_id") == "Q11" and isinstance(e.get("q11"), dict) else {}),
            "first_at": stamp(e.get("first_at")),
            "last_at": stamp(e.get("last_at")),
        }

    return {
        "event_limit": LIMIT,
        "request_limit": REQUEST_LIMIT,
        **{k: number(value.get(k)) for k in ("overwritten", "unmapped_count", "recording_errors")},
        "events": [event(e) for e in list(value.get("events") or [])[-LIMIT:]],
        "requests": [
            {
                "request_id": _id(r.get("request_id")),
                "first_at": stamp(r.get("first_at")),
                "last_at": stamp(r.get("last_at")),
                "event_count": number(r.get("event_count")),
                **{k: event(r[k]) if r.get(k) else None for k in ("first_rejection", "last_rejection", "last_event")},
            }
            for r in list(value.get("requests") or [])[-REQUEST_LIMIT:]
            if isinstance(r, dict)
        ],
    }
