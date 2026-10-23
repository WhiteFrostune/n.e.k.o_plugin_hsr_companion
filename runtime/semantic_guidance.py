"""Execute declared consultation purposes, not a second raw-text classifier.

Entity/condition resolution remains exact and bounded. A named discussion
subject is never a visual identity, owned role or permission to reuse an item.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone

from .consultation import (
    KNOWLEDGE_ACTIONS,
    condition_reply,
    detail_preference,
    player_stat_reply,
    stat_property,
    stated_equipment,
)


def plan_semantic_guidance(query, base, *, catalog, book, previous=None, now_monotonic=None,
                           now=None, discussion_character="", declaration=None):
    from .challenge import challenge_scope
    from .guidance import CONTEXT_TTL, MODES, GuidancePlan, _names, signature_statement, window

    action = base.intent
    if action in {"read_screen", "explain_page"}:
        return GuidancePlan()
    prior = previous or {}
    clock = time.monotonic() if now_monotonic is None else now_monotonic
    valid = 0 <= clock - prior.get("at", -1e9) <= CONTEXT_TTL
    continuation = action == "continue_consultation"
    saved_cone = bool(declaration and declaration["scope"] == "saved_cone" or
                      declaration is None and continuation and valid and prior.get("evidence_source") == "saved_profile")
    needs_source = action == "assess_cone" and declaration is None and prior.get("kind") == "challenge"
    if continuation and not saved_cone and (not valid or prior.get("kind") not in {"build", "challenge"}):
        return GuidancePlan(kind="build", consultation_action="clarify", execution_fields=frozenset(),
                            execution_intent=action)

    # These are facts stated by the player, not a new interpretation of task
    # purpose. No generic buff/team inference or profile/vision writes.
    names = _names(query, catalog)
    excluded = [r["name"] for r in names if re.search(
        r"(?:没(?:有)?|缺|不带|不用|不要)\s*" + re.escape(r["name"]), query)]
    ownership = re.search(r"(?:我只有|我有|我的角色有|我已经练了)([^。？！!\n]+)", query)
    if ownership and not _names(re.split(r"[，,]", ownership[1], maxsplit=1)[0], catalog):
        ownership = None
    owned = tuple(r["name"] for r in _names(ownership[1], catalog) if r["name"] not in excluded) if ownership else ()
    proposed = tuple(r["name"] for r in names if re.search(
        r"(?:换成|换为|改用|替换成)\s*" + re.escape(r["name"]), query))
    targets = tuple(r["id"] for r in names if r["name"] not in {*excluded, *owned, *proposed})
    origin = "player_text" if targets else "unresolved"
    hint = catalog.resolve_character(discussion_character) if discussion_character else None
    subject_state, subject_reason = "legacy", ""
    if declaration is not None:
        subject_state = declaration["subject_state"]
        if subject_state == "specified":
            subject = catalog.resolve_character(declaration["character"])
            if subject and targets and subject["id"] not in targets:
                raise ValueError("subject_conflict: 指定角色与本轮明确角色冲突，请澄清")
            targets = (subject["id"],) if subject else ()
            origin = "declared_subject" if targets else "unresolved"
            subject_reason = "" if targets else "invalid_specified_character"
        elif subject_state == "inherit":
            inherited = tuple(prior.get("character_ids") or ()) if valid and prior.get("kind") == "build" else ()
            if targets and targets != inherited:
                raise ValueError("subject_conflict: 本轮明确新角色，不能声明沿用旧角色")
            targets, origin = inherited, "discussion_context" if inherited else "unresolved"
            subject_reason = "" if inherited else "no_valid_discussion_subject"
        else:
            targets, origin, subject_reason = (), "unresolved", "declared_unresolved"
    elif not targets and hint:
        targets, origin = (hint["id"],), "conversation_hint"
    if declaration is None and not targets and action in {"equipment_guide", "build_guide"} and any(w in query for w in ("本期", "当前卡池", "这期卡池")):
        targets = tuple(r["identity_id"] for r in book.characters if window(r, now or datetime.now(timezone.utc)) == "active"
                        and ("新角色" not in query or r["release"] == "new")
                        and ("复刻" not in query or r["release"] == "rerun"))
        if targets:
            origin = "banner_calendar"
    if (declaration is None and not targets and valid and prior.get("kind") == "build"
            and action not in {"assess_relic", "assess_cone", "assess_panel"}):
        targets = tuple(prior.get("character_ids") or ())
        origin = "discussion_context" if targets else "unresolved"
    if needs_source:
        # Preserve the role resolved above, NOT the expired team/equipment.
        # No second raw-question classifier guesses which source was intended.
        return GuidancePlan(kind="build", character_ids=targets, target_origin=origin,
                            consultation_action="clarify", execution_fields=frozenset(), execution_intent=action,
                            source_state="needs_source", evidence_source="unresolved")
    same = valid and bool(targets) and targets == tuple(prior.get("character_ids") or ())
    modes = [key for key, aliases in MODES.items() if any(alias in query for alias in aliases)]
    periods = [p for p in book.periods if p["name"] in query]
    nodes = [(p, node) for p in book.periods for node in p.get("nodes", [])
             if any(alias in query for alias in node["aliases"])]
    node = nodes[0][1]["id"] if len({n["id"] for _, n in nodes}) == 1 else ""
    all_modes = bool(re.search(r"(?:各|所有|全部).*(?:高难|副本)|本期高难", query))
    challenge = (not saved_cone and (action == "challenge_guide" or continuation and prior.get("kind") == "challenge" or action == "team_guide"
                 and (bool(modes or periods or nodes or all_modes or base.fields) or not targets)))
    kind = "challenge" if challenge else "build"
    mode = modes[0] if len(modes) == 1 else periods[0]["mode"] if periods else ""
    period_ids = tuple(p["id"] for p in periods)
    if not mode and nodes and len({p["mode"] for p, _ in nodes}) == 1:
        mode = nodes[0][0]["mode"]
    floor, difficulty = challenge_scope(query)
    same_stage = challenge and valid and prior.get("kind") == "challenge" and not (modes or periods or nodes or all_modes)
    if same_stage:
        mode, period_ids = prior.get("mode", ""), tuple(prior.get("period_ids", ()))
        floor, difficulty = floor or prior.get("challenge_floor", 0), difficulty or prior.get("challenge_difficulty", "")
        node = node or prior.get("node", "")
        all_modes = all_modes or bool(prior.get("all_modes"))
    same = same_stage or same and not challenge
    signature, _ = signature_statement(query)
    no_signature = signature is False or same and signature is not True and bool(prior.get("no_signature"))
    if same:
        excluded = [name for name in dict.fromkeys([*prior.get("excluded_names", ()), *excluded]) if name not in owned]
        if not owned or "我只有" not in query:
            owned = tuple(dict.fromkeys([*prior.get("supplied_owned", ()), *owned]))
    focus, kinds = "", frozenset()
    purpose = {"equipment_guide": "equipment_guide", "build_guide": "build_guide", "team_guide": "team_guide",
               "build_priority": "priority", "build_explain": "explain", "build_howto": "howto",
               "stat_threshold": "threshold", "stat_value": "stat_value"}.get(action, "assess")
    if continuation:
        focus = prior.get("request_focus", "")
        kinds = frozenset(prior.get("equipment_kinds") or ())
        purpose = prior.get("consultation_action", "assess")
    elif action in {"assess_relic", "assess_cone", "equipment_guide"}:
        focus = "equipment"
        kinds = frozenset({"main_stat", "sub_stat"}) if action == "assess_relic" else frozenset()
    elif action == "assess_panel":
        focus = "attributes"
    prop, player_value = player_stat_reply(query, prior) if same and (continuation or action == "stat_value") else ("", "")
    if player_value:
        purpose = "stat_value"
    elif purpose == "stat_value":
        purpose = "clarify"  # No unbound number is promoted into a stat fact.
    elif purpose == "threshold":
        prop = stat_property(query)
    if not focus and same and purpose in KNOWLEDGE_ACTIONS:
        focus = prior.get("request_focus", "")
    fields = base.fields
    if continuation:
        fields = frozenset({focus}) if focus and purpose not in KNOWLEDGE_ACTIONS else frozenset()
    if saved_cone:
        focus, kinds, purpose, fields = "equipment", frozenset(), "equipment_guide", frozenset()
    if challenge:
        # Stage identifiers come from current visible text only when absent
        # from the user's explicit stage/valid discussion. No enemy-page scan.
        fields = frozenset({"visible_text"}) if not (all_modes or mode and (mode != "moc" or floor and difficulty)) else frozenset()
        if base.fields:
            fields |= base.fields
    topic = condition_reply(query, prior) if same else ""
    declared = stated_equipment(query, prior={"character_ids": list(targets), **({"consultation_action":
                                  prior.get("consultation_action")} if same else {})}, book=book, catalog=catalog)
    stated_ids = declared or (tuple(prior.get("stated_equipment_ids") or ()) if same else ())
    detail = detail_preference(query)
    detail = detail or base.response_detail or (
        prior.get("reply_detail", "brief") if continuation else "brief")
    return GuidancePlan(kind=kind, character_ids=targets if not challenge else (), mode=mode, period_ids=period_ids,
        requires_pixels=bool(fields), no_signature=bool(no_signature), excluded_names=tuple(excluded), supplied_owned=owned,
        node=node, all_modes=all_modes,
        target_origin=origin, proposed_teammates=proposed, request_focus=focus, equipment_kinds=kinds, reply_detail=detail,
        consultation_action=purpose, consultation_topic=topic or (prior.get("consultation_topic", "general") if same else "general"),
        condition_reply=bool(topic), focus_property=prior.get("focus_property", "") if same else "",
        stat_property=prop, player_stat_value=player_value,
        stated_equipment_ids=stated_ids, stated_equipment_origin="player_statement" if declared else
        "discussion_context_player_statement" if stated_ids else "", challenge_floor=floor,
        challenge_difficulty=difficulty, challenge_side="upper" if "上半" in query and "下半" not in query else
        "lower" if "下半" in query and "上半" not in query else "", execution_fields=fields,
        subject_state=subject_state, subject_reason=subject_reason,
        assessment_scope=declaration["scope"] if declaration else "", allow_visual_subject=declaration is None,
        evidence_source="saved_profile" if saved_cone else "current_screen" if action in {
            "assess_cone", "assess_relic", "assess_panel"} else "",
        source_state="restored_named_archive" if saved_cone and not valid and targets else "declared" if declaration else "",
        execution_intent=prior.get("execution_intent", action) if continuation and not saved_cone else action)
