"""F1 canonical diagnostic presentation. Pure metadata, never answer permission."""

from __future__ import annotations

import copy
import json
import math
import re
from datetime import datetime
from uuid import uuid4

from .diagnostic_session import ERRORS, OUTCOMES
from .diagnostic_session import STAGES as ENABLE_STAGES
from .prepare_trace import ENUMS
from .recognition_metrics import PATROL_DECISIONS, PATROL_REASONS, RESET_REASONS
from .timing_detail import STAGES as TIMING_STAGES

TARGET_BYTES = 96 * 1024
MAX_BYTES = 128 * 1024
COLORS = {"green": "绿", "yellow": "黄", "red": "红"}
STATUS_KEYS = {
    "panel.stopped",
    "panel.readFailed",
    "panel.capturePaused",
    "panel.connecting",
    "panel.waitingGame",
    "panel.identityFailed",
    "panel.ready",
    "panel.confirming",
}
IDENTITY_REASONS = {"insufficient_confirmations", "confirmation_reset", "yielded_for_query", "ocr_timeout"}


PANEL_WITNESS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "mount_id": {"type": "string", "maxLength": 32},
        "panel_version": {"type": "string", "maxLength": 24},
        "events": {
            "type": "array",
            "maxItems": 16,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    **{
                        k: {"type": "string", "maxLength": 48}
                        for k in ("status_key", "tone", "reason", "rendered_at", "context_at", "context_session_id")
                    },
                    **{k: {"type": "integer", "minimum": 0} for k in ("context_seq", "sequence")},
                },
            },
        },
    },
}


def number(value):
    return round(value, 3) if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def stamp(value):
    if not isinstance(value, str) or len(value) > 48:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value if parsed.tzinfo is not None else None
    except ValueError:
        return None


def opaque(value):
    return value if isinstance(value, str) and re.fullmatch(r"[a-f0-9]{32}", value) else None


def code(value, allowed, unknown):
    if value is None:
        return None
    if isinstance(value, str) and value in allowed:
        return value
    unknown["count"] += 1
    return "unmapped_code"


def _numbers(value, keys):
    value = value if isinstance(value, dict) else {}
    return {k: number(value.get(k)) for k in keys}


def health_projection(value, unknown):
    if not isinstance(value, dict):
        return {"state": "not_recorded"}
    patrol = value.get("patrol") or {}
    identity = value.get("identity") or {}
    confirm = identity.get("confirmation") or {}
    if patrol.get("decision") is None:
        patrol = {**patrol, "checks": None, "suppressed": None, "age_refreshes": None}
    return {
        "patrol": {
            **_numbers(patrol, ("checks", "suppressed", "age_refreshes")),
            "decision": code(patrol.get("decision"), PATROL_DECISIONS, unknown),
            "reason": code(patrol.get("reason"), PATROL_REASONS, unknown),
            "fallback": patrol.get("fallback") if type(patrol.get("fallback")) is bool else None,
        },
        "recent_fulls": [
            {
                **_numbers(r, ("job_id", "processing_seconds")),
                "at": stamp(r.get("at")),
                "state": code(r.get("state"), {"parsed", "yielded"}, unknown),
                "checkpoint": code(r.get("checkpoint"), {"ocr", "post_ocr", "literal_review"}, unknown),
            }
            for r in list(value.get("recent_fulls") or [])[-8:]
            if isinstance(r, dict)
        ],
        **_numbers(
            value,
            (
                "discarded_for_query",
                "discarded_at_30s_gate",
                "page_only_reuse_timeouts",
                "current_age_seconds",
                "committed_age_seconds",
            ),
        ),
        "identity": (
            {
                **_numbers(identity, ("required", "pending_count", "confirmed_count", "missing_count")),
                "tracked_counts": [number(n) for n in list(identity.get("tracked_counts") or [])[:32]],
                "recent_resets": [
                    {
                        "at": stamp(r.get("at")),
                        "reason": code(r.get("reason"), RESET_REASONS, unknown),
                        **_numbers(r, ("slots_cleared", "confirmed_cleared")),
                    }
                    for r in list(identity.get("recent_resets") or [])[-12:]
                    if isinstance(r, dict)
                ],
                "confirmation": (
                    {
                        "state": code(confirm.get("state"), {"pending", "failed"}, unknown),
                        "reason": code(confirm.get("reason"), IDENTITY_REASONS, unknown),
                        **_numbers(confirm, ("elapsed_seconds", "limit_seconds")),
                    }
                    if confirm
                    else None
                ),
            }
            if identity
            else {}
        ),
    }


def timings_projection(value, unknown):
    value = value if isinstance(value, dict) else {}
    events = []
    for e in list(value.get("events") or [])[:256]:
        if not isinstance(e, dict):
            continue
        row = {
            "stage": code(e.get("stage"), TIMING_STAGES, unknown),
            "event": code(e.get("event"), {"started", "returned", "cancelled", "timeout", "error"}, unknown),
        }
        for key in (
            "after_ms",
            "duration_ms",
            "span",
            "parent",
            "generation",
            "epoch",
            "serial",
            "job_id",
            "frame_seq",
            "verification_frame_seq",
        ):
            v = e.get(key)
            row[key] = round(v, 3) if key == "after_ms" and type(v) in (int, float) and math.isfinite(v) else number(v)
        if "role" in e:
            row["role"] = code(e["role"], {"producer", "query", "delivery", "manual", "idle"}, unknown)
        if "operation" in e:
            row["operation"] = code(
                e["operation"],
                {
                    "extract_text_with_boxes",
                    "recognize_line",
                    "read_character_detail",
                    "analyze_live",
                    "analyze",
                    "analyze_scene",
                    "analyze_navigation",
                    "read_region",
                    "verify_text_regions",
                },
                unknown,
            )
        row["operation_request_id"] = opaque(e.get("operation_request_id"))
        row["adopted"] = e.get("adopted") is True
        events.append(row)
    return {
        "state": code(value.get("state"), {"active", "finished", "not_recorded"}, unknown) or "not_recorded",
        "request_id": opaque(value.get("request_id")),
        **_numbers(value, ("duration_ms", "dropped_events", "event_limit")),
        "events": events,
    }


def witness_projection(value, unknown):
    if not isinstance(value, dict):
        return {"state": "not_observed", "events": []}
    events = []
    for e in list(value.get("events") or [])[-16:]:
        if not isinstance(e, dict):
            continue
        events.append(
            {
                "status_key": code(e.get("status_key"), STATUS_KEYS, unknown),
                "tone": code(e.get("tone"), {"success", "warning", "danger", "default"}, unknown),
                "reason": code(e.get("reason"), IDENTITY_REASONS | {"not_available", "none"}, unknown),
                "rendered_at": stamp(e.get("rendered_at")),
                "context_at": stamp(e.get("context_at")),
                "context_seq": number(e.get("context_seq")),
                "sequence": number(e.get("sequence")),
                "context_session_id": opaque(e.get("context_session_id")),
            }
        )
    version = value.get("panel_version")
    return {
        "state": "observed" if events else "not_observed",
        "source": "client_render_witness",
        "mount_id": opaque(value.get("mount_id")),
        "panel_version": version
        if isinstance(version, str) and re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", version)
        else None,
        "events": events,
        "authority": "display_only_not_answer_permission",
    }


def session_projection(value, unknown):
    value = value if isinstance(value, dict) else {}

    def event(e):
        return {
            "attempt_id": opaque(e.get("attempt_id")),
            "stage": code(e.get("stage"), ENABLE_STAGES, unknown),
            "outcome": code(e.get("outcome"), OUTCOMES, unknown),
            "started_at": stamp(e.get("started_at")),
            "at": stamp(e.get("at")),
            "elapsed_ms": number(e.get("elapsed_ms")),
            "exception_type": code(e.get("exception_type"), ERRORS, unknown),
            "error_code": code(e.get("error_code"), {"cancelled", "stage_error"}, unknown),
        }

    return {
        "session_id": opaque(value.get("session_id")),
        "instance_started_at": stamp(value.get("instance_started_at")),
        "recording_started_at": stamp(value.get("recording_started_at")),
        "instance_start_source": code(
            value.get("instance_start_source"), {"constructor", "first_diagnostic_use"}, unknown
        ),
        "pid": number(value.get("pid")),
        "same_process_instance_rebuilds": number(value.get("same_process_instance_rebuilds")),
        "instance_count_scope": "module_lifetime",
        "process_started_at": None,
        "process_start_source": "not_observed",
        "module_loaded_at": stamp(value.get("module_loaded_at")),
        "cross_process_restarts": None,
        "restart_coverage": "coverage_unknown",
        "recent_events": [event(e) for e in list(value.get("recent_events") or [])[-64:] if isinstance(e, dict)],
        "attempts": [
            {
                "attempt_id": opaque(a.get("attempt_id")),
                "started_at": stamp(a.get("started_at")),
                "state": code(a.get("state"), OUTCOMES, unknown),
                "stages": [event(e) for e in list(a.get("stages") or [])[:5] if isinstance(e, dict)],
            }
            for a in list(value.get("attempts") or [])[-4:]
            if isinstance(a, dict)
        ],
    }


def prepare_projection(records, unknown):
    """Keep parameter shape/routing codes, never legacy question digests or IDs of roles."""

    budget = [16]

    def node(n, depth=0, key=""):
        if not isinstance(n, dict) or depth > 4:
            return {}
        if budget[0] <= 0:
            return {"truncated": True}
        budget[0] -= 1
        row = {
            k: v
            for k, v in n.items()
            if k in {"length", "count", "provided", "empty", "truncated"} and type(v) in (int, bool)
        }
        for k, allowed in (
            ("presence", {"provided", "omitted"}),
            ("type", {"null", "str", "dict", "list", "tuple", "bool", "int", "float", "other"}),
        ):
            if k in n:
                row[k] = code(n[k], allowed, unknown)
        if "value" in n:
            row["value"] = opaque(n["value"]) if key == "request_id" else code(n["value"], ENUMS, unknown)
        if isinstance(n.get("items"), list):
            row["items"] = [node(x, depth + 1, key) for x in n["items"][:16]]
        if isinstance(n.get("fields"), dict):
            row["fields"] = {
                k: node(v, depth + 1, k)
                for k, v in n["fields"].items()
                if k in {"scope", "subject_state", "topics", "answer_parts", "relation", "kind", "request_id"}
            }
        return row

    result = []
    for r in records:
        budget[0] = 16
        row = {
            k: v
            for k, v in r.items()
            if k
            in {
                "request_id",
                "started_at",
                "elapsed_ms",
                "stage",
                "last_completed_stage",
                "outcome",
                "error_code",
                "persistence",
                "query_state",
                "completion_state",
                "capture_boundary",
                "generation",
                "followup_pending",
                "missing_fields",
            }
        }
        context = r.get("context") or {}
        row["context"] = {
            k: code(
                context.get(k), ENUMS | {"build", "challenge", "saved_profile", "current_screen", "unresolved"}, unknown
            )
            for k in ("kind", "execution_intent", "request_focus", "evidence_source")
        }
        row["exception_type"] = code(r.get("exception_type"), ERRORS, unknown)
        row["arguments"] = {
            k: node(v, key=k)
            for k, v in (r.get("arguments") or {}).items()
            if k
            in {
                "query",
                "intent",
                "screen_fields",
                "response_detail",
                "consultation",
                "pending_reply",
                "modality",
                "category",
            }
        }
        # No content-derived hashes, source paths, discussion actor IDs or labels.
        result.append(row)
    return result


def _health(level, reason, at, scope):
    return {"level": level, "reason": reason, "as_of": at, "scope": scope}


def _age(at, now):
    try:
        return (datetime.fromisoformat(now) - datetime.fromisoformat(at)).total_seconds() if at else None
    except (ValueError, TypeError):
        return None


def floor_projection(s):
    now = s["checked_at"]
    service, records, last = s["service_request"], s["prepare_calls"]["records"], s["last_query"]
    latest = records[-1] if records else {}
    if service.get("started_at") and (
        not latest.get("started_at") or (_age(latest["started_at"], service["started_at"]) or 0) > 0
    ):
        latest = {
            "request_id": service.get("request_id"),
            "started_at": service["started_at"],
            "query_state": "in_progress",
        }
    selected_id = latest.get("request_id") or service.get("request_id")
    selected_at = latest.get("started_at") or service.get("started_at")
    last_at = last.get("at")
    # A timestamp alone is not a request join. Legacy audit has no request ID.
    last["source_relation"] = (
        "historical"
        if (selected_at and (not last_at or (_age(last_at, selected_at) or 0) > 0))
        else "unmatched"
        if selected_id
        else "historical"
    )
    last["source_age_seconds"] = number(_age(last_at, now))
    request_level, request_reason = "yellow", "not_recorded"
    if latest:
        if latest.get("outcome") in {"failed", "cancelled"}:
            request_level, request_reason = "red", latest.get("error_code") or latest["outcome"]
        elif latest.get("query_state") in {"ready", "complete"}:
            request_level, request_reason = "green", "plugin_result_ready_not_host_speech"
        elif latest.get("query_state") in {"unconfirmed", "capture_failed"}:
            request_level, request_reason = "red", latest["query_state"]
        else:
            request_reason = latest.get("query_state") or "in_progress_or_partial"
    final = next(
        (r for r in reversed(s["answer_deliveries"]) if selected_id and r.get("request_id") == selected_id), None
    )
    if final and request_level != "red":
        if final.get("answer_state") in {"unconfirmed", "paused", "capture_failed"}:
            request_level, request_reason = "red", final["answer_state"]
        elif final.get("answer_state") == "partial":
            request_level, request_reason = "yellow", "partial_payload"
    request_health = _health(request_level, request_reason, selected_at, "latest_request")
    enabled = s["observer"]["enabled"]
    current = s["current_observation"]
    complete = s["diagnostic_read"]["complete"]
    runtime_health = _health(
        "yellow", "not_running" if not enabled else "in_progress_or_unconfirmed", now, "current_runtime"
    )
    if enabled and s["observer"]["state"] == "error":
        runtime_health = _health("red", "observer_error", now, "current_runtime")
    elif enabled and complete and current["fresh"] and current.get("observed_at"):
        runtime_health = _health("green", "current_projection_available", now, "current_runtime")
    h = s["observation_health"]
    confirmation = (h.get("identity") or {}).get("confirmation") or {}
    observation_health = runtime_health
    if enabled and confirmation.get("state") == "failed":
        observation_health = _health("red", confirmation.get("reason") or "confirmation_failed", now, "current_runtime")
    elif enabled and confirmation.get("state") == "pending":
        observation_health = _health("yellow", "confirming_identity", now, "current_runtime")
    timings = s["query_timings"]
    timings["source_relation"] = (
        "current_request"
        if selected_id and timings["request_id"] == selected_id
        else "historical"
        if selected_id and timings["request_id"]
        else "unmatched"
    )
    events = timings["events"]
    f3events = [
        e
        for e in events
        if e["stage"]
        in {"region_read", "region_page", "region_crop_atlas", "region_name_confirmation", "region_name_assignment"}
    ][-5:]
    f2events = [
        e
        for e in events
        if e["stage"]
        in {"scheduler_wait", "scheduler_drain", "native_lock_wait", "ocr_call", "region_ocr", "ocr_backend_setup"}
    ][-5:]

    def stage_health(rows, label):
        if any(e.get("event") in {"error", "timeout", "cancelled"} for e in rows):
            return _health("red", "recorded_stage_failure", None, label)
        return _health("yellow", "existing_events_only" if rows else "not_recorded", None, label)

    # Existing metadata only; absent native duration/detector facts remain null.
    build = s["build_identity"]
    floors = []

    def add(n, title, health, metrics, recent):
        floors.append({"floor": n, "title": title, "health": health, "metrics": metrics, "recent_events": recent})

    pending_node = (latest.get("arguments") or {}).get("pending_reply") or {}
    pending_fields = pending_node.get("fields") or {}
    pending_path = {
        "present": pending_node.get("presence") == "provided",
        "old_request_id": (pending_fields.get("request_id") or {}).get("value"),
        "relation": (pending_fields.get("relation") or {}).get("value"),
        "last_completed_stage": latest.get("last_completed_stage"),
    }
    add(
        5,
        "请求与交付",
        request_health,
        {
            "latest_request_health": request_health,
            "current_runtime_health": runtime_health,
            "selected_request_id": selected_id,
            "selected_request_at": selected_at,
            "pending_reply_path": pending_path,
            "last_query": last,
            "prepare_calls": s["prepare_calls"],
            "followup": s["explicit_followup"],
            "panel_witness": s["panel_witness"],
            "deliveries": s["answer_deliveries"],
            "message_path_available": s["message_path_available"],
        },
        records[-4:],
    )
    add(
        4,
        "观察与确认",
        observation_health,
        {"observation_health": h, "review": s["review"], "current_observation": current},
        [*h.get("recent_fulls", []), *(h.get("identity") or {}).get("recent_resets", [])][-8:],
    )
    add(
        3,
        "检测与区域",
        stage_health(f3events, "timing_request"),
        {
            "region_plan_missing": sum(e.get("stage") == "region_plan_missing" for e in s["vision_trace"])
            if s["vision_trace"]
            else None,
            "detector_duration_ms": None,
            "candidate_count": next(
                (e.get("character_count") for e in reversed(s["vision_trace"]) if e.get("stage") == "candidate_parsed"),
                None,
            ),
            "counts_scope": "bounded_existing_trace",
        },
        f3events,
    )
    add(
        2,
        "OCR与排队",
        stage_health(f2events, "timing_request"),
        {
            "request_id": timings["request_id"],
            "duration_ms": timings["duration_ms"],
            "dropped_events": timings["dropped_events"],
            "native_max_ms": max(
                (e["duration_ms"] for e in events if e["stage"] == "ocr_call" and e["duration_ms"] is not None),
                default=None,
            ),
            "nested_spans_are_not_additive": True,
        },
        f2events,
    )
    attempts = s["session"]["attempts"]
    attempt = attempts[-1] if attempts else {}
    stage_failed = any(e.get("outcome") in {"failed", "cancelled"} for e in attempt.get("stages", []))
    if attempt.get("state") in {"failed", "cancelled"} or stage_failed:
        build_health = _health("red", "enable_stage_failed", now, "latest_enable_attempt")
    elif build["mismatch"]:
        build_health = _health("yellow", "build_mismatch", now, "plugin_instance")
    elif not s["session"].get("session_id"):
        build_health = _health("yellow", "not_recorded", now, "plugin_instance")
    elif not build["installation_stamp"].get("commit") or not build.get("panel_version"):
        build_health = _health("yellow", "build_evidence_partial", now, "plugin_instance")
    elif attempt and not any(
        e.get("stage") == "first_commit" and e.get("outcome") == "completed" for e in attempt.get("stages", [])
    ):
        build_health = _health("yellow", "first_commit_pending", now, "latest_enable_attempt")
    else:
        build_health = _health("green", "build_and_session_observed", now, "plugin_instance")
    add(
        1,
        "构建与生命周期",
        build_health,
        {"build": build, "session": s["session"], "assistant": s["assistant"]},
        s["session"]["recent_events"][-6:],
    )
    s["latest_request_health"], s["current_runtime_health"] = request_health, runtime_health
    return floors


def build_identity(source, installation, panel, unknown):
    def build(value):
        value = value if isinstance(value, dict) else {}
        result = {}
        for k, pattern in (
            ("version", r"[0-9]+(?:\.[0-9]+){1,3}"),
            ("revision", r"(?:0\.[0-9]+\.[0-9]+|demo-)[a-zA-Z0-9_.-]{0,80}"),
            ("commit", r"[a-f0-9]{7,40}"),
        ):
            v = value.get(k)
            result[k] = v if isinstance(v, str) and re.fullmatch(pattern, v) else None
        return result

    src, installed = build(source), build(installation)
    version = panel if isinstance(panel, str) and re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", panel) else None
    versions = {x for x in (src["version"], installed["version"], version) if x is not None}
    return {
        "source_constants": src,
        "installation_stamp": installed,
        "installation_stamp_authority": "packaged_build_constants_not_independent_manifest",
        "panel_version": version,
        "mismatch": len(versions) > 1
        or bool(src["commit"] and installed["commit"] and src["commit"] != installed["commit"]),
        "independent_installation_verification": "not_observed",
    }


def summaries(snapshot):
    def show(value):
        return "未记录" if value is None else str(value)

    rows = []
    for f in snapshot["floors"]:
        h, metrics = f["health"], f["metrics"]
        floor = f["floor"]
        if floor == 5:
            timing = snapshot["query_timings"]
            detail = "请求耗时=" + show(timing.get("duration_ms")) + "ms"
        elif floor == 4:
            health = metrics.get("observation_health") or {}
            detail = (
                "提交年龄="
                + show(health.get("committed_age_seconds"))
                + "s，让位="
                + show(health.get("discarded_for_query"))
            )
        elif floor == 3:
            detail = "区域计划缺失=" + show(metrics.get("region_plan_missing"))
        elif floor == 2:
            detail = "最长已记原生OCR=" + show(metrics.get("native_max_ms")) + "ms"
        else:
            build = metrics.get("build") or {}
            detail = (
                "源码="
                + show((build.get("source_constants") or {}).get("version"))
                + "，安装="
                + show((build.get("installation_stamp") or {}).get("version"))
                + "，面板="
                + show(build.get("panel_version"))
            )
        rows.append(f"{floor}F {COLORS[h['level']]}｜{f['title']}｜{h['reason']}｜{detail}｜{h['scope']}")
    return rows


def canonical_text(snapshot):
    return json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _wire(snapshot):
    lines = summaries(snapshot)
    for floor, line in zip(snapshot["floors"], lines):
        floor["summary"] = line
    js = canonical_text(snapshot)
    return "\n".join([*lines, js]), js


def seal(snapshot):
    """Bound one canonical object; never slice serialized JSON."""
    snapshot["size_control"] = {
        "state": "complete",
        "target_bytes": TARGET_BYTES,
        "limit_bytes": MAX_BYTES,
        "omitted_events": 0,
    }
    text, js = _wire(snapshot)
    if len(text.encode("utf-8")) > TARGET_BYTES:
        # Drop only optional repeated details; preserve request status and all floors.
        for key in ("vision_trace", "page_control_trace", "revocation_trace"):
            snapshot["size_control"]["omitted_events"] += len(snapshot.get(key, []))
            snapshot[key] = snapshot.get(key, [])[-8:]
        events = snapshot["query_timings"]["events"]
        snapshot["size_control"]["omitted_events"] += max(0, len(events) - 64)
        snapshot["query_timings"]["events"] = events[-64:]
        snapshot["size_control"]["state"] = "size_limited"
        text, js = _wire(snapshot)
    if len(text.encode("utf-8")) > MAX_BYTES:
        snapshot["size_control"]["state"] = "size_limited"
        snapshot["query_timings"]["events"] = []
        for f in snapshot["floors"]:
            f["recent_events"] = []
            if f["floor"] == 5:
                f["metrics"]["last_query"]["stages"] = []
        text, js = _wire(snapshot)
    if len(text.encode("utf-8")) > MAX_BYTES:
        keep = {
            k: snapshot[k]
            for k in (
                "schema_version",
                "diagnostic_id",
                "checked_at",
                "checks",
                "last_query",
                "observation_health",
                "query_timings",
                "latest_request_health",
                "current_runtime_health",
            )
        }
        keep["last_query"] = {k: v for k, v in snapshot["last_query"].items() if k != "stages"}
        keep["query_timings"] = {k: v for k, v in snapshot["query_timings"].items() if k != "events"}
        keep["query_timings"]["events"] = []
        keep["floors"] = [
            {
                "floor": f["floor"],
                "title": f["title"],
                "health": f["health"],
                "metrics": {"state": "size_limited"},
                "recent_events": [],
            }
            for f in snapshot["floors"]
        ]
        keep["size_control"] = {
            "state": "size_limited",
            "reason": "essential_projection_limited",
            "target_bytes": TARGET_BYTES,
            "limit_bytes": MAX_BYTES,
            "details_omitted": True,
        }
        snapshot = keep
        text, js = _wire(snapshot)
        if len(text.encode("utf-8")) > MAX_BYTES:
            raise ValueError("diagnostic_size_limited")
    return {
        "snapshot": snapshot,
        "schema_version": 2,
        "diagnostic_id": snapshot["diagnostic_id"],
        **{k: snapshot[k] for k in ("checked_at", "checks", "last_query", "observation_health", "query_timings")},
        "copy_text": text,
        "json_text": js,
    }


def finish(
    snapshot,
    *,
    query_timings=None,
    observation_health=None,
    session=None,
    panel_witness=None,
    source_build=None,
    installation_stamp=None,
    unknown_count=0,
    source_samples=None,
):
    unknown = {"count": unknown_count}
    snapshot = copy.deepcopy(snapshot)

    # Remove even old content digests: the default diagnostic carries no game/chat-derived hashes.
    def strip(v):
        if isinstance(v, dict):
            return {
                k: strip(x)
                for k, x in v.items()
                if not any(w in k for w in ("digest", "sha256")) and k not in {"subject_ids", "selection"}
            }
        if isinstance(v, list):
            return [strip(x) for x in v]
        return v

    snapshot = strip(snapshot)

    def safe_errors(v):
        if isinstance(v, dict):
            return {
                k: (code(x, ERRORS, unknown) if k in {"error_type", "exception_type"} else safe_errors(x))
                for k, x in v.items()
            }
        if isinstance(v, list):
            return [safe_errors(x) for x in v]
        if v == "unknown":
            unknown["count"] += 1
            return "unmapped_code"
        if v == "unmapped_code":
            unknown["count"] += 1
        return v

    snapshot = safe_errors(snapshot)
    snapshot.update(schema_version=2, diagnostic_id=uuid4().hex)
    snapshot["prepare_calls"]["records"] = prepare_projection(snapshot["prepare_calls"]["records"], unknown)
    snapshot["query_timings"] = timings_projection(query_timings, unknown)
    snapshot["observation_health"] = health_projection(observation_health, unknown)
    snapshot["session"] = session_projection(session, unknown)
    snapshot["panel_witness"] = witness_projection(panel_witness, unknown)
    snapshot["build_identity"] = build_identity(
        source_build, installation_stamp, snapshot["panel_witness"].get("panel_version"), unknown
    )
    now_session = snapshot["session"].get("instance_started_at")
    for record in snapshot["prepare_calls"]["records"]:
        at = record.get("started_at")
        record["source_relation"] = (
            "historical" if now_session and at and (_age(at, now_session) or 0) > 0 else "session_unverified"
        )
    snapshot["sources"] = {
        name: {
            "started_at": stamp(row.get("started_at")),
            "finished_at": stamp(row.get("finished_at")),
            "duration_ms": number(row.get("duration_ms")),
            "outcome": code(row.get("outcome"), {"returned", "timeout", "error"}, unknown),
            "error_type": code(row.get("error_type"), ERRORS, unknown),
        }
        for name, row in (source_samples or {}).items()
        if name in {"assistant", "query_audit", "observation"} and isinstance(row, dict)
    }
    snapshot["floors"] = floor_projection(snapshot)
    snapshot["unmapped_codes"] = {"count": unknown["count"]}
    snapshot["default_payload_scope"] = "metadata_only_no_host_speech_receipt"
    return seal(snapshot)
