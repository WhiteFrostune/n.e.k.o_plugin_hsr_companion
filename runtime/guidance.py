"""Scoped strategy service; knowledge, user conditions and pixels stay separate.

No OCR, networking, input simulation or profile writes. Reuses the existing
catalog and QueryPlan; a strategy answer is never a current-screen assertion.
"""

from __future__ import annotations

import copy
import json
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from urllib.parse import urlparse

from .consultation import (
    KNOWLEDGE_ACTIONS,
    build_reply_plan,
    condition_reply,
    consultation_action,
    consultation_fields,
    detail_preference,
    panel_assessment,
    player_stat_reply,
    stat_property,
    stated_equipment,
)
from .data_freshness import guidance_freshness
from .query_plan import SEMANTIC_INTENTS, QueryPlan, asks_relic_rows_only, build_query_plan

CONTEXT_TTL = 600.0
MODES = {
    "moc": ("混沌回忆", "混沌", "忘却之庭"),
    "pf": ("虚构叙事", "虚构"),
    "as": ("末日幻影", "末日"),
    "aa": ("异相仲裁", "异象仲裁", "仲裁"),
}
PIXEL_WORDS = ("截图", "屏幕", "画面", "这件", "这套", "这把", "这几个", "这张", "身上", "我现在穿", "我当前穿")


def load_guidance_catalog(path):
    """An optional strategy resource must not break unrelated capabilities."""
    try:
        return GuidanceCatalog(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return GuidanceCatalog({"schema_version": 1, "server": "cn", "revision": "unavailable",
                                "game_version": "unknown", "checked_at": None,
                                "available": False, "sources": {}, "characters": [], "teams": [], "periods": []})


def stamp(value):
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("guidance dates require timezone")
    return result


def window(record, now):
    if record.get("release") == "reference" and "starts_at" not in record:
        return "not_applicable"  # evergreen coverage does not invent a banner
    if now < stamp(record["starts_at"]):
        return "upcoming"
    return "ended" if now >= stamp(record["ends_at"]) else "active"


class GuidanceCatalog:
    """Small reviewed strategy records, not a replacement game database."""

    def __init__(self, payload):
        if payload.get("schema_version") != 1 or payload.get("server") != "cn" or not payload.get("revision"):
            raise ValueError("unsupported guidance schema/server/revision")
        self.data = copy.deepcopy(payload)
        self.sources = self.data.get("sources", {})
        self.characters = self.data.get("characters", [])
        self.periods = self.data.get("periods", [])
        team_rows = self.data.get("teams", [])
        self.teams = {t["id"]: t for t in team_rows}
        if len(self.teams) != len(team_rows):
            raise ValueError("duplicate guidance team")
        seen = set()
        for source in self.sources.values():
            url = urlparse(source.get("url", ""))
            if url.scheme != "https" or not url.hostname or not source.get("license") or not source.get("usage"):
                raise ValueError("guidance source missing provenance")
        for record in [*self.characters, *self.periods, *self.teams.values()]:
            key = record.get("id")
            if not key or key in seen:
                raise ValueError("duplicate/missing guidance id")
            seen.add(key)
            refs = record.get("source_refs", [])
            if not refs or any(ref not in self.sources for ref in refs):
                raise ValueError("guidance record missing source")
            if "starts_at" in record and stamp(record["starts_at"]) >= stamp(record["ends_at"]):
                raise ValueError("invalid guidance period")
            if any(ref not in self.teams for ref in record.get("team_refs", [])):
                raise ValueError("unknown guidance team")
        for role in self.characters:
            main_stats, substats = role.get("main_stats"), role.get("substats")
            if (not isinstance(main_stats, dict) or not main_stats or
                    any(not isinstance(slot, str) or not isinstance(choices, list) or not choices
                        or any(not isinstance(c, str) or not c for c in choices) for slot, choices in main_stats.items())
                    or not isinstance(substats, list) or not substats or any(not isinstance(s, str) or not s for s in substats)):
                raise ValueError("guidance stat rules missing or invalid")
            if not role.get("form_id", "").startswith("character_form.") or not role.get("identity_id", "").startswith("character."):
                raise ValueError("guidance character requires explicit form")
            ids = [*role.get("relic_ids", []), *role.get("ornament_ids", [])]
            if any(not value.startswith("relic_set.") for value in ids):
                raise ValueError("invalid guidance relic id")
            if not role.get("signature_id", "").startswith("light_cone."):
                raise ValueError("invalid guidance light cone id")
            if any(not c.get("id", "").startswith("light_cone.") for c in role.get("alternative_cones", [])):
                raise ValueError("invalid alternative light cone id")
            notes = role.get("stat_notes", {})
            if not isinstance(notes, dict) or any(
                not isinstance(key, str) or not isinstance(note, dict)
                or any(not isinstance(note.get(field), str) or not note[field].strip() for field in ("meaning", "check"))
                for key, note in notes.items()
            ):
                raise ValueError("invalid guidance stat notes")
            rules = role.get("stat_rules", {})
            if not isinstance(rules, dict) or any(not isinstance(rule, dict)
                    or rule.get("source_ref") not in self.sources
                    or not str(rule.get("trace_id", "")).startswith("character_trace.")
                    or rule.get("revision") != self.sources[rule["source_ref"]].get("revision")
                    or not isinstance(rule.get("reviewed_params"), list)
                    for rule in rules.values()):
                raise ValueError("invalid reviewed stat rule provenance")
        for team in self.teams.values():
            if len(team.get("slots", [])) != 4 or any(not slot for slot in team["slots"]):
                raise ValueError("guidance teams require four nonempty slots")
        for period in self.periods:
            for node in period.get("nodes", []):
                if not node.get("id") or not node.get("aliases") or not node.get("source_refs"):
                    raise ValueError("guidance node missing identity/source")
                if any(r not in self.sources for r in node["source_refs"]) or any(r not in self.teams for r in node.get("team_refs", [])):
                    raise ValueError("guidance node unknown source/team")
        if any(p.get("mode") not in MODES for p in self.periods):
            raise ValueError("unknown guidance mode")
        for mode in MODES:
            ordered = sorted((p for p in self.periods if p["mode"] == mode), key=lambda p: stamp(p["starts_at"]))
            if any(stamp(a["ends_at"]) > stamp(b["starts_at"]) for a, b in zip(ordered, ordered[1:])):
                raise ValueError("overlapping guidance periods")

    def provenance(self, refs):
        return [{"source_id": ref, **copy.deepcopy(self.sources[ref])} for ref in dict.fromkeys(refs)]


@dataclass(frozen=True)
class GuidancePlan:
    kind: str = "not_requested"
    character_ids: tuple[str, ...] = ()
    mode: str = ""
    period_ids: tuple[str, ...] = ()
    requires_pixels: bool = False
    no_signature: bool = False
    excluded_names: tuple[str, ...] = ()
    supplied_owned: tuple[str, ...] = ()
    node: str = ""
    all_modes: bool = False
    target_origin: str = "unresolved"
    proposed_teammates: tuple[str, ...] = ()
    request_focus: str = ""
    equipment_kinds: frozenset[str] = frozenset()
    reply_detail: str = "brief"
    consultation_topic: str = "general"
    condition_reply: bool = False
    consultation_action: str = "assess"
    focus_property: str = ""
    stated_equipment_ids: tuple[str, ...] = ()
    stated_equipment_origin: str = ""
    stat_property: str = ""
    player_stat_value: str = ""
    challenge_floor: int = 0
    challenge_difficulty: str = ""
    challenge_side: str = ""
    execution_fields: frozenset[str] | None = None
    execution_intent: str = ""
    subject_state: str = "legacy"
    subject_reason: str = ""
    assessment_scope: str = ""
    allow_visual_subject: bool = True
    evidence_source: str = ""
    source_state: str = ""
    guide_topics: frozenset[str] = frozenset()
    answer_parts: frozenset[str] = frozenset()

    @property
    def requested(self):
        return self.kind != "not_requested"

    @property
    def experimental(self):
        return self.kind == "challenge" or self.target_origin == "banner_calendar"

    def observation_plan(self, base: QueryPlan) -> QueryPlan:
        plan = self._observation_plan(base)
        # Only full changes what the observation must collect; "brief" is the
        # answer wording, not a new demand on the capture plan.
        return replace(plan, response_detail="full") if self.reply_detail == "full" else plan

    def _observation_plan(self, base: QueryPlan) -> QueryPlan:
        if not self.requested:
            return replace(base, response_detail="full") if self.reply_detail == "full" else base
        if self.execution_fields is not None:
            return replace(base, intent=self.execution_intent, planning_source="semantic_intent").with_fields(
                self.execution_fields, equipment_kinds=self.equipment_kinds)
        if self.requires_pixels:
            if self.kind == "challenge":
                # Caller hints cannot replace stage identifiers with a quest.
                # Keep real mixed demands found in the user's words instead.
                actual = build_query_plan(base.query, base.intent)
                fields = consultation_fields(base.query, actual.fields, "visible_text")
                return base.with_fields(fields, equipment_kinds=actual.equipment_kinds)
            if self.request_focus:
                # Carry a service demand, never a previous frame's values.
                fields = consultation_fields(base.query, base.fields, self.request_focus)
                return base.with_fields(fields, equipment_kinds=self.equipment_kinds)
            if self.kind == "build" and not self.character_ids:
                # A pronoun needs a selected subject, not generic page text.
                return base.with_fields(base.fields | {"characters"})
            if base.screen:
                return base
            return base.with_fields({"visible_text"})
        return base.with_fields(frozenset())

    def public(self):
        return {"kind": self.kind, "observation_required": self.requires_pixels,
                "character_count": len(self.character_ids), "mode": self.mode,
                "explicit_period": bool(self.period_ids), "target_origin": self.target_origin,
                "request_focus": self.request_focus, "signature_excluded": self.no_signature,
                "consultation_action": self.consultation_action,
                "subject_state": self.subject_state, "subject_reason": self.subject_reason,
                "assessment_scope": self.assessment_scope,
                "evidence_source": self.evidence_source, "source_state": self.source_state,
                "guide_topics": sorted(self.guide_topics), "answer_parts": sorted(self.answer_parts),
                "challenge_floor": self.challenge_floor or None,
                "challenge_difficulty": self.challenge_difficulty or None,
                "challenge_side": self.challenge_side or None}


def _names(query, catalog):
    records = catalog.match_characters_in_text(query)
    # Exact overlapping names (银狼 / 银狼LV.999) must not become two targets.
    selected = []
    for record in sorted(records, key=lambda r: -len(r["name"])):
        if not any(record["name"] in other["name"] for other in selected):
            selected.append(record)
    return selected


def signature_statement(query):
    """Clause-local assertions; an equipment question is not an ownership doubt."""
    signature = r"(?:专(?:武|锥)|专(?=了|呢|[？?，,。；;\s]|$))"
    mentioned = bool(re.search(signature, query))
    value = None
    for clause in re.split(r"[，,。；;！!\n]", query):
        if re.search(r"如果|假如|假设|要是|有没有|是否|是不是|有无", clause):
            continue
        for match in re.finditer(r"(没(?:有)?|不(?:用|带|要)|无|有|拿到|抽到)\s*(?:她的|他的|专属)?" + signature, clause):
            negative = match[1] in {"没", "没有", "不用", "不带", "不要", "无"}
            if not negative and re.search(r"吗|么|[？?]", clause):
                continue
            value = not negative
    return value, mentioned


def plan_request(base: QueryPlan, *, catalog, book, previous=None, explicit_fields=False,
                 discussion_character="", consultation=None) -> tuple[QueryPlan, GuidancePlan]:
    """Resolve the executable evidence once, before reading any evidence."""
    if base.intent == "assess_relic" and base.fields == {"equipment"} and asks_relic_rows_only(base.query):
        from .consultation_contract import validate_declaration
        declared = validate_declaration(consultation, intent=base.intent, previous=previous)
        if not (set((declared or {}).get("answer_parts", ())) - {"rows"}):
            read_plan = build_query_plan(base.query, "read_screen", screen_fields=("equipment",), response_detail="full")
            return read_plan, GuidancePlan()

    guidance = plan_guidance(base.query, base, catalog=catalog, book=book, previous=previous,
                             explicit_fields=explicit_fields, discussion_character=discussion_character,
                             consultation=consultation)
    observation = guidance.observation_plan(base)
    if guidance.requested and not observation.screen:
        observation = replace(observation, archived_guidance=True)
    return observation, guidance


def plan_guidance(query, base, *, catalog, book, previous=None, now_monotonic=None, now=None,
                  explicit_fields=False, discussion_character="", consultation=None):
    query = str(query or "")
    from .consultation_contract import answer_requirements, validate_declaration
    declared = validate_declaration(consultation, intent=base.intent, previous=previous)
    if base.intent == "read_screen" and previous:
        prior_detail = previous.get("reply_detail")
        if prior_detail in {"brief", "full"}:
            base = replace(base, response_detail=prior_detail)
    if base.intent in SEMANTIC_INTENTS:
        from .semantic_guidance import plan_semantic_guidance
        selected = plan_semantic_guidance(query, base, catalog=catalog, book=book, previous=previous,
                                     now_monotonic=now_monotonic, now=now, discussion_character=discussion_character,
                                     declaration=declared)
        prior_demand = previous if base.intent == "continue_consultation" and selected.character_ids else None
        return answer_requirements(selected, query, declaration=declared, previous=prior_demand,
                                   inherit_parts=base.intent == "continue_consultation")
    prior = previous or {}
    clock = time.monotonic() if now_monotonic is None else now_monotonic
    valid_prior = 0 <= clock - prior.get("at", -1e9) <= CONTEXT_TTL
    all_named = _names(query, catalog)
    signature_owned, signature_mentioned = signature_statement(query)
    prior_focus = prior.get("request_focus", "") if valid_prior else ""
    topic_reply = condition_reply(query, prior) if valid_prior else ""
    active = bool(valid_prior and prior.get("kind") == "build" and len(prior.get("character_ids") or []) == 1)
    follow_action = consultation_action(query, active=active or bool(all_named))
    stat_prop, player_value = player_stat_reply(query, prior) if active else ("", "")
    if player_value:
        follow_action = "stat_value"
    elif follow_action == "threshold":
        stat_prop = stat_property(query)
    declared = stated_equipment(query, prior=prior, book=book, catalog=catalog) if active else ()
    if declared:
        follow_action = "equipment_guide"
    short = re.sub(r"[\s～~。！!？?啊呀呢哦嘛（）()，,]", "", query)
    screen_followup = bool(prior_focus in {"equipment", "attributes"} and (
        short in {"看看", "你看看", "这个", "这件", "这套", "这把", "换成这件", "换成这个", "换成这把",
                  "对", "是的", "详细说说", "详细讲讲", "完整说说", "简单说说",
                  "那这件", "那这个", "那件", "那套", "那把", "那套呢", "那件呢"}
        or "词条" in query and follow_action not in KNOWLEDGE_ACTIONS
        and not any(w in query for w in ("什么意思", "是什么", "有哪些", "推荐", "怎么配", "如何配"))))
    constraint = any(w in query for w in ("没专", "换谁", "替代")) or bool(all_named and any(
        w in query for w in ("没有", "不带", "不用", "我有", "我只有", "我的角色有", "我已经练了")))
    constraint |= any(re.search(r"(?:没(?:有)?|缺|不带|不用|不要)\s*" + re.escape(r["name"]), query)
                      for r in all_named)
    negative_names = [r["name"] for r in all_named if re.search(
        r"(?:没(?:有)?|缺|不带|不用|不要)\s*" + re.escape(r["name"]), query)]
    bare_subject = catalog.resolve_character(query.strip(" 。！!？?啊呀呢"))
    clarification = bool(valid_prior and prior.get("awaiting_subject") and bare_subject)
    proposed = tuple(r["name"] for r in all_named if re.search(
        r"(?:换成|换为|改用|替换成)\s*" + re.escape(r["name"]), query))
    equipment_assessment = bool(any(w in query for w in ("光锥", "遗器", "装备", "词条", "这件", "这套"))
                               and any(w in query for w in ("行吗", "能用吗", "可以用吗", "合适吗", "适合", "怎么样", "换吗",
                                                            "注意什么", "注意哪些", "有什么影响")))
    attributes_assessment = panel_assessment(query)
    attributes_assessment |= bool(any(w in query for w in ("养得", "养的", "练度"))
                                  and any(w in query for w in ("怎么样", "好吗", "如何", "评价")))
    attributes_assessment |= bool(active and any(w in query for w in ("进阶属性", "基础属性"))
                                  and any(w in query for w in ("看看", "看下", "给你看", "评价", "分析")))
    if active and any(w in query for w in ("读错", "看错", "认错", "不是这件")):
        screen_followup = True
    advice = any(w in query for w in ("配队", "配装", "阵容", "怎么配", "如何配", "怎么搭配", "如何搭配", "怎么打", "如何打", "攻略", "推荐",
                                         "带什么", "用什么", "带啥", "用啥", "毕业", "打不过", "过不去", "替代", "换谁", "怎么选",
                                         "怎么养", "如何养", "养成建议", "培养建议"))
    advice |= equipment_assessment or attributes_assessment or bool(proposed) or screen_followup or bool(topic_reply) or bool(follow_action)
    challenge_followup = bool(valid_prior and prior.get("kind") == "challenge" and re.search(r"上半|下半|换谁|怎么换|怎么打|为什么|理由", query))
    advice |= challenge_followup
    gear_followup = bool(valid_prior and not base.screen and any(w in query for w in ("遗器", "光锥", "词条", "专武")))
    if not advice and not clarification and not negative_names and not (valid_prior and (constraint or signature_mentioned)) and not gear_followup:
        return GuidancePlan()
    exclusions = negative_names
    ownership = re.search(r"(?:我只有|我有|我的角色有|我已经练了)([^。？！?！\n]+)", query)
    if ownership and not _names(re.split(r"[，,]", ownership[1], maxsplit=1)[0], catalog):
        # '我有专武，真珠的遗器怎么配' asserts a cone, not every
        # character in the following clause. Existing character lists survive.
        ownership = None
    owned = tuple(r["name"] for r in _names(ownership[1], catalog) if r["name"] not in exclusions) if ownership else ()
    named = [r for r in all_named if r["name"] not in exclusions and r["name"] not in owned and r["name"] not in proposed]
    modes = [key for key, aliases in MODES.items() if any(alias in query for alias in aliases)]
    periods = [p for p in book.periods if p["name"] in query]
    node_matches = [(p, n) for p in book.periods for n in p.get("nodes", [])
                    if any(a in query for a in n["aliases"])]
    node = node_matches[0][1]["id"] if len({n["id"] for _, n in node_matches}) == 1 else ""
    all_modes = bool(re.search(r"(?:各|所有|全部).*(?:高难|副本)|本期高难", query))
    challenge = bool(modes or periods or node_matches or all_modes or challenge_followup
                     or any(w in query for w in ("试炼", "高难", "这关", "这一关"))
                     or "这期" in query and not any(w in query for w in ("卡池", "遗器", "光锥", "配装")) and not named)
    if challenge:
        follow_action = ""  # A new challenge must not inherit a panel consultation goal.
    equipment = any(w in query for w in ("遗器", "光锥", "专武", "词条", "配装", "套装", "毕业"))
    if challenge:
        kind = "challenge"
    elif clarification:
        kind = prior["kind"]
    elif named and advice:
        kind = "build"
    elif valid_prior and (constraint or proposed or signature_mentioned or screen_followup or topic_reply or follow_action or any(w in query for w in ("专武", "遗器", "光锥", "词条"))):
        kind = prior.get("kind", "not_requested")
    elif equipment or equipment_assessment or attributes_assessment or negative_names or proposed or any(w in query for w in ("配队", "阵容")):
        kind = "build"  # Missing target is clarification, not hallucination.
    else:
        return GuidancePlan()
    character_ids = tuple(r["id"] for r in named) if kind == "build" else ()
    origin = "player_text" if character_ids or modes or periods or node_matches else "unresolved"
    hint = catalog.resolve_character(discussion_character) if discussion_character else None
    if kind == "build" and not character_ids and hint:
        character_ids = (hint["id"],)
        origin = "conversation_hint"
    if kind == "build" and not character_ids and any(w in query for w in ("本期", "当前卡池", "这期卡池")):
        date = now or datetime.now(timezone.utc)
        character_ids = tuple(r["identity_id"] for r in book.characters if window(r, date) == "active"
                              and ("新角色" not in query or r["release"] == "new")
                              and ("复刻" not in query or r["release"] == "rerun"))
        if character_ids:
            origin = "banner_calendar"
    mode = modes[0] if len(modes) == 1 else (periods[0]["mode"] if periods and len({p["mode"] for p in periods}) == 1 else "")
    if not mode and node and len({p["mode"] for p, _ in node_matches}) == 1:
        mode = node_matches[0][0]["mode"]
    period_ids = tuple(p["id"] for p in periods)
    inherits_prior = bool(valid_prior and prior.get("kind") == kind and not character_ids and not modes and not periods
                          and not node_matches and not all_modes and not clarification)
    if inherits_prior:
        character_ids = tuple(prior.get("character_ids", ()))
        mode = prior.get("mode", "")
        period_ids = tuple(prior.get("period_ids", ()))
        if not node:
            node = prior.get("node", "")
        all_modes = bool(prior.get("all_modes"))
        origin = "discussion_context" if character_ids or mode or period_ids or all_modes else "unresolved"
    from .challenge import challenge_scope
    floor, difficulty = challenge_scope(query)
    side = "upper" if "上半" in query and "下半" not in query else "lower" if "下半" in query and "上半" not in query else ""
    if inherits_prior:
        floor = floor or prior.get("challenge_floor", 0)
        difficulty = difficulty or prior.get("challenge_difficulty", "")
    no_signature = signature_owned is False
    same_subject = (inherits_prior or clarification or
                    bool(screen_followup and prior_focus in {"equipment", "attributes"} and not character_ids) or
                    tuple(prior.get("character_ids", ())) == character_ids and bool(character_ids))
    if valid_prior and prior.get("kind") == kind and same_subject:
        has_signature = signature_owned is True
        no_signature = no_signature or bool(prior.get("no_signature")) and not has_signature
        exclusions = [name for name in dict.fromkeys([*prior.get("excluded_names", ()), *exclusions]) if name not in owned]
        if not owned:
            owned = tuple(prior.get("supplied_owned", ()))
        elif "我只有" not in query:
            owned = tuple(dict.fromkeys([*prior.get("supplied_owned", ()), *owned]))
    if explicit_fields and not active and not (named or hint or attributes_assessment or equipment_assessment):
        # Preserve non-consultation, explicit low-level callers. Only an
        # named/established consultation or an actual assessment resolves
        # over-broad model hints against the user's current purpose.
        follow_action = ""
    focus = "attributes" if attributes_assessment else "equipment" if equipment_assessment or screen_followup and "词条" in query or active and follow_action == "equipment_guide" else prior_focus if screen_followup or clarification or topic_reply or follow_action else ""
    equipment_kinds = base.required_equipment_kinds
    if focus == "equipment":
        if (screen_followup or clarification) and "词条" not in query:
            equipment_kinds = frozenset(prior.get("equipment_kinds", ()))
        elif "词条" in query:
            equipment_kinds = build_query_plan(query, screen_fields=["equipment"]).required_equipment_kinds
        elif "遗器" in query and "套装" not in query:
            equipment_kinds = frozenset({"main_stat", "sub_stat"})
        elif equipment_assessment and not any(w in query for w in ("光锥", "套装", "这套")):
            # "这件给真珠用怎么样" is an item-fit request, not a set-label
            # lookup. The same demand must drive reuse, direct and followup.
            equipment_kinds = frozenset({"main_stat", "sub_stat"})
    requires = bool(base.fields and any(word in query for word in PIXEL_WORDS))
    requires |= bool(attributes_assessment)
    requires |= bool(focus and (base.screen or screen_followup or any(w in query for w in ("看看", "看", "这个", "这件", "这套", "我", "现在"))))
    requires |= bool(equipment_assessment and base.screen)
    requires |= bool(clarification and focus)
    requires |= bool(explicit_fields or base.navigation or base.scene_only)
    unresolved = kind == "build" and not character_ids or kind == "challenge" and not (mode or period_ids or all_modes)
    requires |= bool(unresolved and any(w in query for w in ("这期", "这关", "这个", "当前", "现在", "画面", "截图", "她", "他")))
    requires |= bool(kind == "challenge" and mode == "moc" and not (floor and difficulty)
                     and any(w in query for w in ("这期", "当前", "现在", "这关", "页面", "画面")))
    # Explicit screen requirements are never downgraded by a knowledge route.
    requires |= bool(base.screen and base.query == query and any(w in query for w in ("页面", "界面"))
                     and not (character_ids or mode or period_ids))
    action = follow_action if follow_action and not (attributes_assessment or equipment_assessment or screen_followup) else "assess"
    if topic_reply:
        action = "howto"
    if clarification and prior.get("consultation_action") in KNOWLEDGE_ACTIONS:
        action = prior["consultation_action"]
    if action in KNOWLEDGE_ACTIONS and kind == "build" and character_ids:
        # General advice/player statements do not need current pixels. Tool
        # hints are not permission to manufacture an unrelated screen debt.
        requires = False
    focus_property = prior.get("focus_property", "") if valid_prior and same_subject else ""
    if declared and not same_subject:
        declared = stated_equipment(query, prior={"character_ids": list(character_ids)}, book=book, catalog=catalog)
    stated_ids = declared or (tuple(prior.get("stated_equipment_ids", ())) if valid_prior and same_subject else ())
    stated_origin = "player_statement" if declared else "discussion_context_player_statement" if stated_ids else ""
    detail = detail_preference(query) or (prior.get("reply_detail", "brief") if screen_followup or clarification else "brief")
    topic = topic_reply or (prior.get("consultation_topic", "general") if valid_prior and same_subject else "general")
    selected = GuidancePlan(kind, character_ids, mode, period_ids, requires, no_signature, tuple(exclusions), owned, node, all_modes,
                        origin, proposed, focus, equipment_kinds, detail, topic, bool(topic_reply), action,
                        focus_property, stated_ids, stated_origin, stat_prop, player_value, floor, difficulty, side)
    return answer_requirements(selected, query, previous=prior if valid_prior and same_subject else None,
                               inherit_parts=screen_followup or clarification)


def _entity(catalog, entity_id, *, expected_type):
    record = catalog.get(entity_id)
    if not record or record.get("entity_type") != expected_type or not record.get("sources"):
        return {"id": entity_id, "available": False}
    result = {"id": record["id"], "name": record["name"], "available": True, "sources": record["sources"]}
    if expected_type == "light_cone":
        result.update(path=record.get("path"), effect=copy.deepcopy(record.get("rank_effect") or {}))
    elif expected_type == "relic_set":
        result["effect"] = copy.deepcopy(record.get("desc") or record.get("properties") or {})
    return result


def team_options(refs, *, book, catalog, profile, plan):
    owned = set(plan.supplied_owned or (profile.get("owned_characters") or [])) - set(plan.excluded_names)
    result = []
    for ref in dict.fromkeys(refs):
        template = book.teams[ref]
        members, missing, unverified = [], [], []
        for choices in template["slots"]:
            allowed = [name for name in choices if name not in plan.excluded_names]
            # A form label is intentional; never substitute the base protagonist.
            verified = [name for name in allowed if name == "开拓者（欢愉）" or catalog.resolve_character(name)]
            confirmed = [name for name in verified if name in owned]
            if not verified:
                unverified.extend(allowed)
            selected = confirmed[0] if confirmed else (verified[0] if verified else "")
            members.append(selected)
            if not confirmed:
                missing.append({"choices": verified, "reason": "excluded" if not allowed else "ownership_or_form_unconfirmed"})
        result.append({"id": ref, "label": template["label"], "members": members,
                       "filled_slot_count": sum(bool(member) for member in members),
                       "complete": all(members) and len(set(members)) == 4,
                       "account_ready": not missing and not unverified,
                       "unconfirmed_slots": missing, "unresolved_names": unverified,
                       "excluded": list(plan.excluded_names), "conditions": template["conditions"],
                       "tags": template["tags"], "sources": book.provenance(template["source_refs"]),
                       "kind": "reference_team_not_optimal_or_enemy_verified"})
    return result


def build_guidance(plan, query, *, book, catalog, profile, visual=None, now=None):
    if not plan.requested:
        return {"requested": False}
    if plan.evidence_source == "saved_profile" or plan.source_state == "needs_source":
        from .saved_cone import build_saved_cone_guidance
        return build_saved_cone_guidance(plan, catalog=catalog, profile=profile)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("guidance clock requires timezone")
    visual = visual or {}
    labels = "\n".join(str(row.get("text", "")) for row in (visual.get("screen_facts") or {}).get("visible_text", []))
    guides, missing, refs, team_refs = [], [], [], []
    targets = plan.character_ids
    mode, period_ids = plan.mode, plan.period_ids
    origin = plan.target_origin
    # Only fresh, verified visible text can bind an otherwise missing target.
    # Player-stated names/modes remain usable regardless of the current page.
    if plan.requires_pixels and visual.get("fresh") and (visual.get("scene") or {}).get("verified"):
        if plan.kind == "challenge" and not mode and not period_ids:
            found = [p for p in book.periods if p["name"] in labels]
            if len(found) == 1:
                period_ids = (found[0]["id"],)
                mode = found[0]["mode"]
                origin = "current_visual"
            else:
                found_modes = [k for k, aliases in MODES.items() if any(alias in labels for alias in aliases)]
                if len(found_modes) == 1:
                    mode = found_modes[0]
                    origin = "current_visual"
        elif plan.kind == "build" and not targets and plan.allow_visual_subject:
            subjects = [c for c in visual.get("characters", []) if c.get("identity_scope") in {"detail_subject", "banner_label"}]
            if len(subjects) == 1:
                targets = (subjects[0]["entity_id"],)
                origin = "current_visual"
    current_equipment = list(dict.fromkeys(
        f["entity_id"] for f in (visual.get("screen_facts") or {}).get("facts", [])
        if visual.get("fresh") and f.get("kind") == "entity_label"
        and f.get("entity_type") in {"light_cone", "relic_set"} and f.get("entity_id")))
    assessment = {"requested": bool(plan.request_focus and plan.consultation_action not in KNOWLEDGE_ACTIONS), "kind": plan.request_focus,
                  "subject_ids": list(targets), "subject_origin": origin,
                  "visual_subject_confirmed": bool(visual.get("fresh") and len(targets) == 1 and any(
                      c.get("entity_id") == targets[0] and c.get("identity_scope") == "detail_subject"
                      for c in visual.get("characters") or [])),
                  "facts": [], "state": "not_requested", "evaluation_available": False}
    if assessment["requested"]:
        observed = plan.observation_plan(build_query_plan(query))
        assessment["facts"] = observed.relevant_facts((visual.get("screen_facts") or {}).get("facts") or []) if visual.get("fresh") else []
        from .query_coordinator import satisfied_fields
        assessment["state"] = "evidence_available" if plan.request_focus in satisfied_fields(observed, visual) else "partial" if assessment["facts"] else "unconfirmed"
        assessment["scope"] = "sourced_field_fit_not_damage_score_or_graduation_threshold"
        if assessment["state"] != "evidence_available":
            missing.append("当前装备/面板所需字段未读全；先回答已知角色与已明确条件下的资料，不能断言这件装备合适、需更换或面板达标")
        from .equipment import assess_cone, assess_panel, assess_relic
        from .equipment_view import equipment_card
        card = equipment_card(visual)
        if plan.request_focus == "equipment":
            assessment["equipment_answer"] = card
            from .equipment_view import equipment_object
            assessment["current_object"] = equipment_object(visual, card)
            if plan.equipment_kinds:
                # A loose database label is useful reference material, but
                # cannot identify this selected relic after its rows withdraw.
                current_equipment = [s["id"] for s in card["sets"]] if assessment["current_object"]["confirmed"] else []
        role = next((c for c in book.characters if len(targets) == 1 and c["identity_id"] == targets[0]), None)
        if role:
            sources = book.provenance(role["source_refs"])
            subjects = {c.get("entity_id") for c in visual.get("characters") or []
                        if c.get("identity_scope") == "detail_subject" and c.get("entity_id")} if visual.get("fresh") else set()
            if plan.request_focus == "attributes":
                assessment["panel_subject_state"] = ("confirmed" if subjects == set(targets) and len(subjects) == 1
                                                     else "mismatch" if subjects and set(targets).isdisjoint(subjects)
                                                     else "unconfirmed")
            evaluation = (assess_panel(visual, role, sources) if plan.request_focus == "attributes"
                          and assessment["panel_subject_state"] == "confirmed" else
                          {"available": False, "kind": "panel", "rows": [], "reason": "panel_subject_unbound"}
                          if plan.request_focus == "attributes" else
                          assess_cone(visual, role, sources, catalog) if any(f.get("entity_type") == "light_cone" for f in assessment["facts"])
                          and not card["main_stat"] else assess_relic(card, role, sources))
            assessment["evaluation"] = evaluation
            assessment["evaluation_available"] = evaluation["available"]
        assessment["answer_contract"] = {
            "observed_fact_source": "equipment_answer.label_and_value" if plan.request_focus == "equipment" else "evaluation.rows",
            "evaluation_source": "evaluation_only_when_available",
            "missing_rows_must_not_be_filled": True,
            "recommended_properties_are_not_observed": True,
            "instruction": "按reply_plan选择本轮重点，普通评价不重讲整篇配装；明确完整请求不压缩已确认词条。"
                           "实际字段与建议分开；没有比较对象不说必须换，未列优先不等于完全无用。"}
        missing.append("未接入全账号最优解、强化档次评分或带队伍条件的毕业阈值；逐项适配不等于整体优秀或必须替换")
        if plan.request_focus == "attributes" and not role:
            assessment["panel_subject_state"] = "unconfirmed"
    equipment_request = bool(plan.requires_pixels and any(w in query for w in ("这个光锥", "这件", "这套", "这把")))
    if equipment_request and len(current_equipment) != 1:
        missing.append("当前装备尚未唯一确认；配装候选不证明画面正在展示该装备")
    if plan.proposed_teammates:
        missing.append("玩家提议的替代队友协同尚未核实；不能因角色存在或提供通用增益就认可这套替代")
    if plan.kind == "build":
        selected = [c for c in book.characters if c["identity_id"] in targets]
        if not targets:
            missing.append("要讨论哪位角色的培养或配队？")
        if len(selected) < len(targets):
            missing.append("部分目标角色尚未收录4.6特化培养方案；原有角色资料查询仍可使用")
        for role in selected:
            form = catalog.get(role["form_id"])
            if form and form.get("identity_id") != role["identity_id"]:
                missing.append("角色形态与策略实体不一致，未采用该方案")
                continue
            from .mechanics import character_mechanics
            identity = catalog.get(role["identity_id"]) or {}
            mechanics = character_mechanics(identity, catalog=catalog) if identity else {"forms": []}
            mechanics["forms"] = [f for f in mechanics["forms"] if f["id"] == role["form_id"]]
            mechanics["needs_form_selection"] = False
            if not mechanics["forms"]:
                mechanics["missing_information"] = ["外置资料尚未提供这个形态的可解释技能，不能自行补机制"]
            cones = []
            if not plan.no_signature:
                signature = _entity(catalog, role["signature_id"], expected_type="light_cone")
                if signature["available"] and signature.get("path") != role["path"]:
                    missing.append("专属参考光锥命途不一致，未采用该配置")
                else:
                    cones.append({**signature, "role": "signature_reference",
                                  "condition": "仅已拥有或主动比较时考虑；不建议为使用插件而抽取"})
            for choice in role["alternative_cones"]:
                cone = _entity(catalog, choice["id"], expected_type="light_cone")
                if cone["available"] and cone.get("path") != role["path"]:
                    missing.append("光锥命途不一致，未采用该替代候选")
                    continue
                cones.append({**cone, "role": "conditional_alternative", **{k: choice[k] for k in ("reason", "condition", "basis")}})
            equipment = [_entity(catalog, entity_id, expected_type="relic_set")
                         for entity_id in [*role["relic_ids"], *role["ornament_ids"]]]
            guides.append({"id": role["id"], "name": role["name"], "identity_id": role["identity_id"],
                           "form_id": role["form_id"], "banner_status": window(role, now),
                           "main_stats": role["main_stats"], "substats": role["substats"],
                           "equipment_candidates": equipment, "light_cone_candidates": cones,
                           "mechanics": mechanics,
                           "principles": role["principles"], "sources": book.provenance(role["source_refs"]),
                           "coverage": "reviewed_build_candidates_not_account_simulation"})
            refs.extend(role["source_refs"])
            team_refs.extend(role["team_refs"])
            if any(not item["available"] for item in [*equipment, *cones]):
                missing.append("部分装备实体资料未安装或未覆盖；不补写名称、效果或强度")
    else:
        selected = [p for p in book.periods if p["id"] in period_ids] if period_ids else [
            p for p in book.periods if (not mode or p["mode"] == mode) and window(p, now) == "active"]
        if not mode and not period_ids and not plan.all_modes:
            selected = []
            missing.append("你指混沌回忆、虚构叙事、末日幻影还是异相仲裁？")
        if (mode or period_ids) and not selected:
            missing.append("没有已核对且适用这次期次的策略，不能用过期攻略冒充本期")
        for period in selected:
            nodes = [n for n in period.get("nodes", []) if not plan.node or n["id"] == plan.node]
            node_refs = [r for n in nodes for r in n["source_refs"]]
            guides.append({**copy.deepcopy(period), "nodes": [
                {**copy.deepcopy(n), "sources": book.provenance(n["source_refs"])} for n in nodes],
                           "period_status": window(period, now),
                           "sources": book.provenance(period["source_refs"]),
                           "coverage": "reviewed_nodes_and_period_rules" if nodes else "period_rules_and_conditional_candidates_not_full_node_guide"})
            refs.extend([*period["source_refs"], *node_refs])
            team_refs.extend([r for n in nodes for r in n.get("team_refs", [])] if plan.node else period["team_refs"])
            if plan.node and not nodes:
                missing.append("这个节点不在已核对范围，不能把其他节点队伍搬来冒充")
            missing.extend(period["missing"])
        if selected:
            missing.append("要进一步给具体队伍，需要关卡层数/节点、普通或星启模式，以及可用角色；只问最关键缺项，不要求重新打开整个游戏")
    teams = team_options(team_refs, book=book, catalog=catalog, profile=profile, plan=plan)
    conflicts = sorted({name for team in teams for name in team["members"] if name and
                        sum(name in other["members"] for other in teams) > 1})
    state = "partial" if guides and missing else "ready" if guides else "needs_input"
    if targets and not guides:
        state = "out_of_scope"
    if book.data.get("available") is False:
        state = "unavailable"
        missing.append("攻略组件暂不可用；原有基础资料和画面查询仍可使用，不编造配队")
    role = next((c for c in book.characters if len(targets) == 1 and c["identity_id"] == targets[0]), None)
    from .consultation_contract import assessment_completion
    completion = assessment_completion(plan, assessment, role=role, catalog=catalog)
    from .mechanics import reviewed_stat_advice
    stat_advice = reviewed_stat_advice(role, plan.stat_property, catalog=catalog, stated_value=plan.player_stat_value)
    reply_plan = build_reply_plan(plan, assessment, role, query=query, guides=guides, teams=teams, stat_advice=stat_advice)
    if assessment.get("panel_subject_state") in {"unconfirmed", "mismatch"}:
        panel_values = "、".join(
            f"{f.get('label')} {f.get('value')}" for f in assessment.get("facts") or []
            if f.get("value") and f.get("scope") == "attributes")
        reply_plan.update(conclusion=("当前面板属于另一角色，不能用于指定角色评价。"
                                      if assessment.get("panel_subject_state") == "mismatch" else
                                      ("这位角色的面板：" + panel_values + "；" if panel_values else "")
                                      + "还没确认角色归属，告诉我角色名我再评价适配。"),
                          next_step="", evaluation_available=False, response_state="unconfirmed")
    elif completion.get("state") == "rules_unavailable":
        reply_plan.update(conclusion="暂缺这个角色的评价依据。", next_step="", response_state="rules_unavailable")
    elif completion.get("state") == "needs_subject":
        reply_plan.update(conclusion="这件要给哪位角色用？", next_step="", response_state="needs_subject")
    from .challenge import build_challenge_plan, challenge_scope
    challenge_demand = plan
    if plan.kind == "challenge" and mode == "moc" and origin == "current_visual":
        floor, difficulty = challenge_scope(labels)
        challenge_demand = replace(plan, mode=mode, period_ids=period_ids,
                                   challenge_floor=plan.challenge_floor or floor,
                                   challenge_difficulty=plan.challenge_difficulty or difficulty)
    elif plan.kind == "challenge" and mode == "moc" and visual.get("fresh") and (visual.get("scene") or {}).get("verified"):
        # Scope labels must also contain this period, not a random normal menu.
        if any(p["name"] in labels for p in book.periods if p["mode"] == "moc"):
            floor, difficulty = challenge_scope(labels)
            challenge_demand = replace(plan, challenge_floor=plan.challenge_floor or floor,
                                       challenge_difficulty=plan.challenge_difficulty or difficulty)
    try:
        challenge = build_challenge_plan(challenge_demand, book=book, catalog=catalog, profile=profile, now=now)
    except (ValueError, KeyError, TypeError, AttributeError):
        # A bad optional D2 record must not take down D1 or screen facts.
        challenge = {"requested": plan.kind == "challenge" and mode == "moc", "state": "coverage_unavailable",
                     "assignments": [], "candidates": [], "missing_information": ["配队资料格式异常，未生成方案"],
                     "question": "本期节点资料需要核对，暂不生成上下半配队。"}
    imported_builds = copy.deepcopy(profile.get("imported_builds", {}))
    if plan.requires_pixels and plan.request_focus == "attributes":
        # Current-panel answers must not offer archive totals as replacement
        # measurements when admission is partial/unknown. Leave storage and
        # explicit archive/knowledge/D2 paths untouched, including cone data.
        for row in imported_builds.get("characters", []):
            if "panel" in row:
                row["panel"] = {"state": "withheld_for_current_screen", "source": "player_archive"}
        imported_builds["instruction"] = (
            "此处只提供保存的账号条件，不提供存档面板数值。当前属性只引用本次assessment的事实；"
            "本次未确认的属性保持未知，不能从存档或历史对话补成当前值。明确查存档时再读取保存资料。")
    result = {
        "requested": True, "kind": plan.kind, "state": state, "server": "cn", "game_version": book.data["game_version"],
        "evidence_source": plan.evidence_source or ("current_screen" if plan.requires_pixels else "reference_knowledge"),
        "source_state": plan.source_state,
        "revision": book.data["revision"], "checked_at": book.data["checked_at"],
        "data_freshness": guidance_freshness(book, now=now),
        "imported_builds": imported_builds,
        "target_origin": origin,
        "current_equipment_label_confirmed": len(current_equipment) == 1,
        "equipment_identity_scope": "visible_label_not_selection_or_wearer",
        "current_equipment_ids": current_equipment,
        "assessment": assessment,
        "completion": completion,
        "reply_plan": reply_plan,
        "guides": guides, "team_candidates": teams, "multi_team_conflicts": conflicts,
        "player_conditions": {"owned_source": "current_player_statement" if plan.supplied_owned else
                                               "confirmed_profile_and_player_builds" if profile.get("imported_builds", {}).get("scope") == "player_confirmed_builds" else
                                               "confirmed_profile_and_uid_snapshot" if profile.get("imported_builds") else "confirmed_plugin_profile",
                              "owned_characters": list(plan.supplied_owned or profile.get("owned_characters") or []),
                              "excluded_names": list(plan.excluded_names), "signature_excluded": plan.no_signature,
                              "proposed_teammates": list(plan.proposed_teammates),
                              "stated_equipment": [{"id": entity_id, "name": record["name"], "source": plan.stated_equipment_origin,
                                                    "visually_confirmed": False, "account_ownership_verified": False}
                                                   for entity_id in plan.stated_equipment_ids
                                                   if (record := catalog.get(entity_id)) and record.get("name") and record.get("sources")],
                              "roster_complete": False, "equipment_inventory_known": False},
        "missing_information": list(dict.fromkeys(missing)), "sources": book.provenance(refs),
        "instruction": "reply_plan.enabled=true时，本轮表达以reply_plan为准，guides和完整事实卡供核对，不要默认全部朗读；"
                       "mode=full必须列齐已确认的请求项，不能用简短要求截断；没有question不另开追问。"
                       "其他攻略需求先给有来源且适用的思路/候选，再问一个关键缺项。"
                       "guides是版本化攻略，不是看见当前画面或确认账号拥有。角色由玩家说清时，不以识别失败拒绝介绍方案。"
                       "候选套装不按列表顺序排名；参考队伍不是最优解/保证通关，未核实敌人不得假称针对该敌人。"
                       "本期只用active期次；具名历史/未来期次须说明状态。多个队伍有重复成员时不得当作可同时上场。"
                       "缺失资料不表示不存在；不得编固定伤害差距、毕业面板或继承未确认装备/星魂。"
                       "讨论对象已明确时不因画面未显示姓名重新追问角色；但不能声称已视觉确认归属。"
                       "适配问题先区分套装、主副词条和面板；只在assessment.evaluation_available=true时引用evaluation的逐项结论，不能扩大成极品、毕业或最优。"
                       "如需报数，逐字引用assessment.equipment_answer中label/value；推荐词条不是实际词条，缺行不补齐；不要把百分比数值转述成固定值。"
                       "参考队伍complete=false时不是完整四人队，不得自行补人或称四人阵容。"
                       "可以给条件建议，不能承诺最棒阵容。不要泛搜页面名称或不带游戏和期次的试炼；只登记过的followup可承诺补答。",
        "context": {"at": time.monotonic(), "kind": plan.kind, "character_ids": list(targets), "mode": mode,
                    "period_ids": list(period_ids), "no_signature": plan.no_signature,
                    "excluded_names": list(plan.excluded_names), "supplied_owned": list(plan.supplied_owned),
                    "node": plan.node, "all_modes": plan.all_modes,
                    "awaiting_subject": plan.kind == "build" and not targets,
                    "target_origin": origin, "request_focus": plan.request_focus,
                    "equipment_kinds": sorted(plan.equipment_kinds), "reply_detail": plan.reply_detail,
                    "guide_topics": sorted(plan.guide_topics), "answer_parts": sorted(plan.answer_parts),
                    "consultation_topic": plan.consultation_topic,
                    "consultation_action": plan.consultation_action,
                    "execution_intent": plan.execution_intent,
                    "subject_state": plan.subject_state,
                    "focus_property": reply_plan.get("primary_direction") or plan.focus_property,
                    "stated_equipment_ids": list(plan.stated_equipment_ids),
                    "stat_reply_property": plan.stat_property if plan.consultation_action == "threshold" else "",
                    "challenge_floor": challenge_demand.challenge_floor, "challenge_difficulty": challenge_demand.challenge_difficulty,
                    "question_offered": bool(reply_plan.get("question"))},
    }
    if challenge["requested"]:
        result["challenge_plan"] = challenge
        result["team_candidates"] = []  # Period-wide examples are not this node's allocated teams.
        result["multi_team_conflicts"] = []
        result["missing_information"] = challenge["missing_information"]
        result["instruction"] += "混沌期次规则仍可说明，但具体配队只采用challenge_plan：assignments为两队，candidates为待确认候选。"
        result["instruction"] += "assignments为空时不得用guides或聊天中的参考名单自行补足两队；只说明当前缺口。"
        result["instruction"] += challenge.get("instruction", "") + "必要澄清只问challenge_plan.question，不复问已明确的模式/层数。"
        result["state"] = challenge["state"]
    return result
