"""Current character-detail acquisition; old geometry never authorizes facts."""
from __future__ import annotations

import math
from io import BytesIO

from PIL import Image, ImageOps

from ..adapters.native_text import _box_value, check_current, read_region
from ..adapters.text_verification import verify_name_regions
from ..runtime.contracts import native_name_confirmed
from ..runtime.rejection_trace import note_compact_read
from ..runtime.screen_facts import extract_screen_facts
from ..runtime.timing_detail import phase
from .layout import detect_characters_in_page
from .scene_preview import scene_preview

# Acquisition only: the unchanged classifier and native name verifier still decide.
_CONTROL_AREAS = ((.035, .02, .18, .085), (.09, .185, .16, .245), (.09, .34, .16, .40))
_NAME_AREA = (.72, .07, .99, .20)
_KEYS = ("left", "top", "right", "bottom")


def _valid(region):
    return (isinstance(region, dict)
        and all(isinstance(region.get(k), (float, int)) and math.isfinite(region[k]) and 0 <= region[k] <= 1 for k in _KEYS)
        and region["left"] < region["right"] and region["top"] < region["bottom"])


def _acquisition(image, layout):
    fallback = [dict(zip(_KEYS, a)) for a in _CONTROL_AREAS]
    controls, names, source = fallback, [], "default"
    if isinstance(layout, dict) and layout.get("source_size") == list(image.size):
        proposed = [p.get("region") for p in layout.get("page_controls", []) if isinstance(p, dict)]
        labels = [p.get("region") for p in layout.get("name_regions", []) if isinstance(p, dict)]
        if 0 < len(proposed) <= 8 and len(labels) <= 8 and all(_valid(p) for p in [*proposed, *labels]):
            # Cached controls optimize acquisition, but cannot remove the
            # current title required by the unchanged scene_preview gate.
            controls, names, source = [fallback[0]], labels, "cached"
            for area in proposed:
                if any(a["left"] <= area["left"] and a["top"] <= area["top"]
                       and a["right"] >= area["right"] and a["bottom"] >= area["bottom"] for a in controls):
                    continue
                controls.append(area)
            if len(controls) > 8:
                controls, source = fallback, "default"  # Keep the existing bound, not an arbitrary cached subset.
    # A prior narrow name box cannot hide another plausible subject in the same header.
    name = dict(zip(_KEYS, _NAME_AREA))
    for area in names:
        name = {"left": min(name["left"], area["left"]), "top": min(name["top"], area["top"]),
                "right": max(name["right"], area["right"]), "bottom": max(name["bottom"], area["bottom"])}
    title = fallback[0]
    covered = any(a["left"] <= title["left"] and a["top"] <= title["top"]
                  and a["right"] >= title["right"] and a["bottom"] >= title["bottom"] for a in controls)
    note_compact_read(crop_source=source, title_covered=covered)
    return [("control", a) for a in controls] + [("name", name)]


def _compact_read(backend, image, layout, cancelled):
    """Read native crops once; map only boxes fully inside ONE pasted crop."""
    chips = []
    for kind, area in _acquisition(image, layout):
        px, py = max(8, round(image.width*.004)), max(6, round(image.height*.004))
        bounds = (max(0, math.floor(area["left"]*image.width)-px), max(0, math.floor(area["top"]*image.height)-py),
                  min(image.width, math.ceil(area["right"]*image.width)+px), min(image.height, math.ceil(area["bottom"]*image.height)+py))
        w, h = bounds[2]-bounds[0], bounds[3]-bounds[1]
        if w > 1400-32 or h > 1500-32:
            return [], [], {"reason": "layout_crop_too_large"}
        chips.append((kind, bounds, w, h))
    # Pick a compact shelf layout, never downsample or join different captures.
    options = []
    minimum = max(c[2] for c in chips)+32
    for width in sorted({minimum, max(minimum, 1024), 1400}):
        if width > 1400:
            continue
        x, y, row_h, used = 16, 16, 0, 0
        positions = []
        for chip in sorted(chips, key=lambda c: -c[3]):
            if x+chip[2]+16 > width:
                x, y, row_h = 16, y+row_h+16, 0
            positions.append((chip,x,y))
            used = max(used,x+chip[2]+16)
            row_h = max(row_h,chip[3])
            x += chip[2]+16
        height = y+row_h+16
        if height <= 1500:
            options.append((used*height,used,height,positions))
    if not options:
        return [], [], {"reason": "layout_atlas_too_large"}
    _, width, height, positions = min(options,key=lambda p:p[0])
    atlas = Image.new("RGB",(width,height),(24,24,24))
    for (_,bounds,_,_),x,y in positions:
        atlas.paste(image.crop(bounds),(x,y))
    check_current(cancelled)
    read = (getattr(backend, "extract_native_crops_with_boxes", None) if image.height >= 1080 else None)
    if callable(read):
        _, values = read(atlas, min_short_side=320 if image.height >= 1440 else 512)
    else:
        _, values = backend.extract_text_with_boxes(atlas)
    check_current(cancelled)
    boxes, names, native_count = [], [], 0
    for value in values:
        native_count += 1
        box = _box_value(value)
        try:
            left,top,right,bottom = [float(box[k]) for k in _KEYS]
        except (KeyError,ValueError,TypeError):
            continue
        if not all(math.isfinite(v) for v in (left,top,right,bottom)) or left >= right or top >= bottom:
            continue
        for (kind,bounds,w,h),x,y in positions:
            if x <= left < right <= x+w and y <= top < bottom <= y+h:
                mapped = {**box,"left":bounds[0]+left-x,"right":bounds[0]+right-x,
                          "top":bounds[1]+top-y,"bottom":bounds[1]+bottom-y}
                boxes.append(mapped)
                if kind == "name":
                    names.append(mapped)
                break
    note_compact_read(native_boxes=native_count, mapped_boxes=len(boxes))
    return boxes,names,{"atlas_size":[width,height],"source_size":list(image.size),"crop_count":len(chips)}


def read_character_detail(backend, payload, catalog, *, fields, cancelled=None, layout_regions=None):
    """Current-frame controls and name crops, then the unchanged name gate."""
    with Image.open(BytesIO(payload)) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
    if image.width*image.height > 16_000_000:
        raise ValueError("Frame exceeds local recognition size limit")
    check_current(cancelled)
    with phase("region_crop_atlas"):
        boxes,names,acquisition = _compact_read(backend,image,layout_regions,cancelled)
    with phase("region_page"):
        preview = scene_preview(boxes,width=image.width,height=image.height)
    failure = ("preview_empty" if preview is None else
               "wrong_page" if preview["page"].get("page_type") != "character_detail" else
               "scene_unverified" if not preview["scene"].get("verified") else
               "no_controls" if not preview["recognition"].get("scene_regions") else None)
    if failure:
        note_compact_read(subcondition=failure)
        return None
    with phase("region_name_confirmation"):
        names,name_check = verify_name_regions(backend,image,names,catalog,cancelled=cancelled,single_line=True)
    with phase("region_name_assignment"):
        actors = detect_characters_in_page(preview["page"],names,catalog,width=image.width,height=image.height)
    actors = [a for a in actors if a.get("identity_scope") == "detail_subject" and native_name_confirmed(a)]
    if len({a["entity_id"] for a in actors}) != 1:
        actors = []
    native = list(names)
    if "attributes" in fields:
        native.extend(read_region(backend,image,dict(zip(_KEYS,(.50,.24,.995,.90))),cancelled=cancelled))
    check_current(cancelled)
    return {**preview,"characters":actors,
            "screen_facts":extract_screen_facts(native,width=image.width,height=image.height,page=preview["page"],catalog=catalog),
            "recognition":{**preview["recognition"],"phase":"region_complete","region_query":True,
                "source_size":list(image.size),"name_verification":name_check,"acquisition":acquisition,
                "text_reader":{"strategy":"current_character_regions","full_frame_covered":False}}}
