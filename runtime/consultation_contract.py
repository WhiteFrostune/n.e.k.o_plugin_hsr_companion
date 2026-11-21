"""D1 demand/subject declarations, independent of OCR and speech permission.

The host model declares meaning; this module validates structure and identity.
It does not classify the player's words, guess aliases or certify pixels.
"""

import time
from dataclasses import replace

from .contracts import CORRECTION_REASONS as CORRECTION_REASONS
from .contracts import CURRENT_SCOPES

CONSULTATION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "description": "评价当前装备/面板时填写本轮语义声明，和intent一致。通用配装用reference_build；"
                   "不要把眼前装备适配声明成通用攻略。saved_cone查玩家保存的光锥，不读眼前装备；"
                   "current_cone才读当前画面。角色省略、指定新角色、未解析、当前画面是不同状态。"
                   "纯读屏不要填consultation；当前画面评价且玩家没点名角色时用current。",
    "properties": {
        "scope": {"type": "string", "enum": ["current_relic", "current_cone", "current_panel", "reference_build", "saved_cone"]},
        "subject_state": {"type": "string", "enum": ["inherit", "specified", "unresolved", "current"],
                          "description": "inherit仅玩家省略角色、沿用有效讨论；specified为本轮指定且已理解的角色，必须给character；"
                                         "明确新对象但没解析成功用unresolved，不能改用旧角色；"
                                         "current仅用于current_*且当前画面就是对象，不填character。"},
        "character": {"type": "string", "maxLength": 80,
                      "description": "仅specified填写规范角色名；模型确认的错字/别名须在此落到规范名，"
                                     "有歧义则unresolved并澄清。不是视觉身份或账号拥有证明。"},
        "topics": {"type": "array", "uniqueItems": True, "minItems": 1, "maxItems": 2,
                   "items": {"type": "string", "enum": ["relic", "cone"]},
                   "description": "本题实际要求的配装主题，可并列；排除玩家不要的主题。不提供时省略，不填空数组。不是当前装备事实。"},
        "answer_parts": {"type": "array", "uniqueItems": True, "minItems": 1, "maxItems": 4,
                         "items": {"type": "string", "enum": ["rows", "assessment", "next_step", "options"]},
                         "description": "玩家要听的内容，不是评价前需要读取的数据。普通‘给谁用怎么样’选assessment；"
                                        "rows仅玩家要求逐项读数时选，并配response_detail=full；不能因需读全装备就要求朗读。"
                                        "next_step/options按本题需求选。不提供时省略，不填空数组。"},
    },
    "required": ["scope", "subject_state"],
}
SCOPE_INTENTS = {
    "current_relic": {"assess_relic"}, "current_cone": {"assess_cone"},
    "current_panel": {"assess_panel"}, "reference_build": {"build_guide", "equipment_guide"},
    "saved_cone": {"assess_cone", "equipment_guide"},
}


def validate_declaration(value, *, intent, previous=None):
    if value is None:
        return None  # Backward-compatible calls are not proof of model routing.
    if not isinstance(value, dict) or set(value) - {"scope", "subject_state", "character", "topics", "answer_parts"}:
        raise ValueError("invalid_consultation_declaration")
    scope, state, character = value.get("scope"), value.get("subject_state"), value.get("character", "")
    if (not isinstance(scope, str) or scope not in SCOPE_INTENTS
            or state not in ("inherit", "specified", "unresolved", "current")
            or not isinstance(character, str) or len(character) > 80
            or state == "specified" and not character.strip()
            or state != "specified" and character
            or state == "current" and scope not in CURRENT_SCOPES):
        raise ValueError("invalid_consultation_subject")
    # A declared saved source with a newly specified subject can be resolved
    # from the archive even after discussion expiry. This restores NO old team
    # or visual values and does not extend the discussion TTL.
    effective = ("assess_cone" if scope == "saved_cone" and intent == "continue_consultation" else
                 (previous or {}).get("execution_intent", "") if intent == "continue_consultation" else intent)
    if intent == "continue_consultation" and not effective:
        # Old bounded contexts predate execution_intent; use their declared
        # task fields, not a new interpretation of the current question.
        prior = previous or {}
        effective = ("assess_relic" if prior.get("request_focus") == "equipment" and prior.get("equipment_kinds")
                     else "assess_cone" if prior.get("request_focus") == "equipment"
                     else "assess_panel" if prior.get("request_focus") == "attributes" else "")
    if (effective and effective not in SCOPE_INTENTS[scope]) or (not effective and intent != "continue_consultation"):
        raise ValueError("scope_intent_conflict: 声明范围与intent冲突；请澄清或按实际需求重新调用，不执行另一种服务")
    result = {"scope": scope, "subject_state": state, "character": character.strip()}
    for key, allowed in (("topics", {"relic", "cone"}), ("answer_parts", {"rows", "assessment", "next_step", "options"})):
        if key in value:
            items = value[key]
            if (not isinstance(items, list) or not items or any(not isinstance(x, str) or x not in allowed for x in items)
                    or len(items) != len(set(items))):
                raise ValueError("invalid_consultation_" + key)
            result[key] = list(items)
    return result


def _valid_previous(previous):
    if not isinstance(previous, dict) or not previous.get("character_ids"):
        return False
    try:
        age = time.monotonic() - float(previous.get("at"))
    except (TypeError, ValueError):
        return False
    return 0 <= age <= 600.0


def normalize_model_consultation(value, *, intent, screen_fields=(), previous=None):
    """Correct current-screen model overfills before the strict D1 contract.

    A pure read_screen drops any evaluation declaration; a current evaluation
    with no character is marked current. Other intents keep their existing
    strict behavior.
    """
    if not isinstance(value, dict):
        return value, []
    scope = value.get("scope")
    state = value.get("subject_state")
    character = str(value.get("character") or "").strip()
    if intent == "read_screen":
        # A pure screen read has no evaluation subject. Any model-supplied
        # declaration is stale routing metadata and must not block the read,
        # whether or not it names a character.
        if scope in CURRENT_SCOPES and not character:
            return None, [{
                "reason": "read_screen_current_scope_dropped",
                "original_scope": scope,
                "original_subject_state": state,
                "intent": intent,
            }]
        return None, [{
            "reason": "read_screen_declaration_dropped",
            "original_scope": scope,
            "original_subject_state": state,
            "intent": intent,
        }]
    if intent in {"team_guide", "challenge_guide"} and scope == "reference_build":
        # A team question may inherit a stale reference-build declaration from
        # the previous relic/cone turn. It cannot describe this service, so
        # drop it instead of failing the whole prepare before any screen read.
        return None, [{
            "reason": "stale_reference_build_for_team_dropped",
            "original_scope": scope,
            "original_subject_state": state,
            "intent": intent,
        }]
    if scope not in CURRENT_SCOPES or character:
        return value, []
    expected = {"current_relic": "assess_relic", "current_cone": "assess_cone",
                "current_panel": "assess_panel"}[scope]
    if intent != expected:
        return value, []
    if state in {"specified", "unresolved"} or (state == "inherit" and not _valid_previous(previous)):
        return {**value, "subject_state": "current"}, [{
            "reason": "current_screen_subject_inferred",
            "original_scope": scope,
            "original_subject_state": state,
            "corrected_scope": scope,
            "corrected_subject_state": "current",
            "intent": intent,
        }]
    return value, []


def answer_requirements(plan, query, *, declaration=None, previous=None, inherit_parts=False):
    """Normalize demand ONCE at the existing planning boundary.

    New callers declare semantics. Legacy calls use a bounded clause fallback;
    selectors/formatters must not reinterpret the question a second time.
    This neither changes pixel requirements nor supplies missing evidence.
    """
    import re

    topics = (declaration or {}).get("topics")
    if topics is None:
        positive, excluded = set(), set()
        for clause in re.split(r"[，,。；;！!？?\n]", query):
            for match in re.finditer(r"遗器|套装|光锥", clause):
                topic = "cone" if match[0] == "光锥" else "relic"
                prefix = clause[:match.start()]
                # A positive scope boundary resets an earlier negation. This
                # compatibility parser is not used after explicit declaration.
                prefix = re.split(r"只看|只讲|只说|改看|改讲|而是", prefix)[-1]
                (excluded if re.search(r"不用|不要|无需|不必|别讲|先不", prefix) else positive).add(topic)
        topics = positive - excluded if positive or excluded else set(previous.get("guide_topics") or ()) if previous else set()
        if not topics and not (positive or excluded):
            topics = {"relic", "cone"}
    parts = (declaration or {}).get("answer_parts")
    if parts is None and inherit_parts and previous and previous.get("answer_parts"):
        parts = set(previous["answer_parts"])
        # The upstream planner already accepted a new brief preference. It
        # must not be undone by the old full-list part on a continuation.
        if plan.reply_detail != "full":
            parts.discard("rows")
    if parts is None:
        parts = set()
        if plan.reply_detail == "full" and plan.request_focus in {"equipment", "attributes"}:
            parts.add("rows")
        if plan.consultation_action == "assess" and (plan.reply_detail != "full" or re.search(r"适合|适配|评价|怎么样", query)):
            parts.add("assessment")
        if re.search(r"下一步|接下来|怎么改|怎么优化|先养|先提升|要注意", query):
            parts.add("next_step")
        if plan.consultation_action in {"equipment_guide", "build_guide", "team_guide"}:
            parts.add("options")
    # Detail has ONE authority: the existing upstream normalization of the
    # player's detail demand / response_detail. Do not let answer_parts create
    # a second implicit full mode merely because assessment needs all rows.
    # Retain the raw over-wide declaration in prepare_trace, not in the speech
    # requirement. This changes neither reading coverage nor numeric evidence.
    parts = set(parts)
    if plan.reply_detail == "full" and plan.request_focus in {"equipment", "attributes"}:
        parts.add("rows")
    elif "rows" in parts:
        parts.discard("rows")
        if plan.consultation_action == "assess":
            parts.add("assessment")
    return replace(plan, guide_topics=frozenset(topics), answer_parts=frozenset(parts))


def assessment_completion(plan, assessment, *, role, catalog):
    """Reading and sourced assessment have separate success states.

    Never use this status to delete observations or forbid partial fact output.
    It only decides whether waiting for more *pixels* can finish this service.
    """
    requested = bool(assessment.get("requested"))
    ids = assessment.get("subject_ids") or []
    names = [(catalog.get(key) or {}).get("name") for key in ids]
    evaluation = assessment.get("evaluation") or {}
    available = bool(assessment.get("evaluation_available"))
    evidence_complete = (bool((assessment.get("equipment_answer") or {}).get("complete"))
                         if evaluation.get("kind") == "equipment"
                         else assessment.get("state") == "evidence_available")
    has_rules = bool(role and role.get("source_refs"))
    state = ("not_requested" if not requested else "needs_subject" if len(ids) != 1
             else "rules_unavailable" if not has_rules
             else "subject_mismatch" if assessment.get("panel_subject_state") == "mismatch"
             else "needs_subject_binding" if assessment.get("panel_subject_state") == "unconfirmed"
             else "complete" if available and evidence_complete
             else "partial" if available else "needs_evidence")
    return {"requested": requested, "state": state, "reading_state": assessment.get("state", "not_requested"),
            "reading_complete": evidence_complete, "evaluation_available": available,
            "evaluation_complete": state == "complete", "subject_state": plan.subject_state,
            "subject_reason": plan.subject_reason,
            "subject_ids": list(ids), "subject_names": [name for name in names if name],
            "subject_origin": assessment.get("subject_origin", plan.target_origin),
            "panel_subject_state": assessment.get("panel_subject_state", "not_applicable"),
            "can_wait_for_evidence": bool(requested and len(ids) == 1 and has_rules and state != "subject_mismatch"),
            "instruction": "数值可独立交付；读取完成不等于适配完成。needs_subject只澄清角色，不等截图；"
                           "rules_unavailable说明规则覆盖不足，不谎称画面加载。partial只评价已确认项，不补造缺项。"
                           "subject_mismatch/needs_subject_binding只可报未归属的当前读数，不能套指定角色的养成结论；"
                           "前者澄清当前选中角色，后者说明尚未确认面板归属。"}


def bound_followup_plan(plan, guidance):
    """Freeze the resolved discussion role, NEVER the old item's facts."""
    completion = guidance.get("completion") or {}
    if not completion.get("requested"):
        return plan
    return replace(plan, character_ids=tuple(completion.get("subject_ids") or ()),
                   target_origin=completion.get("subject_origin", plan.target_origin), allow_visual_subject=False)
