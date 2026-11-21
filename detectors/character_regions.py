"""Current character-detail acquisition; no old observations or player names."""
from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageOps

from ..adapters.native_text import _box_value, check_current, read_region
from ..adapters.text_verification import verify_name_regions
from ..runtime.contracts import native_name_confirmed
from ..runtime.screen_facts import extract_screen_facts
from .layout import detect_characters_in_page
from .names import read_name_label
from .scene_preview import scene_preview


def _region(boxes, width, height, fallback):
    if not boxes:
        return dict(zip(("left", "top", "right", "bottom"), fallback))
    return {"left": max(.5, min(b["left"] for b in boxes)/width - .01),
            "top": max(0, min(b["top"] for b in boxes)/height - .015),
            "right": min(1, max(b["right"] for b in boxes)/width + .015),
            "bottom": min(1, max(b["bottom"] for b in boxes)/height + .015)}


def read_character_detail(backend, payload, catalog, *, fields, cancelled=None):
    """Probe this capture, then read native name/stats; never run tile OCR."""
    with Image.open(BytesIO(payload)) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
    if image.width * image.height > 16_000_000:
        raise ValueError("Frame exceeds local recognition size limit")
    check_current(cancelled)
    probe = image.copy()
    probe.thumbnail((768, 768))
    _, values = backend.extract_text_with_boxes(probe)
    check_current(cancelled)
    sx, sy = image.width/probe.width, image.height/probe.height
    boxes = []
    for value in values:
        b = _box_value(value)
        boxes.append({**b, "left": b["left"]*sx, "right": b["right"]*sx,
                      "top": b["top"]*sy, "bottom": b["bottom"]*sy})
    preview = scene_preview(boxes, width=image.width, height=image.height)
    if (preview is None or preview["page"].get("page_type") != "character_detail"
            or not preview["scene"].get("verified") or not preview["recognition"].get("scene_regions")):
        return None
    # Layout geometry only. Native OCR and the normal literal checks decide
    # what is actually present; the crop cannot authorize a name or a number.
    labels = [b for b in boxes if b["left"] >= image.width*.5 and b["bottom"] < image.height*.30
              and len(read_name_label(b["text"], catalog)[0]) == 1]
    area = _region(labels, image.width, image.height, (.50, .04, .99, .25))
    names = read_region(backend, image, area, cancelled=cancelled)
    names, name_check = verify_name_regions(backend, image, names, catalog, cancelled=cancelled)
    actors = detect_characters_in_page(preview["page"], names, catalog, width=image.width, height=image.height)
    actors = [a for a in actors if a.get("identity_scope") == "detail_subject" and native_name_confirmed(a)]
    # More than one plausible subject is ambiguous, even if each name is real.
    if len({a["entity_id"] for a in actors}) != 1:
        actors = []
    native = list(names)
    if "attributes" in fields:
        stat_labels = {"生命值", "攻击力", "防御力", "速度", "暴击率", "暴击伤害"}
        rows = [b for b in boxes if b["left"] >= image.width*.5 and b["text"].strip() in stat_labels]
        area = _region(rows, image.width, image.height, (.50, .24, .99, .90))
        area["right"] = .995  # Include values to the right of the labels.
        native.extend(read_region(backend, image, area, cancelled=cancelled))
    check_current(cancelled)
    return {**preview, "characters": actors,
            "screen_facts": extract_screen_facts(native, width=image.width, height=image.height,
                                                 page=preview["page"], catalog=catalog),
            "recognition": {**preview["recognition"], "phase": "region_complete", "region_query": True,
                "name_verification": name_check,
                "text_reader": {"strategy": "current_character_regions", "full_frame_covered": False}}}
