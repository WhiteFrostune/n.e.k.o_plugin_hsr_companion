"""Read only the regions a query needs, from the committed page geometry."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from io import BytesIO

from PIL import Image

from ..adapters.text_reader import ObservationSuperseded
from ..detectors.character import detect_characters
from .contracts import TEAM_MEMBER_SLOTS, team_roster_state
from .live_refresh import fetch_recent_frame, trace_refresh
from .regions import cached_region_plan, region_plan, remember_region_plan
from .screen_facts import extract_screen_facts
from .vision_scheduler import vision_turn


def _known_page(visual):
    return (visual.get("page") or {}).get("page_type") or visual.get("page_type")


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
    best_boxes, best_frame = [], None
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
        read = len({item["entity_id"] for item in detect_characters("\n".join(texts), owner._catalog)})
        if read >= TEAM_MEMBER_SLOTS:
            break
        trace_refresh(owner, "team_roster_short", attempt=attempts, characters=read,
                      expected=TEAM_MEMBER_SLOTS)
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
    roster = team_roster_state(page["page_type"], characters)
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
    if roster and not roster["characters_complete"]:
        trace_refresh(owner, "team_roster_incomplete", read=roster["team_members_read"],
                      expected=roster["team_members_expected"])
    trace_refresh(owner, "region_query_ready", page=page["page_type"], regions=len(regions),
                  boxes=len(boxes), characters=len(characters),
                  characters_complete=roster.get("characters_complete"),
                  read_seconds=round(time.monotonic() - started, 3))
    return result
