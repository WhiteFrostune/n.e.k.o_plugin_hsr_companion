"""A notification is a bounded text channel, not a full tool-return channel.

Project only requested current facts. Never place audit/guide JSON ahead of
numbers. Keep the full packet in normal tool output and the existing audit.
No OCR, model, state write or host configuration change.
"""
from __future__ import annotations

import hashlib
import json


def bridge_preview(text):
    """Read-only compatibility probe; no dependency is installed or vendored.

    This host already sanitizes notifications with this formatter. Optional
    import keeps older hosts loadable. Without it, use the conservative UTF-8
    byte bound (at most 200 UTF-8 bytes means at most 200 byte-level tokens).
    Caller runs this potentially cold host formatter off the event loop.
    """
    try:
        from utils.result_parser import parse_push_message_content
    except ImportError:
        return text if len(text.encode("utf-8")) <= 200 else None
    return parse_push_message_content(text, lang="zh")


def _safe(value):
    # This is display escaping, not OCR normalization. In particular no value
    # or percent-sign replacement, fuzzy matching, or property substitutions.
    return str(value or "").replace("\r", " ").replace("\n", " ")


def section_receipt(receipt, reply, carried):
    """Coverage of the selected answer AFTER formatting, not a new selector."""
    required = set(reply.get("required_sections") or [])
    carried = set(carried) if receipt.get("complete") else set()
    missing = required - carried
    receipt.update(carried_sections=sorted(carried), required_sections=sorted(required),
                   missing_sections=sorted(missing))
    receipt["answer_complete"] = bool(receipt.get("answer_complete") and not missing)
    receipt["service_satisfied"] = receipt["answer_complete"]
    return receipt


def render_followup(data, *, plan, ready):
    """Return text plus a safe receipt for what can survive this host bridge.

    Optional prose may be dropped, requested facts may NOT be truncated.
    If the complete essential packet cannot cross, emit an explicit limited
    notice instead of allowing the host to silently cut a five-row answer.
    This receipt proves a dry-run boundary, never model generation/playback.
    """
    if not ready:
        text = "星铁原问题补答：本次仍未确认所需画面字段，结束等待；不引用旧装备，不承诺再回复，不追问或闲聊。"
        return text, {"mode": "timeout_notice", "complete": False, "required_rows": 0,
                      "message_rows": 0, "text_sha256": hashlib.sha256(text.encode()).hexdigest()}
    reply = ((data.get("guidance_answer") or {}).get("reply_plan") or {})
    full = reply.get("mode") == "full" or not reply.get("enabled")
    if not full:
        from .cone_message import summarize_cone_fit
        summary = summarize_cone_fit(data, plan=plan, preview=bridge_preview)
        if summary is not None:
            return summary
    essential = ["星铁同题补答：直接回答，不念核验过程，不用旧件，不追问。"]
    optional = []
    carried = set()
    count = 0
    progress = data.get("answer_progress") or {}
    if progress.get("complete") is False:
        essential.append("部分进展；尚待确认：" + "、".join(progress.get("missing_fields") or []) + "。不宣称整题完成。")
    card = data.get("equipment_answer") or {}
    relic_fields = bool(card.get("name") or card.get("slot") or card.get("main_stat") or card.get("sub_stats"))
    if "equipment" in plan.fields and relic_fields and card.get("state") not in {None, "unconfirmed", "conflict"}:
        essential.append("当前装备：" + "／".join(_safe(card[k]) for k in ("name", "slot", "level") if card.get(k)))
        if full:
            kinds = plan.required_equipment_kinds
            rows = ([{**card["main_stat"], "kind": "main_stat"}] if "main_stat" in kinds and card.get("main_stat") else [])
            if "sub_stat" in kinds:
                rows += [{**r, "kind": "sub_stat"} for r in card.get("sub_stats") or []]
        else:
            rows = [r for r in reply.get("evidence_rows") or [] if r.get("speak_value")]
            # Summary selection and OCR completeness are separate contracts.
            # Without this status, a 4/4 card projected to two useful rows
            # sounds like a new partial read to the receiving character.
            if not card.get("complete"):
                essential.append("仅据已确认项评价，未读项不补。")
        for kind, title in (("main_stat", "主"), ("sub_stat", "副")):
            selected = [r for r in rows if r.get("kind") == kind]
            if selected:
                essential.append(title + "：" + "；".join(_safe(r["label"]) +
                    (_safe(r["value"]) if full or r.get("speak_value", True) else "") for r in selected))
                count += len(selected)
                carried.add("rows")
        if ("sub_stat" in plan.required_equipment_kinds and not card.get("sub_stats_complete")
                or "main_stat" in plan.required_equipment_kinds and not card.get("main_stat")):
            essential.append("仅已确认项，缺项未读全，不补。")
    facts = (data.get("screen_facts") or {}).get("facts") or []
    if "attributes" in plan.fields and reply.get("enabled") and not full:
        facts = [*([f for f in facts if f.get("scope") != "attributes"]),
                 *[{**r, "scope": "attributes"} for r in reply.get("evidence_rows") or [] if r.get("speak_value")]]
    for f in facts:
        semantic_label = (f.get("kind") == "entity_label" and f in plan.relevant_facts([f])
                          and "equipment" in plan.fields and f.get("entity_type") in {"light_cone", "relic_set", "item"})
        if (f.get("scope") == "equipment" and relic_fields
                or f.get("scope") in {"location", "quest"} and data.get("navigation")
                or f.get("scope") not in plan.fields and not semantic_label or not f.get("value")):
            continue
        essential.append(_safe(f.get("label") or f.get("kind")) +
                         ("：" + _safe(f["value"]) if full or f.get("speak_value", True) else ""))
        count += 1
        if f.get("scope") == "attributes":
            carried.add("rows")
    # Same grounded lookup as the direct tool, tied to ONE current cone label.
    # Never pick one of several labels or expand unknown superimposition.
    cones = {f.get("entity_id") for f in facts if f.get("kind") == "entity_label" and f.get("entity_type") == "light_cone"}
    grounding = data.get("grounding") or {}
    if full and len(cones) == 1 and not grounding.get("needs_target_clarification"):
        for record in grounding.get("knowledge_results") or []:
            if record.get("id") in cones and record.get("sources"):
                effect = record.get("rank_effect") or {}
                desc = effect.get("desc") if isinstance(effect, dict) else ""
                if desc:
                    essential.append("资料效果（非实测增益，未确认叠影不展开倍率）：" + json.dumps(desc, ensure_ascii=False))
    nav = data.get("navigation") or {}
    for field, title in (("current_location", "当前位置"), ("quest_title", "任务"),
                         ("objective", "任务目标"), ("destination", "导航目的地")):
        value = nav.get(field) or {}
        if value.get("known"):
            essential.append(title + "：" + _safe(value.get("value")))
            count += 1
    if plan.fields & {"characters", "controlled_character"}:
        names = [_safe(c.get("name")) for c in data.get("characters") or [] if c.get("name")]
        if names:
            essential.append("角色：" + "、".join(names))
            count += len(names)
            if data.get("characters_complete") is False or data.get("team_roster_known") is False:
                read = data.get("team_members_read") or len(names)
                expected = data.get("team_members_expected") or 4
                if data.get("team_roster_known") is False:
                    essential.append(
                        f"名单未完全确认：只读到 {read}/{expected} 位；"
                        "另有位置是空位还是漏读未确认，不当作完整队伍，也不断言空位。")
                else:
                    essential.append(
                        f"名单不完整：只读到 {read}/{expected} 位队员，"
                        "可能还有未识别队员；空位与漏识别不可区分，不当作完整队伍。")
    if "page" in plan.fields and (data.get("scene") or {}).get("verified") and data.get("page_type") not in {None, "unknown"}:
        essential.append("页面：" + _safe((data.get("scene") or {}).get("label") or data.get("page_type")))
        count += 1
    if "visible_text" in plan.fields:
        # Quote untrusted page prose; never copy an OCR-provided instruction.
        essential += ["文字数据：" + json.dumps(r.get("text", ""), ensure_ascii=False)
                      for r in (data.get("screen_facts") or {}).get("visible_text") or []]
        count += len((data.get("screen_facts") or {}).get("visible_text") or [])
    if reply.get("enabled"):
        conclusion = reply.get("conclusion") or ""
        if conclusion:
            # Fit is the original question, not optional collateral prose.
            (optional if full and not reply.get("evaluation_available") else essential).append("依据：" + conclusion)
            if reply.get("evaluation_available"):
                carried.add("assessment")
        if reply.get("next_step") and (not full or "next_step" in reply.get("required_sections", [])):
            essential.append("建议：" + _safe(reply["next_step"]))
            carried.add("next_step")
        if "options" in reply.get("required_sections", []) and reply.get("discussion_options"):
            essential.append("所需候选（资料，非当前装备）：" + json.dumps(reply["discussion_options"], ensure_ascii=False))
            carried.add("options")
    guidance = data.get("guidance_answer") or {}
    completion = guidance.get("completion") or {}
    if completion.get("requested") and completion.get("subject_names"):
        essential.insert(1, "评价角色：" + "、".join(_safe(name) for name in completion["subject_names"]))
    if completion.get("panel_subject_state") == "mismatch" and not reply.get("conclusion"):
        essential.append("当前面板已确认属于另一角色；不能套用指定角色评价，请确认评价对象，不继续等截图。")
    elif completion.get("panel_subject_state") == "unconfirmed" and not reply.get("conclusion"):
        essential.append("面板所属角色未绑定到评价角色；以上仅为未归属读数，不作该角色养成结论。")
    ambiguous_cone = bool(cones and (len(cones) != 1 or grounding.get("needs_target_clarification")))
    if ambiguous_cone:
        essential.append("多个候选，尚未确认所指光锥；不任选一个解释效果。")
    challenge = guidance.get("challenge_plan") or {}
    if challenge.get("requested"):
        # The screen only identifies the stage; do not push all OCR ahead of
        # the actual account advice or fall back to period-wide team examples.
        period = (challenge.get("period") or {}).get("name", "")
        essential = ["星铁原问题补答：" + _safe(period) + "；按已存资料，条件建议，不保证通关。"]
        optional, count = [], 0
        assigned = challenge.get("assignments") or []
        for team in assigned or challenge.get("provisional_halves") or []:
            if challenge.get("focus_side") and team["side"] != challenge["focus_side"]:
                continue
            title = "上半" if team["side"] == "upper" else "下半"
            names = [_safe(row["name"]) for row in team["members"]]
            essential.append(title + ("：" if assigned else "已存部分：") + "、".join(names))
            count += len(names)
            if assigned:
                optional.append(title + "理由：" + _safe(team["reason"]))
        if not assigned:
            essential.append("未形成完整两队，不补人；" + _safe(challenge.get("question")))
        else:
            essential.append("普通12层，两队不重人；未知练度不默认毕业。")
        # Risk notices are part of the answer contract, not optional prose to
        # drop when the host bridge's bounded text channel is tight.
        essential += ["注意：" + _safe(warning["message"]) for warning in challenge.get("warnings", [])
                      if warning.get("message") and (not challenge.get("focus_side")
                                                     or warning.get("side") == challenge["focus_side"])]
    elif guidance.get("kind") == "challenge" and guidance.get("requested"):
        guides = guidance.get("guides") or []
        essential += ["攻略期次：" + _safe(g.get("name")) for g in guides]
        teams = guidance.get("team_candidates") or []
        if teams:
            first = teams[0]
            essential.append(("参考队伍：" if first.get("complete") else "非完整参考成员：")
                             + "、".join(_safe(n) for n in first.get("members") or [] if n))
            essential.append("条件待核对，不证明账号持有或保证通关。")
        elif guides:
            strategy = guides[0].get("strategy") or []
            if strategy:
                essential.append("策略：" + _safe(strategy[0]))
    core = "\n".join(essential)
    text = "\n".join([*essential, *optional])
    try:
        if bridge_preview(text) != text:
            text = core
        compatible = bridge_preview(text) == text
    except Exception:
        compatible = False
    if not compatible:
        try:
            from .cone_message import summarize_cone_fit
            summary = summarize_cone_fit(data, plan=plan, preview=bridge_preview)
            if summary is not None:
                return summary
            from .page_message import summarize_page
            summary = summarize_page(data, plan=plan, preview=bridge_preview)
            if summary is not None:
                text, receipt = summary
                section_receipt(receipt, reply, receipt.get("carried_sections") or [])
                return text, receipt
        except Exception:
            pass  # A failed projection never certifies facts or completion.
    # Ordinary advice can be meaningful without reciting a single number.
    # An unavailable/generic conclusion is NOT a substitute for evidence.
    # Readings arriving without identity remain a real, bounded progress
    # state. Do not turn 'don't recite numbers' into empty_projection which
    # cancels the obligation before later identity can finish it (R1).
    attribution_progress = bool(completion.get("requested")
        and completion.get("panel_subject_state") in {"unconfirmed", "mismatch"}
        and any(f.get("scope") == "attributes" and f.get("value")
                for f in (data.get("screen_facts") or {}).get("facts") or []))
    has_content = count > 0 or attribution_progress or bool(reply.get("enabled") and reply.get("evaluation_available")
                                    and reply.get("conclusion") and not full)
    if not has_content:
        text = "星铁补答未形成可交付内容；不得宣称已经读到或补全。"
    elif not compatible:
        text = ("星铁补答：完整页面文字超出通知通道容量，本条未完整交付；不能把摘要冒充全文，结束等待。"
                if "visible_text" in plan.fields and plan.fields <= {"page", "visible_text"} else
                "星铁补答：完整结果超出通知通道容量，本条未交付词条；不能引用旧装备或宣称已报全，结束等待。")
    receipt = {"mode": "empty_projection" if not has_content else "current_facts" if compatible else "transport_limited_notice",
                  "complete": compatible and has_content, "has_content": has_content,
                  "answer_complete": bool(compatible and has_content and progress.get("complete", True)
                                           and not ambiguous_cone
                                           and (not completion.get("requested") or completion.get("evaluation_complete"))),
                  "required_rows": count, "message_rows": count if compatible else 0,
                  "text_sha256": hashlib.sha256(text.encode()).hexdigest()}
    section_receipt(receipt, reply, carried)
    if "visible_text" in plan.fields and plan.fields <= {"page", "visible_text"}:
        from .page_message import requires_full
        receipt.update(requested_detail="full" if requires_full(plan) else "normal",
                       all_input_rows_delivered=receipt["complete"], source_rows=count,
                       omitted_rows=0 if receipt["complete"] else count,
                       carried_sections=sorted(plan.fields) if receipt["complete"] else [])
    return text, receipt
