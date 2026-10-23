"""Purpose-sized cone-fit notification; full source remains in the tool packet.

No OCR, new knowledge, source substitution or literal repair. Never apply to
explicit complete requests, multiple cones, mixed demands or unassessed facts.
"""
from __future__ import annotations

import hashlib
import json


def summarize_cone_fit(data, *, plan, preview):
    from .page_message import requires_full

    guidance = data.get("guidance_answer") or {}
    reply = guidance.get("reply_plan") or {}
    assessment = guidance.get("assessment") or {}
    completion = guidance.get("completion") or {}
    evaluation = assessment.get("evaluation") or {}
    if (plan.fields != {"equipment"} or requires_full(plan) or reply.get("mode") == "full"
            or not reply.get("enabled") or evaluation.get("kind") != "light_cone"
            or not evaluation.get("available") or not evaluation.get("sources")
            or completion.get("evaluation_complete") is not True):
        return None
    facts = (data.get("screen_facts") or {}).get("facts") or []
    cones = {f.get("entity_id") for f in facts if f.get("kind") == "entity_label"
             and f.get("entity_type") == "light_cone" and f.get("entity_id")}
    if cones != {evaluation.get("entity_id")} or (data.get("grounding") or {}).get("needs_target_clarification"):
        return None
    labels = {f.get("value") for f in facts if f.get("kind") == "entity_label"
              and f.get("entity_type") == "light_cone"}
    if labels != {evaluation.get("name")}:
        return None
    names = completion.get("subject_names") or []
    conditions = evaluation.get("conditions") or []
    # Content selection belongs to reply_plan. This fallback only transports
    # its selected, grounded reason and WHOLE condition; it must not rank a
    # second set of advice or substitute a different next step.
    reason = reply.get("conclusion")
    condition = reply.get("next_step")
    if (len(names) != 1 or len(completion.get("subject_ids") or []) != 1
            or completion.get("subject_ids") != assessment.get("subject_ids")
            or not evaluation.get("name") or not reason or reason != evaluation.get("reason")
            or not condition or condition not in conditions):
        return None
    # Select one WHOLE reviewed condition, never cut characters or turn
    # database placeholders into an invented superimposition-specific effect.
    text = "\n".join(["星铁同题补答（适配摘要，非完整效果）：",
        "评价：" + json.dumps(names[0], ensure_ascii=False) + "／" + json.dumps(evaluation["name"], ensure_ascii=False),
        "依据：" + reason, "建议：" + condition, "仅作该光锥参考，不代表毕业；不追问。"])
    try:
        if preview(text) != text:
            return None
    except Exception:
        return None
    satisfied = (data.get("answer_progress") or {}).get("complete", True) is True
    receipt = {"mode": "cone_fit_summary", "complete": True, "has_content": True,
        "answer_complete": satisfied, "service_satisfied": satisfied, "requested_detail": "normal",
        "required_rows": 4, "message_rows": 4, "all_input_rows_delivered": False,
        "carried_sections": ["subject", "current_cone", "reviewed_fit", "reviewed_condition"],
        "omitted_sections": ["full_catalog_effect", "additional_reviewed_conditions"],
        "used_condition_indices": [conditions.index(condition)], "text_sha256": hashlib.sha256(text.encode()).hexdigest()}
    from .answer_message import section_receipt
    section_receipt(receipt, reply, {"assessment", "next_step", *receipt["carried_sections"]})
    return text, receipt
