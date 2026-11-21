"""Read only the regions a query needs, from the committed page geometry."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from io import BytesIO

from PIL import Image

from ..adapters.native_text import ObservationSuperseded
from ..detectors.character import detect_characters
from ..detectors.team import assign_slot_names, slot_card_states
from .contracts import team_roster_state
from .live_refresh import fetch_recent_frame, trace_refresh
from .regions import cached_region_plan, region_plan, remember_region_plan
from .screen_facts import extract_screen_facts
from .vision_scheduler import vision_turn
from .vision_trace import traced_operation


def _named_boxes(boxes, width, height, catalog):
    found = []
    if catalog is None:
        return found
    for box in boxes:
        text = str(box.get("text") or "").strip()
        if not text:
            continue
        for record in catalog.match_characters_in_text(text):
            found.append({
                "entity_id": record.get("id"), "name": record.get("name"),
                "region": {
                    "left": float(box["left"]) / max(width, 1),
                    "top": float(box["top"]) / max(height, 1),
                    "right": float(box["right"]) / max(width, 1),
                    "bottom": float(box["bottom"]) / max(height, 1),
                },
            })
    return found


def _team_slots(owner, boxes, latest, catalog):
    with Image.open(BytesIO(latest.image_bytes)) as image:
        width, height = image.size
        states = slot_card_states(image)
        return assign_slot_names(states, _named_boxes(boxes, width, height, catalog))


def _known_page(visual):
    return (visual.get("page") or {}).get("page_type") or visual.get("page_type")


@traced_operation("region_read")
async def read_query_regions(owner, plan, deadline):
    if plan is None or not getattr(plan, "fields", None):
        trace_refresh(owner, "region_skip", reason="no_fields")
        return None
    vision = getattr(owner, "_vision", None)
    if not callable(getattr(vision, "read_region", None)):
        trace_refresh(owner, "region_skip", reason="no_reader")
        return None
    committed = getattr(owner, "_committed_observation", None) or {}
    if not committed:
        trace_refresh(owner, "region_skip", reason="no_commit")
        return None
    facts = (committed.get("screen_facts") or {}).get("facts") or []
    plan_regions = region_plan(committed, plan)
    if plan_regions is None:
        plan_regions = cached_region_plan(owner, committed, plan)
        if plan_regions is not None:
            trace_refresh(owner, "region_relook_cached", page=plan_regions["page_type"])
    if plan_regions is None:
        page = _known_page(committed)
        trace_refresh(owner, "region_plan_missing", page=page, fields=sorted(plan.fields),
                      fact_count=len(facts), fact_kinds=sorted({f.get("kind") for f in facts if f.get("kind")}))
        return None
    regions = [item["region"] for item in plan_regions["regions"]]
    if not regions:
        trace_refresh(owner, "region_skip", reason="empty_regions", page=plan_regions["page_type"])
        return None
    boxes, latest = [], None
    best_boxes, best_frame, best_slots = [], None, []
    texts = []
    started = time.monotonic()
    attempts = 0
    while attempts < 2:
        attempts += 1
        if time.monotonic() >= deadline:
            trace_refresh(owner, "region_skip", reason="deadline")
            return None
        latest = await fetch_recent_frame(owner)
        if latest is None:
            trace_refresh(owner, "region_skip", reason="no_frame")
            return None
        try:
            async with vision_turn(owner, "query"):
                boxes = await asyncio.to_thread(
                    vision.read_region, latest.image_bytes, regions,
                    cancelled=lambda: time.monotonic() >= deadline)
        except ObservationSuperseded:
            if attempts == 1:
                await asyncio.sleep(.1)
                continue
            trace_refresh(owner, "region_skip", reason="busy")
            return None
        if not boxes:
            trace_refresh(owner, "region_relook", attempt=attempts, reason="empty")
            if best_boxes:
                break  # An optional completeness retry never discards a good read.
            if attempts == 1:
                await asyncio.sleep(.05)
                continue
            break
        best_boxes, best_frame = boxes, latest
        texts.append("\n".join(str(box.get("text") or "") for box in boxes))
        if plan_regions["page_type"] != "team_setup":
            break
        slots = _team_slots(owner, boxes, latest, owner._catalog)
        best_slots = slots
        characters_now = detect_characters("\n".join(texts), owner._catalog)
        roster = team_roster_state(plan_regions["page_type"], characters_now, slots=slots)
        if roster["characters_complete"] or not roster.get("team_roster_known", True):
            break
        trace_refresh(owner, "team_roster_short", attempt=attempts,
                      characters=roster["team_members_read"],
                      expected=roster["team_members_expected"])
        if time.monotonic() + 1.0 >= deadline:
            break
        await asyncio.sleep(.05)
    boxes, latest = best_boxes, best_frame
    if not boxes or latest is None:
        trace_refresh(owner, "region_skip", reason="no_boxes")
        return None
    with Image.open(BytesIO(latest.image_bytes)) as image:
        width, height = image.size
    page = committed.get("page") or {"page_type": plan_regions["page_type"]}
    page = {**page, "page_type": page.get("page_type") or plan_regions["page_type"]}
    facts = extract_screen_facts(boxes, width=width, height=height, page=page,
                                 catalog=getattr(owner, "_catalog", None))
    characters = []
    if "characters" in plan.fields:
        characters = detect_characters("\n".join(texts), owner._catalog)
    if page["page_type"] == "team_setup" and best_slots:
        by_id = {item.get("entity_id"): item for item in characters}
        characters = []
        for slot in best_slots:
            if slot.get("state") != "filled_named":
                continue
            known = by_id.get(slot.get("entity_id")) or {}
            characters.append({
                "entity_id": slot.get("entity_id"), "name": slot.get("name"),
                "evidence": known.get("evidence", "exact_catalog_text"),
                "confidence": known.get("confidence", "high"),
            })
    roster = team_roster_state(page["page_type"], characters, slots=best_slots)
    result = {
        "fresh": True,
        "page": page,
        "page_type": page["page_type"],
        "scene": committed.get("scene") or {"primary_state": "unknown", "verified": True},
        "characters": characters,
        "screen_facts": facts,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "read_scope": "full",
        "status": "observing",
        **roster,
    }
    remember_region_plan(owner, plan_regions)
    if page["page_type"] == "team_setup" and best_slots:
        # A later stale fallback may only have a full-frame snapshot without
        # slot states. Keep the last slot table so it can still mark a short
        # old roster incomplete instead of presenting it as complete.
        owner._team_slot_states = [dict(slot) for slot in best_slots]
    if roster and not roster["characters_complete"]:
        trace_refresh(owner, "team_roster_incomplete", read=roster["team_members_read"],
                      expected=roster["team_members_expected"],
                      slots=[slot.get("state") for slot in best_slots])
    if roster.get("team_roster_known") is False:
        trace_refresh(owner, "team_roster_unknown", slots=[slot.get("state") for slot in best_slots])
    trace_refresh(owner, "region_query_ready", page=page["page_type"], regions=len(regions),
                  boxes=len(boxes), characters=len(characters),
                  characters_complete=roster.get("characters_complete"),
                  roster_known=roster.get("team_roster_known"),
                  read_seconds=round(time.monotonic() - started, 3))
    return result
