"""Bounded D1 reply selection above facts, not an OCR gate or a text cutter.

Keep complete packets for audit/explicit requests. Ordinary assessments select
one direction and up to two rows. The host still generates the final wording.
"""
from __future__ import annotations

import copy
import re

KNOWLEDGE_ACTIONS = frozenset({"priority", "explain", "howto", "equipment_guide", "team_guide", "build_guide",
                               "threshold", "stat_value", "clarify"})

STAT_PROPERTIES = ("能量恢复效率", "治疗量加成", "效果抵抗", "击破特攻", "暴击伤害", "暴击率", "防御力", "攻击力", "生命值", "速度")


def stat_property(query):
    return next((p for p in STAT_PROPERTIES if p in query or p == "防御力" and "防御" in query), "")


def panel_assessment(query):
    """Require both a panel subject and an evaluation/optimization purpose."""
    text = re.sub(r"\s+", "", str(query or ""))
    subjects = [clause for clause in re.split(r"[，,。；;！!？?\n]", text)
                if any(w in clause for w in ("面板", "练度"))
                or "属性" in clause and any(w in clause for w in ("看", "这个", "当前", "现在", "我的"))
                and not any(w in clause for w in ("遗器", "光锥", "这件", "这套", "这把"))]
    return bool(subjects and (
        any(w in text for w in ("怎么样", "合适", "够", "达标", "好吗", "评价"))
        or re.search(r"(?:优先|先|需要|值得|应该|该).{0,8}(?:提升|提高|改善|优化|关注)", text)
        or re.search(r"(?:提升|改善|优化).{0,8}(?:什么|哪|方向)", text)))


def player_stat_reply(query, prior):
    """One exact player number after a scoped stat question, not visual data."""
    prop = prior.get("stat_reply_property", "")
    compact = str(query or "").strip().rstrip("。！!？? ")
    if prop in STAT_PROPERTIES and re.fullmatch(r"\d{1,8}(?:\.\d{1,3})?%?", compact):
        return prop, compact
    return "", ""


def consultation_action(query, *, active):
    """Recognize a bounded continuation's purpose, not a dialogue script."""
    text = re.sub(r"[\s～~。！!？?，,（）()]", "", str(query or ""))
    if active and str(query or "").strip() and not text:
        return "clarify"
    if any(w in text for w in ("任务", "在哪", "地点", "页面", "界面", "剧情", "天气", "工作", "养猫", "别的")):
        return ""
    if any(w in text for w in ("这件", "这套", "这把", "这个光锥", "这个遗器", "眼前", "身上", "当前穿", "现在穿", "画面", "屏幕", "截图")):
        return ""
    advice = any(w in text for w in ("怎么配", "如何配", "怎么搭", "推荐", "带什么", "用什么", "选哪个", "用哪个",
                                    "怎么选", "如何选", "怎样选"))
    if advice and any(w in text for w in ("遗器", "套装", "光锥", "配装")):
        return "equipment_guide"
    if advice and any(w in text for w in ("配队", "队伍", "阵容")):
        return "team_guide"
    if not active or len(text) > 40:
        return ""
    if re.search(r"(?:优先|先|需要|值得|应该|该).{0,8}(?:提升|提高|改善|优化|关注)", text):
        return "priority"
    if stat_property(text) and (any(w in text for w in ("要多少", "堆到", "够不够", "达标", "阈值"))
                               or re.search(r"多少.*(?:合适|够|好)", text)):
        return "threshold"
    if re.fullmatch(r"(?:那|所以|我|她|他|应该|现在|到底|先|要|该)*(?:重点|优先|先关注|先提升).{0,12}", text):
        return "priority"
    if re.fullmatch(r"(?:那|所以|到底)?(?:为什么|为何|什么意思)(?:要|先|看|关注|这个|那个|重点|防御|速度|双暴|回能|循环|呢|呀|啊)*", text):
        return "explain"
    if re.fullmatch(r"(?:那|我|她|他|这个角色|接下来|所以|应该|要|到底)*(?:怎么|如何|怎样)(?:养|培养|提升|练)(?:好|她|他|呢|呀|啊)*", text):
        return "howto"
    return ""


def stated_equipment(query, *, prior, book, catalog):
    """Only explicit positive player statements of known referenced equipment.

    This is a temporary consultation condition, never a visual/account fact.
    No fuzzy matching, arbitrary inventory scanner or new database coverage.
    """
    if len(prior.get("character_ids") or []) != 1:
        return ()
    def name_key(text):
        # Only player-typed punctuation/spacing; never OCR values, aliases or
        # approximate matching. Return the catalog's unchanged canonical ID.
        return re.sub(r"[\s，,。？！!?「」『』\"']", "", str(text))

    clauses = [c for c in re.split(r"[。；;！!？?\n]", str(query or ""))
               if re.search(r"我(?:带|用|穿|有)|带的是|用的是|光锥是|套装是", c)
               and not re.search(r"如果|假设|要是|能不能|有没有|没(?:有)?|不带|不用|不是", c)]
    role = next((r for r in book.characters if r["identity_id"] == prior["character_ids"][0]), {})
    ids = [*role.get("relic_ids", []), *role.get("ornament_ids", []), role.get("signature_id"),
           *(r["id"] for r in role.get("alternative_cones") or [])]
    return tuple(entity_id for entity_id in dict.fromkeys(ids) if entity_id and
                 (record := catalog.get(entity_id)) and record.get("sources") and record.get("name")
                 and (any(name_key(record["name"]) in name_key(c) for c in clauses)
                      or prior.get("consultation_action") == "equipment_guide"
                      and name_key(query) == name_key(record["name"])))


def consultation_fields(query, fields, focus):
    """Keep actual mixed demands, not optional guesses from broad tool hints."""
    def requested(words):
        topic = "(?:" + "|".join(re.escape(w) for w in words) + ")"
        for clause in re.split(r"[，,。；;！!？?\n]", query):
            if not any(w in clause for w in words):
                continue
            if re.search(r"(?:不用|无需|不要|别|不必|先不).{0,6}(?:看|查|读|管|考虑).{0,2}" + topic, clause):
                continue
            return True
        return False

    fields = set(fields)
    if focus == "equipment":
        # A deictic "这件...怎么样" gets a generic characters fallback in
        # the low-level classifier. Item fit must not wait for a roster the
        # player never asked about. Real mixed identity questions stay intact.
        if not requested(("角色", "人物", "谁", "队员", "头像")):
            fields -= {"characters", "controlled_character"}
    if focus in {"attributes", "visible_text"} and not requested(("任务", "追踪", "导航", "任务目标")):
        fields.discard("quest")
    if focus == "attributes":
        if not requested(("遗器", "光锥", "词条", "装备", "套装")):
            fields.discard("equipment")
    return frozenset(fields | {focus})


def detail_preference(query):
    query = str(query or "")
    # A soft brevity preference cannot override an explicit request to list
    # every stat. Negated detail clauses do not create a full-list demand.
    for clause in re.split(r"[，,。；;！!？?\n]", query):
        if re.search(r"(?:不用|不要|不必|不需要|无需)(?:详细|完整|逐项|逐条|逐字|全部|列|读|念|报)", clause):
            continue
        if (re.search(r"完整|全部|所有|逐项|逐条|逐字|每条|别漏|详细|列出", clause)
                or re.search(r"(?:主副词条|主词条|副词条|词条|属性).*(?:是什么|有哪些|分别|多少|列|报|念|读)", clause)
                or re.search(r"(?:列|报|念|读|告诉我).*(?:主副词条|主词条|副词条|词条|属性)", clause)):
            return "full"
    return "brief" if re.search(r"不用详细|不必详细|不要展开|不用展开|别展开|不(?:用|要)完整|简单说说|简短|只(?:说|讲|看).*重点", query) else ""


def explicit_detail(query):
    return detail_preference(query) == "full"


def condition_reply(query, prior):
    """A short usage condition inside the same scoped consultation.

    No raw chat storage, no catch-all intent classifier, no implied frame reuse.
    Caller has already checked the existing discussion TTL and subject.
    """
    if not (len(prior.get("character_ids") or []) == 1
            and (prior.get("question_offered") or prior.get("kind") == "build"
                 and prior.get("consultation_action") in KNOWLEDGE_ACTIONS | {"assess"})):
        return ""
    short = re.sub(r"[\s～~。！!？?啊呀呢哦嘛，,]", "", str(query or ""))
    return {
        "循环": "rotation", "回能": "rotation", "回能不够": "rotation",
        "大招接不上": "rotation", "终结技接不上": "rotation", "循环不顺": "rotation",
        "输出": "damage", "伤害": "damage", "伤害不够": "damage", "输出不够": "damage",
        "生存": "sustain", "治疗": "sustain", "奶不住": "sustain", "容易死": "sustain",
    }.get(short, "")


TOPIC_PROPERTIES = {
    "rotation": ("能量恢复效率", "速度"),
    "damage": ("暴击率", "暴击伤害", "攻击力", "攻击力百分比"),
    "sustain": ("防御力", "防御力百分比", "治疗量加成", "生命值", "效果抵抗"),
}


def build_reply_plan(plan, assessment, role, *, query="", guides=(), teams=(), stat_advice=None):
    """Select grounded content; never mutate or fill assessment/card values."""
    knowledge = plan.consultation_action in KNOWLEDGE_ACTIONS
    if plan.kind != "build" or not (plan.request_focus or knowledge):
        return {"enabled": False}
    full = plan.reply_detail == "full"
    topic = plan.consultation_topic or "general"
    evaluation = assessment.get("evaluation") or {}
    available = bool(assessment.get("evaluation_available"))
    card = assessment.get("equipment_answer") or {}
    rows = []
    kinds = plan.equipment_kinds or {"main_stat", "sub_stat"}
    if plan.request_focus == "equipment" and card.get("state") not in {"conflict", "unconfirmed"}:
        if "main_stat" in kinds and card.get("main_stat"):
            main = card["main_stat"]
            review = evaluation.get("main_stat") or {}
            if (review.get("label"), review.get("value")) != (main["label"], main["value"]):
                review = {}
            rows.append({**review, **main, "kind": "main_stat"})
        if "sub_stat" in kinds:
            evaluated = evaluation.get("sub_stats") or []
            rows.extend({**next((r for r in evaluated if (r["label"], r["value"]) == (row["label"], row["value"])), row),
                         "kind": "sub_stat"} for row in card.get("sub_stats") or [])
    elif plan.request_focus == "attributes":
        from .equipment import _rows
        # Factual extraction does not require a reviewed build for this role.
        # Use the same numeric parser as the assessor, without inventing an
        # evaluation for unsupported roles or forwarding conflicting totals.
        observed = _rows([f for f in assessment.get("facts") or [] if f.get("scope") == "attributes"], "stat")
        if len({r["label"] for r in observed}) == len(observed):
            evaluated = evaluation.get("rows") or []
            rows = [{**next((r for r in evaluated if (r["label"], r["value"]) == (row["label"], row["value"])), row),
                     "kind": "stat"} for row in observed]

    notes = (role or {}).get("stat_notes") or {}
    priorities = TOPIC_PROPERTIES.get(topic) or tuple(notes)
    if full:
        selected = rows
    elif evaluation.get("kind") == "equipment":
        # A mismatched main outranks optional subtleties, without declaring a
        # replacement or deleting the other confirmed rows in the full card.
        selected = [] if evaluation.get("fixed_main_stat") else [r for r in rows if r["kind"] == "main_stat"][:1]
        selected += [r for r in rows if r["kind"] == "sub_stat" and r.get("verdict") == "priority"][:2 - len(selected)]
        if not selected:
            selected = [r for r in rows if r["kind"] == "sub_stat"][:2] or rows[:1]
    else:
        selected = sorted((r for r in rows if r["property"] in priorities),
                          key=lambda r: priorities.index(r["property"]))[:2]
    # Keep exact values for audit/full requests. Ordinary fit advice needs
    # property directions, not a second recital of the panel. A reviewed
    # threshold/stat-value question still needs its actual numbers.
    speak_values = full or plan.consultation_action in {"threshold", "stat_value"}
    selected = [{**r, "speak_value": speak_values} for r in selected]
    complete = bool(card.get("complete"))
    if plan.request_focus == "equipment":
        if kinds == {"main_stat"}:
            complete = bool(card.get("main_stat") and not card.get("pending"))
        elif kinds == {"sub_stat"}:
            complete = bool(card.get("sub_stats_complete"))
    else:
        # Panel coverage/threshold is not proven by this D1 assessor.
        complete = bool(evaluation.get("complete"))
    next_step, direction = "", ""
    if selected and available:
        direction = selected[0]["property"]
        next_step = selected[0].get("check", "")
        if selected[0].get("verdict") == "outside_reviewed_candidates":
            next_step = next((s["action"] for s in evaluation.get("next_steps") or []
                              if s["kind"] == "main_stat_review"), next_step)
    elif (plan.condition_reply or plan.consultation_action in {"priority", "explain", "howto"}) and role:
        # Known source advice is usable without a new screenshot, but never
        # retrieve old panel numbers or call this a new visual evaluation.
        direction = plan.focus_property if plan.focus_property in notes else next((p for p in priorities if p in notes), "")
        next_step = notes.get(direction, {}).get("check", "")
        if plan.consultation_action in {"priority", "howto"}:
            next_step = (role.get("principles") or [next_step])[0]
    elif available and evaluation.get("kind") == "light_cone":
        direction = "光锥适用条件"
        next_step = (evaluation.get("conditions") or [""])[0]
    if direction:
        verdict = selected[0].get("verdict") if selected else None
        conclusion = (f"{direction}主词条不在该部位的已复核候选内，先比较主词条方向。"
                      if verdict == "outside_reviewed_candidates" else
                      f"{direction}主词条符合该部位的候选方向。"
                      if verdict == "matches_reviewed_candidate" else
                      f"先检查{direction}相关配置。")
    else:
        conclusion = "目前只能说明已确认的信息，尚无依据判断适配或养成档次。"
    fit_summary = copy.deepcopy(evaluation.get("fit_summary") or {})
    if evaluation.get("kind") == "equipment" and available:
        fixed = evaluation.get("fixed_main_stat")
        observed = fit_summary.get("observed_substat_count", 0)
        priorities_count = fit_summary.get("priority_substat_count", 0)
        main = evaluation.get("main_stat") or {}
        useful = list(dict.fromkeys(r["property"] for r in evaluation.get("sub_stats") or []
                                   if r.get("verdict") == "priority"))
        # Translate the EXISTING reviewed verdict into a bounded item decision,
        # not a new score/replacement engine. Keep comparison caveats internal
        # unless a comparison/next step was actually requested.
        if main.get("verdict") == "outside_reviewed_candidates" and not fixed:
            conclusion = f"这件不优先作为该角色的候选，{main['property']}主词条不在目前的推荐方向里。"
        elif not main:
            conclusion = "主词条还没确认，暂不能判断整件。"
            if useful:
                conclusion += "已读副词条中的" + "、".join(useful[:2]) + "用得上。"
        elif not observed:
            conclusion = ("该部位主词条固定，不作为单件优劣依据。" if fixed else "主词条方向对路。")
            conclusion += "副词条还没看清，暂不能判断整件。"
        elif priorities_count:
            conclusion = "这件可以先留作候选，"
            conclusion += "主词条固定，重点看副词条；" if fixed else f"{main['property']}主词条对路；"
            conclusion += "副词条中的" + "、".join(useful[:2]) + "也用得上。"
        else:
            conclusion = ("主词条固定，不据此评优劣；" if fixed else "主词条方向对路；")
            conclusion += "已读副词条暂未见优先方向，可先作为过渡候选。"
        if "next_step" in plan.answer_parts:
            if fixed or main.get("verdict") == "matches_reviewed_candidate":
                next_step = "拿同部位另一件比较" + "、".join(useful[:2] or fit_summary.get("priority_properties") or []) + "，再决定是否替换。"
        else:
            next_step = ""
    if plan.consultation_action in {"threshold", "stat_value"}:
        direction = plan.stat_property or plan.focus_property
        conclusion = f"当前服务尚未核验{direction}在此队伍条件下的数值阈值；不能编达标线、越高越好或仅凭单值判毕业。"
        next_step = notes.get(direction, {}).get("check", "没有对应已核验条件，不能给数值目标或适配评分。")
        if (stat_advice or {}).get("available"):
            activation, cap = stat_advice["activation_value"], stat_advice["cap_value"]
            conclusion = f"先确认对应行迹已解锁；{activation}防御是这条能力的触发点，{cap}是这条能力的换算上限，不是毕业线，不必为追满它直接牺牲行动和回能。"
            comparison = stat_advice.get("comparison")
            if comparison:
                relation = {"below_activation": "低于触发点，先核对防御主词条和行迹",
                            "above_activation_below_cap": "已超过触发点，但不表示毕业，不必直接追满上限",
                            "at_or_above_cap": "已达到这条换算的上限，但不表示整套毕业"}[comparison["relation"]]
                conclusion = f"你报的{comparison['value']}若是该行迹生效时的防御力且已解锁行迹，{relation}。"
    elif plan.consultation_action == "clarify":
        conclusion = "玩家尚未说明疑问点，不猜测读得太快或继续评价旧装备。"
    elif available and evaluation.get("kind") == "light_cone":
        conclusion = evaluation.get("reason") or conclusion
    elif available and evaluation.get("kind") == "attributes" and next_step:
        # The check is already the actionable answer. Avoid making the model
        # translate a second, generic paragraph about 'usage conditions'.
        conclusion = "先检查" + direction + "相关配置。"
    question = ""
    if not plan.condition_reply:
        if not assessment.get("subject_ids"):
            question = {"purpose": "identify_subject", "ask_for": "本次要讨论的角色",
                        "reason": "没有唯一讨论对象，不能把通用配装冒充此角色建议。",
                        "player_text_is_sufficient": True}
    if plan.consultation_action == "clarify":
        question = {"purpose": "clarify_question", "ask_for": "玩家是在质疑读数、对象还是建议依据",
                    "reason": "不能凭一个问号编造原因或继续沿用未重新确认的对象。", "player_text_is_sufficient": True}
    options = []
    topics = plan.guide_topics
    if plan.consultation_action in {"equipment_guide", "build_guide"}:
        for guide in guides:
            cones = [c for c in guide.get("light_cone_candidates") or [] if c.get("available")]
            if plan.stated_equipment_ids:
                cones = [c for c in cones if c["id"] in plan.stated_equipment_ids]
            options.append({"character": guide["name"],
                "main_stats": copy.deepcopy(guide.get("main_stats") or {}) if "relic" in topics else {},
                "equipment_candidates": copy.deepcopy([e for e in guide.get("equipment_candidates") or [] if e.get("available")]) if "relic" in topics else [],
                "light_cone_candidates": copy.deepcopy(cones) if "cone" in topics else [],
                "sources": copy.deepcopy(guide.get("sources") or [])})
    elif plan.consultation_action == "team_guide":
        options = copy.deepcopy(list(teams))
    if knowledge and options:
        conclusion = "按已明确角色和玩家条件讨论有来源候选；这些候选不证明当前穿戴或账号拥有。"
        if "next_step" in plan.answer_parts and not next_step:
            next_step = next((p for g in guides for p in g.get("principles") or []), "")
    response_state = "answer" if available or knowledge else "partial" if rows else "unconfirmed"
    if not knowledge and not available:
        # No evidence is not permission to fill the wait with generic advice.
        conclusion = "还没看清这件装备。" if plan.request_focus == "equipment" else "还没看清当前面板。"
        next_step = ""
        if assessment.get("subject_ids"):
            question = ""
    goal = plan.consultation_action
    turn = {"goal": goal, "after_answer": "yield_to_player", "question_is_last": bool(question),
            "finish_when": "requested_answer_parts_delivered_or_necessary_question_asked",
            "optional_information_creates_no_followup": True, "background_observation": "silent_unchanged"}
    return {
        "enabled": True, "mode": "full" if full else "brief", "topic": topic,
        "guide_topics": sorted(topics), "required_sections": sorted(plan.answer_parts),
        "response_state": response_state,
        "goal": goal, "turn_contract": turn, "length_is_hard_limit": False,
        "discussion_options": options,
        "conclusion": conclusion, "primary_direction": direction,
        "evidence_rows": copy.deepcopy(selected), "next_step": next_step,
        "value_policy": "all_requested_exact" if full else "only_when_requested_or_decisive",
        # Minimal comparison basis in the existing reply plan, not a second
        # fact store. Metadata/serial/proof geometry deliberately excluded.
        "answer_basis": {"subjects": list(assessment.get("subject_ids") or []),
                         "panel_subject_state": assessment.get("panel_subject_state"),
                         "rows": [[r["kind"], r["label"], r["value"]] for r in rows]},
        # Completeness belongs to the original card, not this brief selection.
        "evidence_rows_are_summary": not full,
        "equipment_coverage": {
            "state": card.get("state", "unconfirmed"), "complete": bool(card.get("complete")),
            "main_stat_present": bool(card.get("main_stat")),
            "confirmed_substats": len(card.get("sub_stats") or []),
            "expected_substats": card.get("expected_sub_stat_count"),
            "omitted_from_summary_is_not_unread": True,
        } if plan.request_focus == "equipment" else None,
        "advice_source": "reviewed_mechanic_not_account_target" if (stat_advice or {}).get("available") else "assessment.evaluation" if available else
                        "reviewed_stat_note_not_current_reading" if next_step else "unavailable",
        "evaluation_available": available, "rows_complete": complete,
        "current_object": copy.deepcopy(assessment.get("current_object")),
        "stat_advice": copy.deepcopy(stat_advice or {"available": False}),
        "fixed_main_stat": bool(evaluation.get("fixed_main_stat")), "fit_summary": fit_summary,
        "stated_stat": {"property": plan.stat_property, "value": plan.player_stat_value,
                        "origin": "player_statement", "visually_confirmed": False}
                       if plan.player_stat_value else None,
        "must_include_all_confirmed_requested_rows": full,
        "missing_rows_must_be_disclosed": full and not knowledge and not complete,
        "missing_is_not_absent": True, "numerical_grade_available": False,
        "max_sentences": None if full else 3, "max_questions": int(bool(question)), "question": question,
        "instruction": (
            "本轮要求完整或逐项：逐条保留evidence_rows中所有已确认的label/value及百分号，不以句数截断；"
            "required_sections中的评价、下一步等也是必答内容，完整不等于只报词条；"
            "未读全必须说明缺项，不能说成全部读全，不用推荐或上一件补数。详细分析仍只用有来源结论；"
            "若评价对象尚未明确，最多用question澄清一次，不影响先列齐已确认词条。"
            if full else
            "没看清只简短说明，followup.pending为真才可说再看一眼，不猜加载、不讲通用攻略。"
            "看清后直接用conclusion回答玩家该不该留/用或先检查哪里，不先宣布识别成功或复述数据。"
            "next_step非空才补必要行动；没有替代件不编提升幅度，不把留作候选说成应该替换。"
            "数字只在玩家问数值或决定本题结论时说；未问数值不逐项报数、扩展攻略或追加套话。"
            "question确实影响下一步才问，放最后；答到位就交还话轮。"
            "evidence_rows只是重点选择，未列不等于未读；读取完整性看equipment_coverage，不能按摘要行数声称缺项。"
        ) + "这不是固定话术：goal决定内容；保留next_step的完整条件与先后关系，不能把‘如果已是’改成‘如果不是’。"
            "固定主词条不评优劣；不把属性方向、触发点或机制上限说成毕业线。"
            "current_object未确认不借用上一件；stated_stat是玩家陈述，不是画面。"
            "只用本次事实与有来源结论，不把缺读当缺少，不编机制；不为可选信息承诺补答。",
    }


def model_guidance(guidance):
    """Expose the existing selection, not a competing full build article.

    No new selection, IO, facts or inference. Original assessments/cards stay
    intact for numerical/source checks; full guides remain in the catalog and
    internal build result. D2 and unplanned services pass through unchanged.
    """
    reply = guidance.get("reply_plan") or {}
    if not reply.get("enabled") or guidance.get("kind") != "build":
        return guidance
    result = {k: v for k, v in guidance.items() if k not in {
        "guides", "team_candidates", "instruction"}}
    # These are internal delta bookkeeping, not another set of spoken facts.
    result["reply_plan"] = {k: v for k, v in reply.items() if k != "answer_basis"}
    assessment = guidance.get("assessment") or {}
    evaluation = assessment.get("evaluation") or {}
    if reply.get("mode") == "brief" and reply.get("goal") == "assess" and evaluation.get("kind") == "equipment":
        # The selected plan already owns advice. Do not offer a competing
        # generic strategy/paragraph for every row in the model view. Keep all
        # exact facts, verdicts, provenance and the original internal assessment
        # intact. Full-list requests and cone timing conditions are untouched.
        view = {k: v for k, v in evaluation.items() if k not in {"summary", "conditions", "next_steps"}}
        for key in ("main_stat", "sub_stats"):
            rows = view.get(key)
            if isinstance(rows, dict):
                view[key] = {k: v for k, v in rows.items() if k not in {"meaning", "check"}}
            elif isinstance(rows, list):
                view[key] = [{k: v for k, v in row.items() if k not in {"meaning", "check"}} for row in rows]
        result["assessment"] = {**assessment, "evaluation": view}
    return result


def for_followup(reply, *, previous=None):
    """Project useful progress of the original answer, not every fact change.

    Service completion/identity binding stay in request_completion. This
    existing content selector alone decides whether the new plan adds a
    conclusion, condition, required row, correction or final attribution.
    """
    result = copy.deepcopy(reply)
    if result.get("enabled"):
        changed = previous is None or not previous.get("enabled")
        if previous and previous.get("enabled"):
            changed = bool(result.get("evaluation_available") and (
                not previous.get("evaluation_available") or any(result.get(k) != previous.get(k)
                    for k in ("conclusion", "next_step"))))
            changed |= result.get("stated_stat") != previous.get("stated_stat")
            old_basis, new_basis = previous.get("answer_basis") or {}, result.get("answer_basis") or {}
            changed |= bool(new_basis.get("subjects") and old_basis.get("subjects") != new_basis["subjects"])
            changed |= (new_basis.get("panel_subject_state") == "mismatch"
                        and old_basis.get("panel_subject_state") != "mismatch")
            if result.get("mode") == "full":
                # New requested rows/corrections count; mere reordering or a
                # temporary empty read is not useful progress. No rows from
                # the old packet are merged into the current answer.
                changed |= bool({tuple(r) for r in new_basis.get("rows") or []}
                                - {tuple(r) for r in old_basis.get("rows") or []})
                changed |= bool(result.get("rows_complete") and not previous.get("rows_complete"))
            else:
                # The prior selected exact answer is the correction boundary,
                # not every value in the internal facts. Unspoken optional
                # changes are silent unless the actual conclusion changes.
                def values(basis):
                    grouped = {}
                    for kind, label, value in basis.get("rows") or []:
                        grouped.setdefault((kind, label), set()).add(value)
                    return grouped
                old = values({"rows": [[r["kind"], r["label"], r["value"]]
                              for r in previous.get("evidence_rows") or [] if r.get("speak_value")]})
                new = values(new_basis)
                corrections = {key for key in old.keys() & new.keys() if old[key] != new[key]}
                changed |= bool(corrections)
                if corrections:
                    # A correction is useful only if the receiving model can
                    # see the corrected value. Ordinary advice otherwise keeps
                    # numbers silent. These rows are from this selected packet,
                    # never filled from a previous item or another frame.
                    corrected = [{"kind": kind, "label": label, "value": value, "speak_value": True}
                                 for kind, label, value in new_basis.get("rows") or []
                                 if (kind, label) in corrections]
                    result["evidence_rows"] = corrected
                    result["value_policy"] = "correct_previous_reading"
        result["answer_increment"] = bool(changed)
        result.update(max_questions=0, question="")
        result["turn_contract"] = {**result.get("turn_contract", {}), "question_is_last": False,
                                   "finish_when": "original_missing_requested_evidence_delivered"}
        result["instruction"] += "本条为补答，只完成原问题，不开启新追问。"
    return result
