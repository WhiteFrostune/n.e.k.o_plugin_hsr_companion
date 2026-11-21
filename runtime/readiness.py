"""Stateless field readiness shared by planning, analysis, and delivery."""
from __future__ import annotations

from dataclasses import dataclass

from .contracts import ENTITY_TOPICS, QueryPlan
from .equipment_view import equipment_readiness
from .navigation import navigation_answer


@dataclass(frozen=True)
class FieldReadiness:
    confirmed: frozenset[str]
    available: frozenset[str]
    equipment: dict
    resource_labels: frozenset[str]
    applicable: bool


def navigation_applicable(plan: QueryPlan, visual: dict) -> bool:
    return not (plan.navigation and visual.get("fresh") and visual.get("scene", {}).get("verified")
                and visual.get("page_type") not in {"exploration_hud", "mission_menu", "map_or_quest", "unknown"})


def evaluate_readiness(plan: QueryPlan, visual: dict) -> FieldReadiness:
    """Shared semantic result, separate from pixel permission and task completion."""
    equipment = equipment_readiness(visual, plan.required_equipment_kinds)
    if not visual.get("fresh"):
        return FieldReadiness(frozenset(), frozenset(), equipment, frozenset(), True)
    ready = set()
    available = set()
    if (visual.get("scene") or {}).get("verified"):
        ready.add("page")
    nav = navigation_answer(visual, requested=True)
    if nav["current_location"]["known"]:
        ready.add("location")
    if nav["objective"]["known"] or not plan.requires_quest_objective and nav["quest_title"]["known"]:
        ready.add("quest")
    if visual.get("characters"):
        # Only a reader that knows the member positions may declare the list
        # incomplete; a full-frame read keeps its existing partial semantics.
        if visual.get("characters_complete") is False and "characters" in plan.fields:
            available.add("characters")
        else:
            ready.add("characters")
    if visual.get("controlled_character"):
        ready.add("controlled_character")
    screen = visual.get("screen_facts") or {}
    if visual.get("read_scope", "full") == "full":
        if screen.get("visible_text"):
            ready.add("visible_text")
        relevant = plan.relevant_facts(screen.get("facts") or [])
        for topic in plan.topics - {"location", "quest", "visible_text"}:
            if topic == "equipment" and plan.required_equipment_kinds:
                if not equipment["missing"]:
                    ready.add(topic)
                if equipment["observed"]:
                    available.add(topic)
                continue
            if topic == "resource" and plan.required_resource_labels:
                labels = {f.get("label") for f in relevant if f.get("kind") == "resource_count"}
                if plan.required_resource_labels <= labels:
                    ready.add(topic)
                if plan.required_resource_labels & labels:
                    available.add(topic)
                continue
            if any(f.get("scope") == topic or f.get("kind") == "entity_label"
                   and f.get("entity_type") in ENTITY_TOPICS.get(topic, ()) for f in relevant):
                ready.add(topic)
    available |= ready
    pending = {c.get("scope") for c in screen.get("field_checks") or [] if c.get("state") == "pending"}
    # Equipment uses per-kind coverage above; a pending substat cannot block
    # an independently confirmed main-only request.
    ready -= pending - {"equipment"}
    labels = frozenset(f.get("label") for f in screen.get("facts") or [] if f.get("kind") == "resource_count")
    return FieldReadiness(frozenset(ready & plan.fields), frozenset(available & plan.fields), equipment,
                          labels, navigation_applicable(plan, visual))


def satisfied_fields(plan: QueryPlan, visual: dict) -> frozenset[str]:
    return evaluate_readiness(plan, visual).confirmed


def has_answerable_content(plan: QueryPlan, visual: dict) -> bool:
    """A useful partial answer is not a complete answer or an empty cache hit."""
    status = evaluate_readiness(plan, visual)
    # A confirmed unrelated menu is a terminal, useful page answer, not a
    # reason to wait for an exploration quest which cannot be on that page.
    return bool(status.available or not status.applicable)


def answer_coverage(plan: QueryPlan, visual: dict | None) -> tuple:
    """Compare request coverage, NEVER proof or permission to use a packet.

    Cache selection compares reviewed packets with this same measure. The
    follow-up caller may keep a richer current candidate, but still has to
    validate it before delivery. Metadata cannot outweigh requested numbers.
    """
    if not visual or not visual.get("fresh"):
        return (0, 0, 0)
    status = evaluate_readiness(plan, visual)
    facts = plan.relevant_facts((visual.get("screen_facts") or {}).get("facts") or [])
    if plan.required_equipment_kinds:
        facts = [f for f in facts if f.get("kind") in plan.required_equipment_kinds]
    rows = {(f.get("kind"), f.get("label"), str(f.get("value"))) for f in facts if f.get("value")}
    return (len(status.confirmed), len(status.available), len(rows))


def fields_for_page(page):
    """Build the existing manual-refresh demand without interpreting a prompt."""
    fields = {"relic_detail": ["equipment"], "exploration_hud": ["location", "quest"],
              "character_detail": ["characters", "attributes"], "team_setup": ["characters"]}.get(page)
    if not fields:
        return None
    query = "主副词条" if page == "relic_detail" else ""
    return QueryPlan(query, "conversation", False, False, False, False, False, False,
                     frozenset(), frozenset(), planning_source="structured_fields").with_fields(fields)
