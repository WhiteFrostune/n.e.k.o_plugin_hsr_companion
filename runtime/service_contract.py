"""Small shared routing contract; prompts do not enforce host tool selection."""

import copy
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from uuid import uuid4

PREPARE_TOOL_DESCRIPTION = (
    "星铁统一入口：画面、角色、任务、遗器光锥与配队；答前调用，query保留原话，追问补数也调用。"
    "精确intent选目的，read_screen传必要screen_fields。"
    "眼前装备/面板用评价intent，传consultation且scope匹配；存档光锥用saved_cone，不读屏；"
    "subject_state选inherit沿用、specified指定、unresolved未解析；指定时填规范character。"
    "discussion_character兼容旧调用；讨论对象不是视觉身份。换件沿用目的与角色，不沿用旧词条。"
    "通用配装用build_guide/equipment_guide，不强制读屏；consultation.topics声明实际主题，可并列，排除不要的。"
    "answer_parts声明玩家要听的内容，不是内部读取需求；rows只与完整逐项需求的response_detail=full配套。"
    "equipment_answer取原值，guidance_answer.reply_plan给评价；completion分开读数/评价，完整词条不漏。"
    "缺角色先澄清；followup.pending才有补答。待办确认/取消按pending_reply_contract传pending_reply，不重启查询；新需求正常调用。"
    "遗器补数用player_reply传本条原话，不当视觉确认。答完交还话轮。"
)

SERVICE_ROUTING_RULE = (
    "若此前followup.pending=true，用户接话先判断与原待办关系：确认继续/取消按pending_reply_contract调用同一入口，重复确认不延长等待；新问题正常调用新需求替换旧待办。"
    "玩家问星铁或继续星铁追问时先调用hsr_companion_prepare，query保留原问题；"
    "优先按工具schema传精确intent及必要screen_fields，不把建议和页面用途改成任务导航。"
    "补一个角色名、缺队友、没专武、眼前光锥是否适合也不例外。"
    "装备/面板需求中的‘看看’‘这个呢’继续原需求；只认部位或套装不等于读到词条，不能凭资料候选宣布适配。"
    "discussion_character只携带先前已明确的讨论对象，不是当前画面事实。"
    "评价眼前遗器/光锥/面板分别用assess_relic/assess_cone/assess_panel，并传consultation的对应scope。"
    "问存档配队中某角色的光锥影响用assess_cone或continue_consultation，scope=saved_cone，specified填角色；"
    "不要因出现‘这把光锥’就用current_cone。只有明确问眼前装备才声明current_cone；来源不明先澄清。"
    "讨论过期后，明确角色的saved_cone重新查本次档案；仅指‘这队/她’不能复活过期名单，也不能改成读屏。"
    "通用配装才用build_guide/equipment_guide与reference_build；不能把当前遗器适配误声明为通用攻略。"
    "consultation.subject_state明确省略沿用inherit、本轮指定specified、未解析unresolved；specified填已理解的规范角色名。"
    "若已理解玩家别名/错字对应角色，query保留原话，character填规范名；不确定就unresolved并澄清，不能偷偷取旧角色。"
    "换件追问传continue_consultation、同scope和inherit；只延续已确认角色与目的，不携带上一件事实。"
    "若目的和scope均声明错，插件无法凭空纠正；错误或冲突参数不能视为已完成评价。"
    "completion.state=needs_subject先澄清角色，不承诺等截图；rules_unavailable明确规则缺口。"
    "词条读取与适配完成分开；只用本轮角色、当前装备和有来源结论，不因缺评价删除正确词条，也不因词条齐全说评价完成。"
    "观察、版本资料、账号条件和建议分开；此前推荐的装备不能当作眼前装备。"
    "报出的装备词条以本次equipment_answer原值为准，保留数值与百分号；缺行不补，推荐词条不是实际词条。"
    "表达范围统一按guidance_answer.reply_plan；consultation.topics声明本题需要的主题（可并列、排除不要的），"
    "answer_parts声明玩家要听的评价、下一步或候选；需要完整数据来判断不等于要求朗读数据。"
    "明确读数用rows及response_detail=full，完整/逐项请求仍列全已确认项。"
    "咨询中玩家补充循环、输出或生存需求仍调用统一入口，不重新背整篇配装。"
    "重点/怎么养等自然追问沿用仍有效的咨询对象；需求已回答就交还话轮，不把可选资料缺失或等待玩家回答变成补答。"
    "属性多少合适及随后玩家补数也调用；玩家数值不是画面，未提供阈值不编数值目标或宣称越高越好。"
    "遗器缺项追问后玩家补数，query保留原话并用player_reply传本条回答；只按player_reply_contract绑定讨论快照，"
    "不能改成‘屏幕上是不是这个数’；未绑定则澄清，补数不写当前视觉或永久档案。"
    "有stat_advice时，触发点、机制上限与养成建议分开；先给一个能执行的检查点，不把原始机制上限叫毕业线。"
    "问号不猜原因；玩家说读错了先重新确认眼前对象，不能继续评价旧装备。"
    "过期或未确定的讨论对象先确认，不用通用加攻/回能模板自行补配队。"
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
    from .equipment import equipment_card, equipment_digest, equipment_exact_text, handoff_receipt
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
