"""Read-only, allowlisted support report; never export game or chat content."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from typing import Any

from .build_info import BUILD_REVISION, PLUGIN_VERSION
from .equipment import equipment_card, handoff_receipt
from .navigation import navigation_answer
from .query_plan import SCREEN_FIELDS
from .service_contract import public_service_request


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(value, 3) if math.isfinite(value) and value >= 0 else None


def _code(value: Any, allowed: set[str]) -> str:
    return value if isinstance(value, str) and value in allowed else "unknown"


def _timestamp(value: Any) -> str | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.isoformat() if parsed.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def _plan_summary(value: Any) -> dict | None:
    if not isinstance(value, dict):
        return None
    fields = value.get("fields")
    return {"screen": value.get("screen") if isinstance(value.get("screen"), bool) else None,
            **({"planning_source": _code(value.get("planning_source"),
                {"legacy_keywords", "structured_fields", "semantic_intent"})} if "planning_source" in value else {}),
            "read_path": _code(value.get("read_path"), {"scene", "navigation", "full", "knowledge"}),
            "fields": sorted({f for f in fields if isinstance(f, str) and f in SCREEN_FIELDS})
            if isinstance(fields, list) else []}


def _handoff_summary(value: dict) -> dict:
    return {"channel": _code(value.get("channel"), {"direct", "followup", "current_projection"}),
            "observation_serial": _number(value.get("observation_serial")),
            "observed_at": _timestamp(value.get("observed_at")),
            "content_digest": value.get("content_digest") if re.fullmatch(r"[a-f0-9]{64}", str(value.get("content_digest"))) else None,
            "state": _code(value.get("state"), {"confirmed", "partial", "unconfirmed", "conflict"}),
            "main_stat_present": value.get("main_stat_present") is True,
            "sub_stat_count": _number(value.get("sub_stat_count")), "complete": value.get("complete") is True,
            "ack_scope": "plugin_payload_only_not_host_speech"}


def _assessment_summary(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    return {"state": _code(value.get("state"), {"not_requested", "needs_subject", "rules_unavailable",
                "needs_evidence", "partial", "complete", "needs_source", "needs_saved_build", "needs_cone_data"}),
            "reading_state": _code(value.get("reading_state"), {"not_requested", "unconfirmed", "partial", "evidence_available"}),
            "reading_complete": value.get("reading_complete") is True,
            "evaluation_available": value.get("evaluation_available") is True,
            "evaluation_complete": value.get("evaluation_complete") is True,
            "subject_state": _code(value.get("subject_state"), {"legacy", "inherit", "specified", "unresolved"}),
            "subject_count": _number(value.get("subject_count"))}


def _delivery_summary(value: dict) -> dict:
    def fields(name):
        items = value.get(name)
        return sorted({f for f in items if isinstance(f, str) and f in SCREEN_FIELDS}) if isinstance(items, list) else []
    projection = value.get("message_projection")
    projection_summary = None
    if isinstance(projection, dict):
        digest = projection.get("text_sha256")
        projection_summary = {
            "mode": _code(projection.get("mode"), {"current_facts", "page_summary", "cone_fit_summary", "transport_limited_notice", "timeout_notice", "empty_projection"}),
            "complete": projection.get("complete") if isinstance(projection.get("complete"), bool) else None,
            "required_rows": _number(projection.get("required_rows")),
            "message_rows": _number(projection.get("message_rows")),
            "text_sha256": digest if re.fullmatch(r"[a-f0-9]{64}", str(digest)) else None,
        }
        for name in ("source_rows", "omitted_rows"):
            if name in projection:
                projection_summary[name] = _number(projection[name])
        for name in ("service_satisfied", "all_input_rows_delivered"):
            if name in projection:
                projection_summary[name] = projection[name] if isinstance(projection[name], bool) else None
        for key in ("carried_sections", "required_sections", "missing_sections"):
            if isinstance(projection.get(key), list):
                projection_summary[key] = sorted({s for s in projection[key]
                    if isinstance(s, str) and s in {"page", "stage_title", "objectives", "mechanics", "visible_text",
                        "subject", "current_cone", "reviewed_fit", "reviewed_condition",
                        "rows", "assessment", "next_step", "options"}})
        if isinstance(projection.get("omitted_sections"), list):
            projection_summary["omitted_sections"] = sorted({s for s in projection["omitted_sections"]
                if isinstance(s, str) and s in {"full_catalog_effect", "additional_reviewed_conditions"}})

    boundary = value.get("boundary") or {}
    request_progress = value.get("request_completion")
    completion_summary = None
    if isinstance(request_progress, dict):
        completion_summary = {key: request_progress.get(key) if isinstance(request_progress.get(key), bool) else None
                              for key in ("facts_ready", "service_ready", "can_wait_for_evidence",
                                          "body_complete", "submitted", "answer_complete", "delivered_complete")}
        completion_summary["state"] = _code(request_progress.get("state"), {
            "complete", "needs_evidence", "needs_subject", "rules_unavailable", "subject_mismatch",
            "needs_subject_binding", "partial", "awaiting_validation", "needs_source", "needs_saved_build", "needs_cone_data"})
    boundary_summary = {
        "state": _code(boundary.get("state"), {"retained", "partial", "rejected"}),
        "reason": _code(boundary.get("reason"), {"compatible", "fields_revoked", "object_replaced", "epoch_changed",
            "new_question", "capture_failed", "observer_unavailable", "source_unconfirmed", "pending_unproved",
            "page_changed", "revision_unproved", "unscoped_revision", "page_control_challenged", "page_proof_missing"}),
        "source_serial": _number(boundary.get("source_serial")), "current_serial": _number(boundary.get("current_serial")),
        "withheld_fields": sorted({f for f in boundary.get("withheld_fields", []) if isinstance(f, str) and f in SCREEN_FIELDS}),
    } if isinstance(boundary, dict) and boundary else None
    return {"generation": _number(value.get("generation")),
            "consultation_source": {
                "source": _code((value.get("consultation_source") or {}).get("source"),
                                {"saved_profile", "current_screen", "reference_knowledge", "unresolved"}),
                "state": _code((value.get("consultation_source") or {}).get("state"),
                               {"", "declared", "needs_source", "restored_named_archive"}),
            } if isinstance(value.get("consultation_source"), dict) else None,
            "request_completion": completion_summary,
            "assessment": _assessment_summary(value.get("assessment")),
            "boundary": boundary_summary,
            "origin": _code(value.get("origin"), {"hsr_direct_tool", "hsr_explicit_followup"}),
            "request_id": value.get("request_id") if re.fullmatch(r"[a-f0-9]{32}", str(value.get("request_id"))) else None,
            "channel": _code(value.get("channel"), {"direct", "followup"}),
            "at": _timestamp(value.get("at")),
            "selection_serial": _number(value.get("selection_serial")),
            "payload_serial": _number(value.get("payload_serial")),
            "answer_state": _code(value.get("answer_state"),
                {"ready", "partial", "unconfirmed", "paused", "capture_failed", "not_requested"}),
            "confirmed_fields": fields("confirmed_fields"),
            "missing_fields": fields("missing_fields"),
            "body_digest": value.get("body_digest") if re.fullmatch(r"[a-f0-9]{64}", str(value.get("body_digest"))) else None,
            "equipment": _handoff_summary(value["equipment"] if isinstance(value.get("equipment"), dict) else {}),
            "equipment_consistency": _code(value.get("equipment_consistency"), {"matches", "mismatch", "not_requested"}),
            "equipment_digest_valid": value.get("equipment_digest_valid") if isinstance(value.get("equipment_digest_valid"), bool) else None,
            "equipment_text_valid": value.get("equipment_text_valid") if isinstance(value.get("equipment_text_valid"), bool) else None,
            "submitted": value.get("submitted") if isinstance(value.get("submitted"), bool) else None,
            "message_projection": projection_summary,
            "ack_scope": "plugin_return_payload_only_not_host_speech" if value.get("channel") == "direct"
                         else "sdk_submission_only_not_host_speech"}


def build_diagnostic_report(*, observer: dict, assistant: dict, evidence: dict,
                            data_pack: dict, avatar_pack: dict, last_tool: dict,
                            speech: dict, message_path: dict,
                            now: datetime | None = None, incomplete_sources: list | None = None,
                            service_request: dict | None = None, equipment_handoffs: list | None = None,
                            prepare_calls: dict | None = None,
                            followup_status: dict | None = None, answer_deliveries: list | None = None,
                            review_state: dict | None = None, page_control_trace: list | None = None,
                            revocation_trace: list | None = None,
                            vision_trace: list | None = None,
                            frame_comparisons: dict | None = None) -> dict:
    """Project explicit safe fields, not arbitrary logs/config/raw errors."""
    checked_at = (now or datetime.now(timezone.utc)).isoformat()
    from .page_control_trace import sanitize_trace
    from .revocation_trace import sanitize as sanitize_revocations
    incomplete = sorted({s for s in (incomplete_sources or []) if s in {"assistant", "query_audit", "observation"}})
    state = _code(observer.get("state"), {"observing", "waiting", "paused", "stopped", "error", "updating"})
    enabled = observer.get("enabled") is True
    capture = assistant.get("capture") or {}
    game = assistant.get("game") or {}
    refresh = last_tool.get("refresh_diagnostics") or {}
    checks = []

    def check(name: str, level: str, detail: str) -> None:
        checks.append({"name": name, "level": level, "detail": detail})

    check("observation", "info" if not enabled else "warning" if state != "observing" else "success",
          "stopped" if not enabled else state)
    check("assistant", "success" if assistant.get("connected") else "warning",
          "connected" if assistant.get("connected") else "not_connected" if assistant.get("installed") else "not_installed")
    check("game", "success" if game.get("running") and not game.get("minimized") else "info",
          "minimized" if game.get("minimized") else "running" if game.get("running") else "not_running")
    check("facts", "info" if not enabled else "success" if evidence.get("fresh") else "warning",
          "unavailable" if not enabled else "ready" if evidence.get("fresh") else "unconfirmed")
    check("knowledge", "success" if data_pack.get("installed") else "warning",
          "installed" if data_pack.get("installed") else "installing" if data_pack.get("state") == "installing" else "not_installed")
    check("message_path", "warning" if message_path.get("available") is False else "info" if message_path.get("available") is None else "success",
          "unavailable" if message_path.get("available") is False else "unknown" if message_path.get("available") is None else "available")
    for source in incomplete:
        check("diagnostic_" + source, "warning", "read_timeout_or_error")
    for item in checks:
        if item["name"] == "assistant" and "assistant" in incomplete or item["name"] == "facts" and "observation" in incomplete:
            item.update(level="warning", detail="not_checked")

    stages = []
    allowed_stages = {
        "query_started", "reuse_miss", "reuse_validated", "reuse_recovered", "old_worker_stop_requested", "old_worker_drained",
        "capture_selected", "analysis_started", "analysis_finished", "analysis_rejected", "analysis_discarded",
        "analysis_validated", "observation_committed", "delivery_rejected", "completed_read_revalidated", "query_finished",
        "capture_rejected", "capture_validated", "query_guards_built", "health_check_started", "health_check_finished",
        "commit_started", "commit_finished",
        "capture_retry",
        "delivery_text_revalidated", "query_joined_worker",
        "reuse_superseded", "reuse_text_revalidated", "reuse_fields_withdrawn",
        "delivery_deferred", "text_verification",
        "vision_turn_acquired", "answer_incomplete", "identity_validation", "delivery_partial", "proof_rejected", "regions_deferred",
        "page_controls_deferred",
        "verification_frame_advanced",
        "producer_proof_consumed",
        "candidate_retained",
        "verification_queued",
        "verification_continued", "verification_resumed", "verification_scope_changed", "page_check_queued", "page_handoff",
        "late_validated_candidate",
        "page_first_scheduled", "native_review_deferred",
        "page_check_result", "page_check_stopped",
        "reuse_postreview_selected",
    }
    allowed_reasons = {
        "no_valid_snapshot", "pixels_changed", "capture_unavailable_during_analysis", "capture_unavailable_after_analysis",
        "page_changed_during_analysis", "page_changed_after_analysis", "capture_unavailable", "page_changed",
        "observation_invalidated", "capture_unavailable_before_delivery", "stale_frame", "query_deadline",
        "busy", "timeout", "superseded", "error", "unavailable", "missing_proof", "confirmed", "unconfirmed",
        "no_answerable_fields",
        "retained_proof_rejected",
        "identity_revision_mismatch", "identity_not_admitted", "identity_proof_missing", "identity_proof_ambiguous",
        "page_controls_missing", "literal_geometry_missing", "guard_coverage_missing",
        "expired_or_invalidated", "candidate_epoch_mismatch", "invalid_timestamp", "observer_not_observing", "invalidated_during_check", "invalid_image",
        "candidate_missing",
        "same_page_incomplete_read",
        "content_or_page_changed", "content_or_page_changed_between_analysis", "visual_snapshot_needs_refresh",
        "unconfirmed_page_after_analysis",
        "page_first",
        "attempt_limit", "request_obsolete", "same_frame", "controls_unchanged", "unknown_page", "same_page",
        "controls_changed", "confirmed_transition", "probe_failed",
    }
    for item in (refresh.get("stages") or [])[-48:]:
        if not isinstance(item, dict):
            continue
        stage = {"stage": _code(item.get("stage"), allowed_stages), "after_seconds": _number(item.get("after_seconds"))}
        for key in ("processing_seconds", "age_seconds", "original_age_seconds", "worker_elapsed_seconds", "health_polls", "frame_transfers", "serial", "current_serial", "region_count", "queue_seconds", "job_id", "frame_seq",
                    "observed", "confirmed", "pixels_unchanged", "text_revalidated", "withheld", "attempt", "probe_seconds",
                    "native_calls", "native_seconds", "before_rows", "after_rows"):
            if key in item:
                stage[key] = _number(item[key])
        if "baseline_updated" in item:
            stage["baseline_updated"] = item["baseline_updated"] is True
        if "scope" in item:
            stage["scope"] = _code(item["scope"], {"full", "scene", "navigation"})
        if "reason" in item:
            stage["reason"] = _code(item["reason"], allowed_reasons)
        if "role" in item:
            stage["role"] = _code(item["role"], {"producer", "delivery", "manual"})
        if "missing_fields" in item and isinstance(item["missing_fields"], list):
            stage["missing_fields"] = sorted({f for f in item["missing_fields"] if isinstance(f, str) and f in SCREEN_FIELDS})
        stages.append(stage)

    from .prepare_trace import public_records
    prepare_calls = prepare_calls if isinstance(prepare_calls, dict) else {}
    snapshot = {
        "schema_version": 1, "plugin_version": PLUGIN_VERSION, "build_revision": BUILD_REVISION, "checked_at": checked_at,
        "service_request": public_service_request(service_request),
        "prepare_calls": {"storage_read": _code(prepare_calls.get("storage_read"), {"available", "unavailable"}),
                          "records": public_records(prepare_calls.get("records"))},
        "diagnostic_read": {"complete": not incomplete, "incomplete_sources": incomplete},
        "review": {"policy": _code((review_state or {}).get("policy"), {"shared_live_review_v1"}),
                   **{k: (review_state or {}).get(k) is True for k in ("has_committed_candidate", "has_reusable_candidate", "invalidated")},
                   **{k: _number((review_state or {}).get(k)) for k in ("epoch", "revision", "challenged_regions")},
                   "pending": _code((review_state or {}).get("pending"), {"none", "control_verification", "capture", "other"})},
        "observer": {"enabled": enabled, "state": state, "proactive_enabled": observer.get("proactive_enabled") is True},
        "assistant": {"installed": assistant.get("installed") is True, "running": assistant.get("running") is True,
                      "connected": None if "assistant" in incomplete else assistant.get("connected") is True,
                      "service_version": str(assistant.get("service_version")) if re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", str(assistant.get("service_version"))) else None,
                      "isolated_window": capture.get("isolated_window") is True},
        "message_path_available": message_path.get("available") if isinstance(message_path.get("available"), bool) else None,
        "current_observation": {"fresh": enabled and evidence.get("fresh") is True,
                                "observed_at": _timestamp(evidence.get("observed_at")),
                                "last_verified_at": _timestamp(evidence.get("last_verified_at")),
                                "freshness_basis": _code(evidence.get("freshness_basis"), {"scoped_live_verification"}),
                                "equipment": _handoff_summary(handoff_receipt(equipment_card(evidence), channel="current_projection")),
                                "field_status": {
                                    "page": "confirmed" if enabled and evidence.get("fresh") and evidence.get("scene", {}).get("verified") else "unconfirmed",
                                    "location": "confirmed" if enabled and navigation_answer(evidence, requested=True)["current_location"]["known"] else "unconfirmed",
                                    "quest": "confirmed" if enabled and navigation_answer(evidence, requested=True)["objective"]["known"] else "unconfirmed",
                                }},
        "resources": {"knowledge_installed": data_pack.get("installed") is True,
                      "portraits_installed": avatar_pack.get("installed") is True},
        "last_query": {"at": _timestamp(last_tool.get("at")),
                       "plan": _plan_summary(last_tool.get("query_plan")),
                       "answer_state_at_audit": _code((last_tool.get("answer_status_at_audit") or {}).get("state"),
                           {"ready", "partial", "unconfirmed", "paused", "capture_failed", "not_requested"}),
                       "missing_fields_at_audit": sorted({f for f in (last_tool.get("answer_status_at_audit") or {}).get("missing_fields", [])
                                                           if isinstance(f, str) and f in SCREEN_FIELDS}),
                       "outcome": _code(refresh.get("outcome"), {"reused", "ready", "partial", "timeout", "cancelled", "observer_unavailable", "not_requested"}),
                       "wait_seconds": _number(refresh.get("wait_seconds")), "stages": stages},
        "explicit_followup": {"state": _code((followup_status or {}).get("state"),
                                  {"pending", "cancelled", "submitted", "submission_failed", "expired", "ended_incomplete", "completed_silent"}),
                              "outcome": _code((followup_status or {}).get("outcome"), {"ready", "partial", "timeout", "transport_limited"}),
                              "result_ready": (followup_status or {}).get("result_ready") is True,
                              "answer_complete": (followup_status or {}).get("answer_complete") is True,
                              "reason": _code((followup_status or {}).get("reason"), {
                                  "activity_unavailable_after_retries", "activity_retry", "new_user_message", "page_changed",
                                  "observer_or_target_changed", "final_guard_rejected", "followup_failed", "replaced",
                                  "new_query", "observation_stopped", "no_new_verified_facts", "discussion_changed",
                                  "confirmed_continue", "awaiting_message_relation", "message_relation_unresolved",
                                  "user_cancelled", "new_request", "new_question", "service_incomplete",
                                  "partial_delivered_missing_fields", "body_not_delivered", "subject_mismatch",
                                  "needs_subject", "rules_unavailable", "empty_projection", "service_projection_incomplete", "no_answer_increment"}),
                              "ack_scope": "sdk_submission_only_not_host_speech"},
        "page_control_trace": sanitize_trace(page_control_trace),
        "revocation_trace": sanitize_revocations(revocation_trace),
        "frame_comparisons": {key: _number((frame_comparisons or {}).get(key, 0))
                              for key in ("calls", "total_seconds", "max_seconds")},
        "vision_trace": [
            {**{key: _number(event[key]) for key in ("age_seconds", "seconds", "pending", "frame_seq",
                "job_id", "verification_frame_seq", "main_stats", "sub_stats", "ocr_lines", "tiles_completed", "tiles_total",
                "tiles_skipped", "priority_tile_count", "serial") if key in event},
             **{key: _timestamp(event[key]) for key in ("capture_at", "verification_frame_at") if key in event},
             **({"relic_priority": event["relic_priority"] is True} if "relic_priority" in event else {}),
             **({"ocr_failed": event["ocr_failed"] is True} if "ocr_failed" in event else {}),
             "stage": _code(event.get("stage"), {"ocr_admission", "ocr_drain", "ocr_drain_error", "ocr_backend_setup",
                 "ocr_backend_fallback", "ocr_call", "ocr_verification", "producer_verification",
                 "delivery_verification", "capture_no_fresh_frame", "capture_waiting_frame",
                 "relic_parsed", "relic_verified", "relic_committed", "relic_job_ended"}),
             **({"outcome": _code(event["outcome"], {"started", "returned", "error", "timeout", "rejected"})} if "outcome" in event else {}),
             **({"role": _code(event["role"], {"producer", "delivery", "manual"})} if "role" in event else {}),
             **({"reason": _code(event["reason"], {"window_capture_no_new_frame", "window_capture_stale_frame",
                 "obsolete_worker_draining", "analysis_superseded", "capture_changed_during_analysis", "candidate_too_old"})}
                if "reason" in event else {}),
             **({"error_type": event["error_type"]} if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,63}",
                str(event.get("error_type") or "")) else {})}
            for event in (vision_trace or [])[-48:] if isinstance(event, dict)
        ],
        "answer_deliveries": [_delivery_summary(d) for d in (answer_deliveries or [])[-8:] if isinstance(d, dict)],
        "equipment_handoffs": [_handoff_summary(h)
            for h in (equipment_handoffs or [])[-8:] if isinstance(h, dict)],
        "speech": {"pending_count": _number(speech.get("pending_count")),
                   "submitted_count": _number((speech.get("counts") or {}).get("submitted", 0)),
                   "can_retract_submitted": False, "ack_scope": "sdk_submission_only"},
    }
    return {"checked_at": checked_at, "checks": checks, "last_query": snapshot["last_query"],
            "copy_text": json.dumps(snapshot, ensure_ascii=False, indent=2)}
