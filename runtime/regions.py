"""Query regions derived from same-frame fact evidence, never fixed pixels.

L2a: recognition facts in, normalized regions out.  A plan that needs a field
with no evidence gets no region; callers may retry once on a newer frame and
then fall back to the normal observation path.
"""
from __future__ import annotations

import time

FIELD_FACT_KINDS = {
    "characters": frozenset({"entity_label"}),
    "location": frozenset({"current_location"}),
    "quest": frozenset({"quest_objective", "tracked_quest"}),
    "equipment": frozenset({"equipment_slot", "equipment_level", "main_stat", "sub_stat"}),
}
PAGE_FIELDS = {
    "team_setup": frozenset({"characters"}),
    "exploration_hud": frozenset({"location", "quest"}),
    "relic_detail": frozenset({"equipment"}),
}
CACHE_SECONDS = 30.0


def _union(boxes, padding=.003):
    left = max(0.0, min(float(box["left"]) for box in boxes) - padding)
    top = max(0.0, min(float(box["top"]) for box in boxes) - padding)
    right = min(1.0, max(float(box["right"]) for box in boxes) + padding)
    bottom = min(1.0, max(float(box["bottom"]) for box in boxes) + padding)
    return {"left": left, "top": top, "right": right, "bottom": bottom}


def _page_type(visual):
    page = visual.get("page") or {}
    return page.get("page_type") or visual.get("page_type")


def _regions_for_field(facts, field, *, page=None, visual=None):
    kinds = FIELD_FACT_KINDS.get(field)
    if not kinds:
        return None
    boxes = [proof["region"] for fact in facts if fact.get("kind") in kinds
             for proof in fact.get("evidence") or [] if proof.get("region")]
    if page == "team_setup" and field == "characters":
        # Path labels sit BELOW the member names. Their union alone cuts the
        # name row short (the topmost name can be ~30px above the first path
        # label), so include the same-frame name boxes too. The result spans
        # every displayed member position, not only the names OCR recognized.
        # The first slot sits close to the crop edge; leave a wider margin so
        # frame jitter cannot clip its first glyph.
        boxes += [item["region"] for item in (visual or {}).get("characters") or []
                  if isinstance(item, dict) and item.get("region")
                  and item.get("identity_scope") in {None, "team_selection", "hud_label"}]
        return _union(boxes, padding=.01) if boxes else None
    return _union(boxes) if boxes else None


def region_plan(visual, plan):
    page = _page_type(visual)
    if page not in PAGE_FIELDS:
        return None
    fields = set(getattr(plan, "fields", ()) or ()) & PAGE_FIELDS[page]
    facts = (visual.get("screen_facts") or {}).get("facts") or []
    regions = []
    for field in sorted(fields):
        region = _regions_for_field(facts, field, page=page, visual=visual)
        if region is not None:
            regions.append({"fields": [field], "region": region, "source": "facts"})
    return {"page_type": page, "regions": regions} if regions else None


def remember_region_plan(owner, plan):
    if not plan or not plan.get("regions"):
        return
    owner._region_cache = {"page_type": plan["page_type"], "regions": plan["regions"],
                           "at": time.monotonic()}


def cached_region_plan(owner, visual, plan, max_age=CACHE_SECONDS):
    cache = getattr(owner, "_region_cache", None)
    if not cache or time.monotonic() - cache["at"] > max_age:
        return None
    page = _page_type(visual)
    if page not in PAGE_FIELDS or cache["page_type"] != page:
        return None
    fields = set(getattr(plan, "fields", ()) or ()) & PAGE_FIELDS[page]
    regions = [item for item in cache["regions"]
               if set(item.get("fields") or ()) & fields]
    return {"page_type": page, "regions": regions, "source": "cache"} if regions else None
