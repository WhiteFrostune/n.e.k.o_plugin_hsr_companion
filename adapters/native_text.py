"""Native-pixel OCR coverage independent of game page and character roster."""

from __future__ import annotations

import math
import re
import time
from functools import partial
from typing import Any


class ObservationSuperseded(Exception):
    """Cooperative exit between OCR calls; never a recognition failure."""


class QueryPreempted(ObservationSuperseded):
    """A waiting query gets the next native-call boundary."""


def check_current(cancelled=None):
    if cancelled is not None and cancelled():
        raise ObservationSuperseded()


def _box_value(box: Any) -> dict[str, Any]:
    keys = ("text", "score", "left", "top", "right", "bottom")
    return dict(box) if isinstance(box, dict) else {key: getattr(box, key, None) for key in keys}


def native_tiles(width: int, height: int) -> list[tuple[int, int, int, int]]:
    """Cover every pixel with overlap; never trigger shared OCR downsampling."""
    columns, rows = math.ceil(width / 1400), math.ceil(height / 1400)
    tiles = []
    for row in range(rows):
        for column in range(columns):
            tiles.append(
                (
                    max(0, round(column * width / columns) - 64),
                    max(0, round(row * height / rows) - 64),
                    min(width, round((column + 1) * width / columns) + 64),
                    min(height, round((row + 1) * height / rows) + 64),
                )
            )
    if len(tiles) > 16:
        raise ValueError("画面比例超出本地文字识别范围")
    return tiles


def _deduplicate(boxes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    accepted = []
    for box in sorted(boxes, key=lambda item: -float(item.get("score") or 0)):
        if not str(box.get("text") or "").strip():
            continue
        cx, cy = (box["left"] + box["right"]) / 2, (box["top"] + box["bottom"]) / 2
        if any(
            other["text"] == box["text"]
            and abs((other["left"] + other["right"]) / 2 - cx) < max(12, (box["right"] - box["left"]) * 0.25)
            and abs((other["top"] + other["bottom"]) / 2 - cy) < max(8, (box["bottom"] - box["top"]) * 0.5)
            for other in accepted
        ):
            continue
        accepted.append(box)
    return sorted(accepted, key=lambda item: (item["top"], item["left"]))


def split_label_geometry(image, box):
    """Split a detected wide text row at actual blank columns, not name length.

    Return no split if ink/background does not establish clear whitespace.
    No roster, fixed HUD coordinates or character substring offsets involved.
    Original OCR stays available; these regions are only retry pixel crops.
    """
    left, top = max(0, math.floor(box["left"])), max(0, math.floor(box["top"]))
    right, bottom = min(image.width, math.ceil(box["right"])), min(image.height, math.ceil(box["bottom"]))
    height = bottom - top
    if height < 4 or right - left < height * 8:
        return []
    crop = image.crop((left, top, right, bottom)).convert("RGB")
    pixels = crop.load()
    ink = []
    for x in range(crop.width):
        count = 0
        for y in range(crop.height):
            r, g, b = pixels[x, y]
            if min(r, g, b) >= 180 and max(r, g, b) - min(r, g, b) <= 60:
                count += 1
        ink.append(count >= max(2, round(height * .07)))
    gap = max(12, round(height * .7))
    occupied = [i for i, present in enumerate(ink) if present]
    if not occupied:
        return []
    intervals, start, previous = [], occupied[0], occupied[0]
    for x in occupied[1:]:
        if x - previous > gap:
            intervals.append((start, previous + 1))
            start = x
        previous = x
    intervals.append((start, previous + 1))
    if len(intervals) < 2:
        return []
    return [(max(0, left + a - 8), max(0, top - 6), min(image.width, left + b + 8), min(image.height, bottom + 6))
            for a, b in intervals if b - a >= height * .5]


def read_native_text(
    backend: Any, image: Any, *, catalog: Any = None, cancelled=None, on_progress=None,
    verify_names: bool = False, accept_native_relic=None, relic_tiles=None, yield_requested=None,
    name_labels=None, skip_name_reread=None, name_verifier=None, split_background=False,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """Uniform native tiles, then one bounded reread of wide merged OCR rows."""
    if catalog is not None and not all(callable(callback) for callback in (name_labels, skip_name_reread, name_verifier)):
        raise ValueError("catalog requires all three text-reading policy callbacks")
    started = time.monotonic()
    boxes, failures = [], []
    tiles = native_tiles(*image.size)
    completed = 0
    relic_priority = False
    scene_probe_seconds = None
    calls = []

    def try_priority():
        nonlocal relic_priority
        if accept_native_relic is None or relic_priority:
            return
        try:
            relic_priority = bool(accept_native_relic(boxes))
        except Exception:
            relic_priority = False

    def read(sample, stage, extract=None):
        check_current(cancelled)
        if yield_requested is not None and yield_requested():
            raise QueryPreempted()
        call_started = time.monotonic()
        try:
            return (extract or backend.extract_text_with_boxes)(sample)
        finally:
            # Includes runtime initialization and shared-backend waiting. Do
            # not mislabel this as pure model inference time.
            calls.append({"stage": stage, "size": list(sample.size),
                          "backend_call_seconds": round(time.monotonic() - call_started, 3)})
    def background_read(sample, stage):
        if not split_background:
            return read(sample, stage)
        extract = getattr(backend, "extract_native_crops_with_boxes", None)

        scene_probe = stage == "scene_probe"
        if scene_probe and callable(extract):
            extract = partial(extract, min_short_side=320)

        def spans(length, limit):
            if length <= limit:
                return [(0, length)]
            midpoint = length // 2
            return [(0, midpoint + 64), (midpoint - 64, length)]

        horizontal = spans(sample.width, 680)
        vertical = spans(sample.height, 420 if scene_probe else 680)
        if len(horizontal) == len(vertical) == 1:
            return read(sample, stage, extract)
        # Whole-source coverage at the original input scale. Translating the
        # boxes before deduplication preserves the existing proof coordinates.
        boxes = []
        for top, bottom in vertical:
            for left, right in horizontal:
                _, values = read(sample.crop((left, top, right, bottom)), stage + "_part", extract)
                for value in values:
                    box = _box_value(value)
                    boxes.append({**box, "left": left + float(box.get("left") or 0),
                                  "right": left + float(box.get("right") or 0),
                                  "top": top + float(box.get("top") or 0),
                                  "bottom": top + float(box.get("bottom") or 0)})
        return "", _deduplicate(boxes)

    # One reduced full-frame pass reads large UI headings/controls, not names.
    # It covers pages with custom team titles too. Native identity coverage is
    # unchanged and never inherits names from this lower-resolution probe.
    if on_progress is not None:
        check_current(cancelled)
        try:
            probe = image.copy()
            probe.thumbnail((768, 768))
            _, values = background_read(probe, "scene_probe")
            check_current(cancelled)
            sx, sy = image.width / probe.width, image.height / probe.height
            scaled = []
            for value in values:
                box = _box_value(value)
                scaled.append({**box, **{key: float(box.get(key) or 0) * scale for key, scale in (
                    ("left", sx), ("right", sx), ("top", sy), ("bottom", sy),
                )}})
            on_progress(scaled)
            scene_probe_seconds = round(time.monotonic() - started, 3)
        except ObservationSuperseded:
            raise
        except Exception as exc:
            failures.append("scene_probe_" + type(exc).__name__)
    preferred = []
    if relic_tiles is not None:
        try:
            preferred = list(dict.fromkeys(
                index for index in (relic_tiles(tiles) or [])
                if isinstance(index, int) and 0 <= index < len(tiles)))
        except Exception:
            preferred = []
    preferred_set = set(preferred)
    order = [*preferred, *(index for index in range(len(tiles)) if index not in preferred_set)]
    for index in order:
        left, top, right, bottom = tiles[index]
        check_current(cancelled)
        if time.monotonic() - started > 22:
            failures.append("ocr_time_budget")
            break
        try:
            _, values = background_read(image.crop((left, top, right, bottom)), "native_tile")
            check_current(cancelled)
            completed += 1
            for value in values:
                box = _box_value(value)
                boxes.append(
                    {
                        **box,
                        "left": left + float(box.get("left") or 0),
                        "right": left + float(box.get("right") or 0),
                        "top": top + float(box.get("top") or 0),
                        "bottom": top + float(box.get("bottom") or 0),
                    }
                )
        except ObservationSuperseded:
            raise
        except Exception as exc:
            failures.append(type(exc).__name__)
        if on_progress is not None:
            on_progress(_deduplicate(boxes))
        try_priority()
        if relic_priority:
            break
    if not completed:
        raise RuntimeError("文字识别未完成：" + ",".join(failures[:3]))
    try_priority()
    boxes = _deduplicate(boxes)
    rereads = 0
    icon_rereads = 0
    # Re-read detected text geometry, not hard-coded exploration coordinates.
    # Wide lines commonly merge several character nameplates with their fields.
    try_priority()
    if not relic_priority and time.monotonic() - started < 18:
        check_current(cancelled)
        if yield_requested is not None and yield_requested():
            raise QueryPreempted()
        from PIL import Image, ImageOps

        parts = []
        icon_parts = set()
        color_parts = set()
        if catalog is not None:
            # A one-letter icon/shortcut may merge with a registered name. Do
            # not strip it into a fact: re-read the remaining original pixels.
            # This is shared by all names, not an alias for one collab role.
            for box in boxes:
                raw = str(box["text"]).strip()
                if not re.match(r"^[A-Za-z]\s*[\u3400-\u9fff]", raw):
                    continue
                records = name_labels(raw[1:])
                if not records:
                    continue
                width = box["right"] - box["left"]
                h = max(1, box["bottom"] - box["top"])
                # Chinese glyphs are approximately twice a Latin/icon cell;
                # only the retry crop uses this estimate. Accepted coordinates
                # still come from its fresh OCR detector.
                cut = width / max(3, 2 * len(raw[1:].strip()) + 1)
                part = (
                    max(0, math.floor(box["left"] + cut)),
                    max(0, math.floor(box["top"] - h * 0.1)),
                    min(image.width, math.ceil(box["right"] + 4)),
                    min(image.height, math.ceil(box["bottom"] + h * 0.1)),
                )
                if part[2] > part[0] and part[3] > part[1] and len(parts) < 4:
                    parts.append(part)
                    icon_parts.add(part)
        for box in sorted(boxes, key=lambda value: (len(str(value["text"])) > 20, -float(value.get("score") or 0))):
            # A complete high-confidence registered name already has native
            # evidence. Spend the extra read on unresolved/merged text instead.
            # Icon-prefixed and low-score labels are still retried above/below.
            if catalog is not None and skip_name_reread(box):
                continue
            segments = split_label_geometry(image, box)
            if segments:
                for part in segments:
                    if len(parts) < 12:
                        parts.append(part)
                        color_parts.add(part)
                continue
            h = max(1, box["bottom"] - box["top"])
            wide = box["right"] - box["left"] >= max(300, h * 12)
            short_cjk = 1 <= len(str(box["text"])) <= 20 and re.search(r"[\u3400-\u9fff]", str(box["text"]))
            if not wide and not short_cjk:
                continue
            top, bottom = max(0, int(box["top"] - h * 0.3)), min(image.height, math.ceil(box["bottom"] + h * 0.3))
            span = max(240, min(700, round(h * 10)))
            for left in range(
                max(0, int(box["left"] - 12)), min(image.width, math.ceil(box["right"])), max(1, span - 64)
            ):
                right = min(image.width, math.ceil(box["right"] + 12), left + span)
                if right - left > 32 and len(parts) < 12:
                    parts.append((left, top, right, bottom))
        if parts:
            scale, padding = 2, 24
            width = max((right - left) * scale for left, _, right, _ in parts) + padding * 2
            height = sum((bottom - top) * scale + padding for _, top, _, bottom in parts) + padding
            # Keep all samples native or larger, never let an oversized atlas
            # shrink back down inside the shared engine.
            while height > 1550 and parts:
                parts.pop()
                height = sum((bottom - top) * scale + padding for _, top, _, bottom in parts) + padding
            if parts:
                atlas = Image.new("RGB", (width, height), "black")
                positions = []
                y = padding
                for left, top, right, bottom in parts:
                    crop = image.crop((left, top, right, bottom)).resize(
                        ((right - left) * scale, (bottom - top) * scale), Image.Resampling.LANCZOS
                    )
                    # Additional high-contrast reading of detected text, not a
                    # replacement of the native colour sample. The original
                    # tile remains authoritative when thresholding loses ink.
                    if (left, top, right, bottom) not in icon_parts | color_parts:
                        crop = ImageOps.grayscale(crop).point(lambda pixel: 255 if pixel > 175 else 0).convert("RGB")
                    atlas.paste(crop, (padding, y))
                    positions.append((left, top, right, bottom, y))
                    y += crop.height + padding
                try:
                    check_current(cancelled)
                    _, values = read(atlas, "unresolved_lines")
                    check_current(cancelled)
                    rereads = len(parts)
                    icon_rereads = sum(part in icon_parts for part in parts)
                    for value in values:
                        box = _box_value(value)
                        cy = (float(box["top"]) + float(box["bottom"])) / 2
                        for left, top, right, bottom, atlas_y in positions:
                            if atlas_y <= cy < atlas_y + (bottom - top) * scale:
                                boxes.append(
                                    {
                                        **box,
                                        "left": max(left, left + (float(box["left"]) - padding) / scale),
                                        "right": min(right, left + (float(box["right"]) - padding) / scale),
                                        "top": max(top, top + (float(box["top"]) - atlas_y) / scale),
                                        "bottom": min(bottom, top + (float(box["bottom"]) - atlas_y) / scale),
                                    }
                                )
                                break
                except ObservationSuperseded:
                    raise
                except Exception as exc:
                    failures.append("reread_" + type(exc).__name__)
    check_current(cancelled)
    if yield_requested is not None and yield_requested():
        raise QueryPreempted()
    try_priority()
    boxes = _deduplicate(boxes)
    verification = {"attempted": 0, "confirmed": 0}
    if not relic_priority and verify_names and catalog is not None:
        boxes, verification = name_verifier(backend, image, boxes, catalog, cancelled=cancelled, read=read)
    try_priority()
    return (
        "\n".join(str(box["text"]) for box in boxes),
        boxes,
        {
            "strategy": "native_overlapping_tiles_and_detected_lines",
            "tiles_total": len(tiles),
            "tiles_completed": completed,
            "full_frame_covered": completed == len(tiles),
            "relic_priority": relic_priority,
            "tiles_skipped": len(tiles) - completed,
            "priority_tiles": preferred,
            "tile_order": order,
            "line_rereads": rereads,
            "icon_name_rereads": icon_rereads,
            "scene_probe_seconds": scene_probe_seconds,
            "backend_calls": calls,
            "name_verification": verification,
            "processing_seconds": round(time.monotonic() - started, 3),
            "errors": failures[:5],
        },
    )


def read_region(backend: Any, image: Any, region: dict, *, cancelled=None, yield_requested=None) -> list[dict[str, Any]]:
    """One native read of one already-derived region, returned in full-frame boxes."""
    check_current(cancelled)
    if yield_requested is not None and yield_requested():
        raise QueryPreempted()
    try:
        left = max(0, int(float(region["left"]) * image.width))
        top = max(0, int(float(region["top"]) * image.height))
        right = min(image.width, int(float(region["right"]) * image.width))
        bottom = min(image.height, int(float(region["bottom"]) * image.height))
    except (KeyError, TypeError, ValueError):
        return []
    if right <= left or bottom <= top:
        return []
    _, values = backend.extract_text_with_boxes(image.crop((left, top, right, bottom)))
    boxes = []
    for value in values:
        box = _box_value(value)
        boxes.append({**box, "left": float(box["left"]) + left, "right": float(box["right"]) + left,
                      "top": float(box["top"]) + top, "bottom": float(box["bottom"]) + top})
    return _deduplicate(boxes)
