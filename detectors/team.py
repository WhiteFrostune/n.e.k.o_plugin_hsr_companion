"""Read-only HUD row geometry; hotkeys identify positions, not characters.

Inspired by StarRailOneDragon's per-position name/avatar separation (GPL-3.0).
No fuzzy best-name choice, controller, operation graph or game input is used.
"""

from __future__ import annotations

from typing import Any

# Team-setup cards are a page layout, not a full-screen crop. Each slot owns a
# card band; an empty slot is flat background (no portrait, name plate or art).
TEAM_SLOT_CARDS = (
    {"slot": 1, "region": {"left": .015, "top": .59, "right": .145, "bottom": .83}},
    {"slot": 2, "region": {"left": .27, "top": .59, "right": .40, "bottom": .83}},
    {"slot": 3, "region": {"left": .55, "top": .61, "right": .69, "bottom": .85}},
    {"slot": 4, "region": {"left": .75, "top": .64, "right": .93, "bottom": .86}},
)


def slot_card_states(image, slots=None) -> list[dict[str, Any]]:
    """Classify each team card as filled / empty / unknown from its own pixels.

    Filled cards carry a portrait, a name plate and level text; an empty slot
    in the live 3-person layout is flat background (std ~17, edge fraction
    ~.012) against filled cards (std ~57-71, edge ~.077-.117). Keep a wide
    middle band as unknown rather than guessing.
    """
    from PIL import ImageFilter, ImageStat

    result = []
    for slot in slots or TEAM_SLOT_CARDS:
        region = slot["region"]
        bounds = (
            max(0, min(image.width, round(float(region["left"]) * image.width))),
            max(0, min(image.height, round(float(region["top"]) * image.height))),
            max(0, min(image.width, round(float(region["right"]) * image.width))),
            max(0, min(image.height, round(float(region["bottom"]) * image.height))),
        )
        crop = image.crop(bounds).convert("L")
        edges = crop.filter(ImageFilter.FIND_EDGES)
        values = list(edges.getdata())
        # FIND_EDGES sees the crop boundary as an artificial edge on an
        # otherwise flat card. Judge only the inner core; the border is not
        # content and would label every empty slot as filled.
        width, height = edges.size
        margin_x = max(2, round(width * .04))
        margin_y = max(2, round(height * .04))
        core = [values[y * width + x]
                for y in range(margin_y, max(margin_y, height - margin_y))
                for x in range(margin_x, max(margin_x, width - margin_x))]
        edge_fraction = sum(value > 40 for value in core) / max(1, len(core))
        deviation = float(ImageStat.Stat(crop).stddev[0])
        if edge_fraction < .03 and deviation < 32:
            state = "empty"
        elif edge_fraction >= .04 or deviation >= 40:
            state = "filled"
        else:
            state = "unknown"
        result.append({"slot": int(slot["slot"]), "state": state, "region": dict(region),
                       "edge_fraction": round(edge_fraction, 4), "deviation": round(deviation, 1)})
    return result


def assign_slot_names(slots, named_boxes, *, max_distance=.08) -> list[dict[str, Any]]:
    """Bind an OCR name to a filled card by horizontal position, one per slot.

    A filled card without a bound name is `filled_unread`; an empty slot is not
    a missing member. Names outside every card band stay unassigned and must
    not turn into a roster row.
    """
    result = []
    used = set()
    for slot in slots:
        state = slot.get("state")
        if state != "filled":
            result.append({**slot, "state": "unknown" if state == "unknown" else state})
            continue
        center = (float(slot["region"]["left"]) + float(slot["region"]["right"])) / 2
        candidates = []
        for item in named_boxes or []:
            entity_id = item.get("entity_id")
            region = item.get("region") or {}
            if not entity_id or entity_id in used or not region:
                continue
            name_center = (float(region.get("left") or 0) + float(region.get("right") or 0)) / 2
            if abs(name_center - center) <= max_distance:
                candidates.append((abs(name_center - center), item))
        if candidates:
            _, item = min(candidates, key=lambda pair: pair[0])
            used.add(item["entity_id"])
            result.append({**slot, "state": "filled_named",
                           "entity_id": item["entity_id"], "name": item.get("name")})
        else:
            result.append({**slot, "state": "filled_unread"})
    return result


def locate_team_rows(boxes, *, width: int, height: int) -> list[float]:
    anchors: dict[int, float] = {}
    for box in boxes:
        text = str(box.get("text") or "").strip()
        if text not in {"1", "2", "3", "4"} or float(box.get("score") or 0) < 0.7:
            continue
        x = (float(box["left"]) + float(box["right"])) / 2
        y = (float(box["top"]) + float(box["bottom"])) / 2
        if x < width * 0.955 or not height * 0.15 <= y <= height * 0.78:
            continue
        anchors[int(text)] = y
    if len(anchors) < 3:
        return []
    first, last = min(anchors), max(anchors)
    step = (anchors[last] - anchors[first]) / (last - first)
    if not height * 0.055 <= step <= height * 0.16:
        return []
    start = anchors[first] - (first - 1) * step
    rows = [start + i * step for i in range(4)]
    if not height * 0.12 < rows[0] < rows[-1] < height * 0.82:
        return []
    if any(abs(rows[slot - 1] - y) > step * 0.18 for slot, y in anchors.items()):
        return []
    return rows


def assign_team_slots(characters: list[dict[str, Any]], rows: list[float], *, height: int):
    result = [dict(item) for item in characters]
    if len(rows) != 4:
        return result
    spacing = (rows[-1] - rows[0]) / 3
    for item in result:
        if item.get("identity_scope") not in {"hud_label", "hud_portrait"}:
            continue
        region = item.get("region") or {}
        center = (float(region.get("top") or 0) + float(region.get("bottom") or 0)) * height / 2
        index = min(range(4), key=lambda i: abs(rows[i] - center))
        if abs(rows[index] - center) <= spacing * 0.4:
            item["slot"] = index + 1
    # Two identities in one position are a conflict, not two team members.
    conflicting = {
        slot for slot in range(1, 5) if len({item["entity_id"] for item in result if item.get("slot") == slot}) > 1
    }
    return [item for item in result if item.get("slot") not in conflicting]


def infer_complete_team_rows(characters: list[dict[str, Any]], *, height: int) -> list[float]:
    """Four independently recognized regular HUD rows establish their order.

    Do not sort a partial team into positions: missing slot 1 is not slot 2.
    """
    hud = [
        item
        for item in characters
        if item.get("identity_scope") in {"hud_label", "hud_portrait"} and item.get("region", {}).get("left", 0) >= 0.82
    ]
    if len(hud) != 4 or len({item.get("entity_id") for item in hud}) != 4:
        return []
    rows = sorted((item["region"]["top"] + item["region"]["bottom"]) * height / 2 for item in hud)
    gaps = [rows[i + 1] - rows[i] for i in range(3)]
    if min(gaps) < height * 0.055 or max(gaps) > height * 0.16 or max(gaps) / min(gaps) > 1.6:
        return []
    return rows
