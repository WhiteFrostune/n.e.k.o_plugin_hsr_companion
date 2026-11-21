"""Small shared routing contract; prompts do not enforce host tool selection."""

import copy
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from uuid import uuid4

from .context_text import SERVICE_ROUTING_RULE as SERVICE_ROUTING_RULE

PREPARE_TOOL_DESCRIPTION = (
    "星铁答前调用，query保留原话。只读遗器词条用read_screen+equipment、full；适配才用assess_relic。"
    "光锥区分当前与saved_cone；通用培养用reference_build；按档案推荐一队直接用team_guide，无需预查档案或读屏。"
    "评价声明scope和subject_state；discussion_character是讨论对象提示。换件只继承角色和目的。"
    "普通评价按reply_plan答重点。完整请求用full，词条按equipment_answer原值。缺评价角色先澄清。"
    "仅followup.pending=true才有补答；待办确认和取消用pending_reply；玩家补数用player_reply。"
)



def public_service_request(value):
    """No raw question, role name, game text, path or arbitrary exception."""
    value = value if isinstance(value, dict) else {}
    state = value.get("state")
    try:
        stamp = datetime.fromisoformat(str(value.get("started_at", "")))
        started_at = stamp.isoformat() if stamp.tzinfo is not None else None
    except ValueError:
        started_at = None
    return {"state": state if isinstance(state, str) and state in {"started", "planned", "completed", "failed", "cancelled"} else "not_checked",
            "request_id": value.get("request_id") if re.fullmatch(r"[a-f0-9]{32}", str(value.get("request_id"))) else None,
            "started_at": started_at,
            "generation": value.get("generation") if type(value.get("generation")) is int else None,
            "tool_selection_enforced": False}


def mark_service_request(owner, generation, state):
    from .prepare_trace import request_id
    previous = getattr(owner, "_service_request", {})
    if state == "started" or previous.get("generation") == generation:
        owner._service_request = {"state": state, "generation": generation,
                                  "request_id": (request_id(owner) or uuid4().hex) if state == "started" else previous.get("request_id"),
                                  "started_at": datetime.now(timezone.utc).isoformat() if state == "started"
                                  else previous.get("started_at")}


def response_contract(owner, generation, *, channel, request_id=None):
    """Plugin request identity is NOT the desktop websocket/session identity.

    Public SDK does not expose a reliable desktop Reload signal. Retain the
    bounded discussion across a reconnect; every item still needs its own
    current snapshot. Never reset observations just because a caller reconnects.
    """
    service = getattr(owner, "_service_request", {})
    if request_id is None and service.get("generation") == generation:
        request_id = service.get("request_id")
    return {
        "source": "hsr_companion", "channel": channel, "request_id": request_id,
        "generation": generation, "discussion_generation": getattr(owner, "_discussion_generation", 0),
        "desktop_reload_is_context_reset": False, "host_connection_identity": "not_exposed",
        "current_equipment_rule": "this_request_snapshot_not_discussion_history",
        "external_task_control": "not_available", "ack_scope": "plugin_payload_not_host_speech",
        "instruction": "本包是这个问题的插件结果；补答替换同题旧的未确认状态，不开启第二个任务。"
                       "首答等待未读到当前画面时先说‘稍等我看一下’，不要道歉、不要让玩家移动光标；"
                       "后台补答沿用原题详细程度。"
                       "后台网页不能补齐当前装备读数；未另有明确联网请求时，不为本包已回答或已登记补答的事项再派相同查询。"
                       "桌面重新加载不是删除玩家资料或重置插件讨论；不能把旧讨论装备当作现在画面。",
    }


def clear_discussion(owner):
    owner._guidance_context = None
    owner._equipment_reply_pending = None
    owner._discussion_generation = getattr(owner, "_discussion_generation", 0) + 1


def configure_delivery_trace(owner, *, seconds):
    """Explicit short-lived opt-in. No image, arbitrary payload or disk writes."""
    seconds = min(120, max(0, seconds))
    owner._delivery_trace_until = time.monotonic() + seconds
    owner._delivery_value_trace = []


def delivery_value_trace(owner):
    remaining = max(0, getattr(owner, "_delivery_trace_until", 0) - time.monotonic())
    if not remaining:
        owner._delivery_value_trace = []
    return {"enabled": bool(remaining), "remaining_seconds": round(remaining, 1),
            "entries": copy.deepcopy(getattr(owner, "_delivery_value_trace", [])),
            "ack_scope": "plugin_payload_only_not_host_speech"}


def _trace_card(card):
    # Only the necessary field packet; never keep questions, OCR dumps or
    # arbitrary caller-supplied dictionaries. Limits also bound memory use.
    def short(value, limit=96):
        return str(value)[:limit] if value is not None else None

    def row(value):
        return {k: short(value.get(k)) for k in ("label", "value", "property")} if isinstance(value, dict) else None

    return {**{k: short(card.get(k)) for k in ("name", "slot", "level", "snapshot_id", "content_digest")},
            "main_stat": row(card.get("main_stat")),
            "sub_stats": [row(r) for r in (card.get("sub_stats") or [])[:4]],
            "exact_text": short(card.get("exact_text"), 1200)}


def record_answer_delivery(owner, *, generation, channel, payload, visual, answer,
                           selection_serial=None, request_id=None, submitted=None, message_projection=None):
    """Bounded final-boundary proof; detailed rows only during explicit opt-in.

    The digest covers the body before this receipt is attached. Direct means
    a constructed plugin return, followup means an SDK submission attempt.
    Neither is a receipt for host generation, playback or transcription.
    """
    from .equipment_view import equipment_card, equipment_digest, equipment_exact_text, handoff_receipt
    selected = equipment_card(visual)
    requested = isinstance(payload.get("equipment_answer"), dict) and payload["equipment_answer"].get("state") != "not_requested"
    sent = copy.deepcopy(payload["equipment_answer"]) if requested else equipment_card({"fresh": False})
    declared_digest = sent.get("content_digest")
    sent["content_digest"] = equipment_digest(sent)
    text_valid = sent.get("exact_text") == equipment_exact_text(sent)
    consistency = "matches" if sent["content_digest"] == selected["content_digest"] and text_valid else "mismatch"
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    receipt = {"generation": generation, "channel": channel,
               "origin": "hsr_direct_tool" if channel == "direct" else "hsr_explicit_followup",
               "at": datetime.now(timezone.utc).isoformat(),
               "selection_serial": selection_serial,
               "payload_serial": (visual.get("screen_facts") or {}).get("observation_serial", 0),
               "answer_state": answer["state"], "confirmed_fields": answer["confirmed_fields"],
               "missing_fields": answer["missing_fields"],
               "body_digest": hashlib.sha256(body.encode()).hexdigest(),
               "equipment": handoff_receipt(sent, channel=channel),
               "equipment_consistency": consistency if requested else "not_requested",
               "equipment_digest_valid": declared_digest == sent["content_digest"] if requested else None,
               "equipment_text_valid": text_valid if requested else None,
               "ack_scope": "plugin_return_payload_only_not_host_speech" if channel == "direct"
                            else "sdk_submission_only_not_host_speech"}
    boundary = visual.get("delivery_boundary")
    if isinstance(boundary, dict):
        receipt["boundary"] = copy.deepcopy(boundary)
    completion = (payload.get("guidance_answer") or {}).get("completion") or {}
    guidance = payload.get("guidance_answer") or {}
    if guidance.get("requested"):
        receipt["consultation_source"] = {"source": guidance.get("evidence_source"),
                                          "state": guidance.get("source_state")}
    if isinstance(payload.get("request_completion"), dict):
        receipt["request_completion"] = copy.deepcopy(payload["request_completion"])
    if completion.get("requested"):
        receipt["assessment"] = {key: completion.get(key) for key in (
            "state", "reading_state", "reading_complete", "evaluation_available", "evaluation_complete", "subject_state", "panel_subject_state")}
        receipt["assessment"]["subject_count"] = len(completion.get("subject_ids") or [])
    # The active service can already belong to the NEXT question after an
    # awaited audit. The detached packet keeps its own identity; never relabel
    # an older result with a new active request, or silently lose its origin.
    contract = payload.get("response_contract")
    if (request_id is None and isinstance(contract, dict) and contract.get("source") == "hsr_companion"
            and contract.get("generation") == generation and contract.get("channel") == channel
            and re.fullmatch(r"[a-f0-9]{32}", str(contract.get("request_id")))):
        request_id = contract["request_id"]
    if request_id is not None:
        receipt["request_id"] = request_id
    elif getattr(owner, "_service_request", {}).get("generation") == generation:
        receipt["request_id"] = owner._service_request.get("request_id")
    if submitted is not None:
        receipt["submitted"] = submitted
    if message_projection is not None:
        receipt["message_projection"] = copy.deepcopy(message_projection)
    owner._answer_deliveries = [*getattr(owner, "_answer_deliveries", []), copy.deepcopy(receipt)][-8:]
    if getattr(owner, "_delivery_trace_until", 0) > time.monotonic():
        entry = {"generation": generation, "channel": channel, "at": receipt["at"],
                 "consistency": receipt["equipment_consistency"],
                 "digest_valid": receipt["equipment_digest_valid"], "body_digest": receipt["body_digest"],
                 "selected_equipment": _trace_card(selected), "sent_equipment": _trace_card(sent)}
        owner._delivery_value_trace = [*getattr(owner, "_delivery_value_trace", []), entry][-8:]
    elif hasattr(owner, "_delivery_value_trace"):
        owner._delivery_value_trace = []
    return receipt
