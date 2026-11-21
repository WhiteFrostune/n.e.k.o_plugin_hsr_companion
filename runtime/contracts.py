from __future__ import annotations

from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any


@dataclass(frozen=True)
class CompanionEvent:
    event_id: str
    kind: str
    priority: int
    ts: float
    payload: dict[str, Any] = field(default_factory=dict)
    max_age_seconds: float = 8.0
    cooldown_seconds: float = 8.0
    cooldown_key: str = ""
    proactive: bool = False
    preempt: bool = False


TEAM_MEMBER_SLOTS = 4
FRESH_LIVE_AGE_SECONDS = 15.0
STALE_MAX_AGE_SECONDS = 60.0
LIVE_PAGE_TYPES = frozenset({"exploration_hud", "map_or_quest", "combat", "story_dialogue"})

# Vocabulary shared by the planner (L3) and every consumer of a plan.
# Keep these data-only: no parsing and no question classification lives here.
TOPIC_WORDS = MappingProxyType({
    "skill": ("技能", "战技", "终结技", "天赋", "行迹", "星魂"),
    "equipment": ("装备", "遗器", "光锥", "词条", "套装"),
    "quest": ("任务", "目标", "接下来", "干嘛", "干吗", "做啥"),
    "attributes": ("属性", "面板", "攻击力", "速度", "暴击", "培养", "等级"),
    "location": ("地点", "位置", "地区", "在哪", "哪里", "哪儿"),
    "enemy": ("敌人", "怪物", "弱点", "机制"),
    "dialogue": ("对话", "剧情", "台词", "说什么"),
    "resource": ("星琼", "专票", "通票", "信用点"),
    "visible_text": ("页面文字", "界面文字", "写的什么", "写了什么", "什么内容", "页面说明", "页面内容", "界面内容"),
})
ENTITY_TOPICS = MappingProxyType({
    "skill": frozenset({"character_skill", "character_trace", "character_rank"}),
    "equipment": frozenset({"light_cone", "relic_set", "item"}),
})
SCREEN_FIELDS = ("page", "location", "quest", "characters", "controlled_character",
                 "visible_text", "skill", "equipment", "attributes", "enemy", "dialogue", "resource")
RESOURCE_LABELS = MappingProxyType({"星琼": "星琼", "专票": "星轨专票", "通票": "星轨通票", "信用点": "信用点"})
SEMANTIC_INTENTS = frozenset({"read_screen", "explain_page", "assess_relic", "assess_cone", "assess_panel",
                              "equipment_guide", "build_guide", "team_guide", "challenge_guide", "continue_consultation",
                              "build_priority", "build_explain", "build_howto", "stat_threshold", "stat_value"})
CURRENT_SCOPES = {"current_relic", "current_cone", "current_panel"}
CORRECTION_REASONS = {"read_screen_current_scope_dropped", "read_screen_declaration_dropped",
                      "current_screen_subject_inferred", "stale_reference_build_for_team_dropped"}


@dataclass(frozen=True)
class QueryPlan:
    """One accepted demand, immutable across grounding and delayed answers.

    The type lives in contracts so L4 analysis and sidebars can refer to a plan
    without importing the L3 planner. The decision that builds it stays in
    ``runtime/query_plan.py``.
    """

    query: str
    intent: str
    screen: bool
    identity: bool
    content: bool
    scene_only: bool
    navigation: bool
    controlled: bool
    topics: frozenset[str]
    fields: frozenset[str]
    equipment_kinds: frozenset[str] = frozenset()
    planning_source: str = "legacy_keywords"
    response_detail: str = ""
    archived_guidance: bool = False

    def with_fields(self, fields, *, equipment_kinds=None):
        """Derive execution switches from ONE accepted field set.

        No semantic classification, capture, proof relaxation or history lookup.
        Guidance and structured/legacy callers share this same projection.
        """
        fields = frozenset(fields)
        scene = bool(fields) and fields <= {"page", "location"}
        navigation = "quest" in fields and fields <= {"page", "location", "quest"}
        return replace(self, fields=fields, screen=bool(fields),
            identity=bool(fields & {"page", "characters", "controlled_character"}),
            controlled="controlled_character" in fields, scene_only=scene, navigation=navigation,
            content=bool(fields - {"page", "characters", "controlled_character"}) and not scene,
            topics=frozenset(fields & TOPIC_WORDS.keys()),
            equipment_kinds=(self.equipment_kinds if equipment_kinds is None else frozenset(equipment_kinds))
                            if "equipment" in fields else frozenset())

    @property
    def required_equipment_kinds(self) -> frozenset[str]:
        if "equipment" not in self.fields:
            return frozenset()
        if self.equipment_kinds:
            return self.equipment_kinds
        if self.intent == "assess_cone":
            return frozenset()
        if self.response_detail == "full":
            return frozenset({"main_stat", "sub_stat"})
        if "主副词条" in self.query or "主词条" in self.query and "副词条" in self.query:
            return frozenset({"main_stat", "sub_stat"})
        if "主词条" in self.query:
            return frozenset({"main_stat"})
        if "副词条" in self.query:
            return frozenset({"sub_stat"})
        return frozenset({"main_stat", "sub_stat"}) if "词条" in self.query else frozenset()

    @property
    def required_resource_labels(self) -> frozenset[str]:
        return frozenset(label for word, label in RESOURCE_LABELS.items() if word in self.query)

    @property
    def requires_quest_objective(self) -> bool:
        return any(word in self.query for word in ("接下来", "做什么", "做啥", "干嘛", "干吗", "任务目标", "怎么做"))

    def refresh_args(self) -> dict:
        # Legacy OCR API's require_characters also selects the full read path.
        # Keep that contract while avoiding roster dependencies in readiness.
        return {"require_characters": not (self.scene_only or self.navigation),
                "require_content": self.content and not self.navigation,
                "require_navigation": self.navigation}

    def public(self) -> dict:
        # No query text, guessed names, paths, or observed facts.
        result = {"screen": self.screen, "fields": sorted(self.fields), "topics": sorted(self.topics),
                  "planning_source": self.planning_source,
                  "evidence_source": "current_screen" if self.screen else
                                     "saved_or_reference" if self.archived_guidance else "optional_context",
                  "read_path": "navigation" if self.navigation else "scene" if self.scene_only else "full" if self.screen else "knowledge"}
        if self.required_equipment_kinds:
            result["equipment_kinds"] = sorted(self.required_equipment_kinds)
        return result

    def relevant_facts(self, facts: list[dict]) -> list[dict]:
        topics = self.topics
        relevant = [f for f in facts if not topics or f.get("scope") in topics
                or "quest" in topics and f.get("kind") == "quest_destination"
                or "equipment" in topics and f.get("scope") == "attributes"
                or topics & {"skill", "equipment", "attributes"} and f.get("scope") == "progression"
                or f.get("kind") == "entity_label" and any(f.get("entity_type") in ENTITY_TOPICS.get(t, ()) for t in topics)]
        labels = self.required_resource_labels
        return [f for f in relevant if f.get("scope") != "resource" or not labels or f.get("label") in labels]


def team_roster_state(page_type: Any, characters: Any, slots: Any = None) -> dict[str, Any]:
    """Team setup has four positions; empty cards are not missing members.

    With slot states, completeness is "every filled card has a name"; a
    three-member team is complete, while a filled card without a name is
    incomplete. Without slots, keep the conservative four-position rule.
    """
    if page_type != "team_setup":
        return {}
    if slots:
        filled = [slot for slot in slots if slot.get("state") in {"filled_named", "filled_unread"}]
        read = sum(1 for slot in slots if slot.get("state") == "filled_named")
        unread = [slot for slot in slots if slot.get("state") == "filled_unread"]
        unknown = [slot for slot in slots if slot.get("state") == "unknown"]
        return {
            "team_members_expected": len(filled),
            "team_members_read": read,
            "characters_complete": not unread,
            "team_roster_known": not unknown,
            "team_slots": [{key: slot.get(key) for key in ("slot", "state", "name") if slot.get(key) is not None}
                           for slot in slots],
        }
    seen = {
        item.get("entity_id") or item.get("name")
        for item in characters or []
        if isinstance(item, dict)
        and item.get("identity_scope") in {None, "team_selection"}
        and (item.get("entity_id") or item.get("name"))
    }
    read = len(seen)
    return {
        "team_members_expected": TEAM_MEMBER_SLOTS,
        "team_members_read": read,
        "characters_complete": read >= TEAM_MEMBER_SLOTS,
    }


__all__ = ["CompanionEvent", "TEAM_MEMBER_SLOTS", "FRESH_LIVE_AGE_SECONDS",
           "STALE_MAX_AGE_SECONDS", "LIVE_PAGE_TYPES", "team_roster_state",
           "TOPIC_WORDS", "ENTITY_TOPICS", "SCREEN_FIELDS", "RESOURCE_LABELS",
           "SEMANTIC_INTENTS", "CURRENT_SCOPES", "CORRECTION_REASONS", "QueryPlan"]


def visual_fingerprints_match(left: object, right: object, *, max_distance: int = 8) -> bool:
    first = str(left or "").strip().lower()
    second = str(right or "").strip().lower()
    if not first or not second:
        return False
    if first.startswith("dhash:") and second.startswith("dhash:"):
        try:
            distance = (int(first[6:], 16) ^ int(second[6:], 16)).bit_count()
        except ValueError:
            return False
        return distance <= max(0, min(int(max_distance), 16))
    return first == second
