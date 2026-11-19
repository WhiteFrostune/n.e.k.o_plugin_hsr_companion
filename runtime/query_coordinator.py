"""Coordinate existing observation workers; never start a parallel OCR path."""

from __future__ import annotations

import asyncio
import copy
import time
from dataclasses import dataclass

from .equipment_view import equipment_readiness
from .navigation import navigation_answer
from .observation_store import ObservationSnapshot
from .query_plan import ENTITY_TOPICS, QueryPlan


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


def answer_status(plan: QueryPlan, visual: dict, observer: dict, followup: dict | None = None) -> dict:
    evaluated = evaluate_readiness(plan, visual)
    ready = evaluated.confirmed
    missing = plan.fields - ready
    resource_labels = evaluated.resource_labels
    partial_resources = bool("resource" in plan.fields and plan.required_resource_labels & resource_labels)
    equipment_kinds = {f.get("kind") for f in (visual.get("screen_facts") or {}).get("facts") or []
                       if visual.get("fresh") and f.get("scope") == "equipment"}
    partial_equipment = bool(plan.required_equipment_kinds and equipment_kinds)
    if not plan.screen:
        state = "not_requested"
    elif not observer.get("enabled") or observer.get("state") in {"paused", "stopped"}:
        state = "paused"
    elif not missing:
        state = "ready"
    elif ready or partial_resources or partial_equipment:
        state = "partial"
    elif observer.get("state") == "error":
        state = "capture_failed"
    else:
        state = "unconfirmed"
    return {"state": state, "confirmed_fields": sorted(ready), "missing_fields": sorted(missing),
            "freshness": visual.get("freshness") or "fresh",
            "age_seconds": visual.get("age_seconds"),
            "age_label": visual.get("age_label") or "",
            "resource_fields": {"confirmed": sorted(plan.required_resource_labels & resource_labels),
                                "missing": sorted(plan.required_resource_labels - resource_labels)},
            "equipment_fields": evaluated.equipment,
            "applicable": evaluated.applicable,
            "followup_registered": bool((followup or {}).get("pending")),
            "connection": "not_checked",
            "instruction": "state 只描述请求画面字段的可答程度，不代表角色评价、正文交付或宿主发声完成；服务完成另看 request_completion。"
                           "这也不是助手连接状态；unconfirmed/partial 不证明断连、切页或游戏加载。"
                           "只答已确认部分；只有 followup_registered=true 才能说明已登记补答，不能保证宿主一定发声。"}


def needs_followup(plan: QueryPlan, visual: dict, observer: dict, *, completion=None) -> bool:
    """Missing service evidence, not merely missing numbers or a slow worker."""
    if not observer.get("enabled") or observer.get("state") in {"paused", "stopped", "waiting_for_game"}:
        return False
    from .request_completion import evaluate_request
    return evaluate_request(plan, visual, completion=completion).can_wait


@dataclass(frozen=True)
class QueryObservation:
    snapshot: ObservationSnapshot
    refresh: dict
    wait_seconds: float
    generation: int


async def resolve_query(owner, plan: QueryPlan, *, refresh, timeout: float, generation: int | None = None) -> QueryObservation:
    """Bound coordination and copy its trace before another request can mutate it."""
    started = time.monotonic()
    generation = getattr(owner, "_answer_query_generation", 0) if generation is None else generation
    deadline = started + timeout
    diagnostics = {}
    selected = None
    if plan.archived_guidance:
        from .observation import public_visual_evidence
        selected = public_visual_evidence({"invalidated": True, "source": "assistant_local_ocr"}, owner._observer_status)
        return QueryObservation(ObservationSnapshot.select(owner, selected), {"outcome": "not_requested", "stages": []}, 0.0, generation)
    if plan.screen and generation == getattr(owner, "_answer_query_generation", 0):
        # Legacy refresh state has one trace/one query gate. Serialize only
        # coordination, not OCR, and let newer questions supersede old ones.
        lock = getattr(owner, "_query_coordinator_lock", None)
        if lock is None:
            owner._query_coordinator_lock = lock = asyncio.Lock()
        active = getattr(owner, "_active_coordinated_refresh", None)
        if active is not None and active[0] != generation and not active[1].done():
            # Cancel only the obsolete query waiter, not the shared OCR worker.
            active[1].cancel()
        acquired = False
        refresh_task = None
        try:
            await asyncio.wait_for(lock.acquire(), timeout=timeout)
            acquired = True
            if generation == getattr(owner, "_answer_query_generation", 0):
                settings = await asyncio.wait_for(owner._read_observer_settings(), timeout=max(.001, deadline - time.monotonic()))
                task = getattr(owner, "_observer_task", None)
                if settings.get("enabled") and (task is None or task.done()):
                    owner._start_observer_task(restored=True)
                remaining = timeout - (time.monotonic() - started)
                if remaining > 0:
                    owner._last_refresh_diagnostics = {"outcome": "observer_unavailable", "stages": []}
                    refresh_task = asyncio.create_task(refresh(owner, timeout=remaining, query_plan=plan, **plan.refresh_args()))
                    owner._active_coordinated_refresh = (generation, refresh_task)
                    selected = await asyncio.wait_for(refresh_task, timeout=remaining)
                    diagnostics = copy.deepcopy(getattr(owner, "_last_refresh_diagnostics", {}))
                else:
                    diagnostics = {"outcome": "timeout", "stages": []}
            else:
                diagnostics = {"outcome": "cancelled", "stages": []}
        except TimeoutError:
            diagnostics = copy.deepcopy(getattr(owner, "_last_refresh_diagnostics", {})) if refresh_task else {}
            diagnostics.update(outcome="timeout", stages=diagnostics.get("stages", []))
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling() or generation == getattr(owner, "_answer_query_generation", 0):
                raise
            diagnostics = {"outcome": "cancelled", "stages": []}
        finally:
            if refresh_task is not None and getattr(owner, "_active_coordinated_refresh", None) == (generation, refresh_task):
                owner._active_coordinated_refresh = None
            if acquired:
                lock.release()
    elif plan.screen:
        diagnostics = {"outcome": "cancelled", "stages": []}
    if not isinstance(selected, dict):
        try:
            from .review_policy import offline_image_context
            if plan.screen and not offline_image_context(owner):
                # Panel/candidate availability is not a live review receipt.
                # A timeout must not turn the preserved display into an answer.
                from .observation import public_visual_evidence
                selected = public_visual_evidence({"invalidated": True, "source": "assistant_local_ocr"}, owner._observer_status)
            else:
                # Do not add an unbounded state read after the recognition deadline.
                selected = await asyncio.wait_for(owner._current_visual_evidence(),
                                                  timeout=max(.001, deadline - time.monotonic()))
        except TimeoutError:
            from .observation import public_visual_evidence
            selected = public_visual_evidence({"invalidated": True, "source": "assistant_local_ocr"}, owner._observer_status)
            if plan.screen and diagnostics.get("outcome") != "cancelled":
                diagnostics = {"outcome": "timeout", "stages": diagnostics.get("stages", [])}
    snapshot = ObservationSnapshot.select(owner, selected, fields=plan.fields)
    if plan.screen:
        selected = snapshot.delivery(owner, generation=generation)
        snapshot = ObservationSnapshot.select(owner, selected, fields=plan.fields)
    return QueryObservation(snapshot, diagnostics, round(time.monotonic() - started, 3), generation)
