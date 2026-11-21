"""Exact, region-local rereads; a pixel mismatch is not a semantic change.

No fuzzy aliases or catalogue guesses. Reuse the existing OCR backend, serially
inside the observation producer. Nothing is persisted and no game input occurs.
"""
from __future__ import annotations

import math
import re
import time
import unicodedata
from io import BytesIO

from ..detectors.exploration import generic_navigation
from ..detectors.relic_geometry import boundary_label
from ..detectors.relic_labels import label_key
from .native_text import ObservationSuperseded, _box_value, check_current

NUMBER_LITERAL = re.compile(r"\+?\d[\d,]*(?:\.\d+)?%?")

def text_key(text, *, control=False):
    value = unicodedata.normalize("NFKC", str(text or ""))
    value = re.sub(r"\s+", "", value)
    # A navigation button's distance is not evidence of a different quest.
    # Only proof controls use this rule, never literal text returned to users.
    if control and generic_navigation(value):
        return "前往任务目标"
    if control and re.fullmatch(r"请前往\S{1,100}", value):
        return "导航目的地提示"
    if control and re.fullmatch(r"状态效果[Oo0]?", value):
        return "状态效果"  # Adjacent keyboard O is not a semantic field value.
    if control and boundary_label(value):
        return "套装效果"
    return value


def proof_key(proof):
    region = proof.get("region") or {}
    stat_label = proof.get("validation_scope") == "relic_stat_label"
    return (tuple(region.get(k, -1) for k in ("left", "top", "right", "bottom")),
            label_key(proof.get("text")) if stat_label else text_key(proof.get("text"), control=proof.get("control") is True),
            "stat_label" if stat_label else "control" if proof.get("control") is True else "literal")


def identity_literal(actor):
    """A label proves a text identity, never a changing portrait or guessed name.

    Reread the actual OCR line (including level/aliases), not canonical name
    search. Keep this internal and bound to the actor's own capture geometry.
    """
    text = actor.get("raw_label")
    if (actor.get("identity_scope") == "hud_portrait" or not actor.get("region")
            or not isinstance(text, str) or not text.strip()):
        return None
    return {"region": actor["region"], "text": text, "identity_literal": True}


def verify_text_regions(backend, payload, proofs, *, cancelled=None, trace=None):
    """Return keys with high-quality, exact rereads at the expected geometry.

    Two crop scales must agree. OCR on the same image is not
    two independent frames; this verifies text against the NEW captured frame.
    Work is bounded; unverified/overflow proofs remain unknown.
    """
    from PIL import Image

    confirmed = set()
    with Image.open(BytesIO(payload)) as opened:
        image = opened.convert("RGB")
    for proof in proofs[:16]:
        check_current(cancelled)
        coords, expected = proof_key(proof)[:2]
        def skipped(reason):
            if trace is not None:
                trace.append({"stage": "proof_skipped", "key": proof_key(proof), "reason": reason})
        if not expected or "已隐藏]" in expected or len(expected) > 240:
            skipped("invalid_expected_text")
            continue
        if not all(isinstance(v, (float, int)) and math.isfinite(v) and 0 <= v <= 1 for v in coords):
            skipped("invalid_coordinates")
            continue
        left, top, right, bottom = coords
        if left >= right or top >= bottom:
            skipped("empty_geometry")
            continue
        bounds = (max(0, math.floor(left * image.width) - 5), max(0, math.floor(top * image.height) - 4),
                  min(image.width, math.ceil(right * image.width) + 5), min(image.height, math.ceil(bottom * image.height) + 4))
        crop = image.crop(bounds)
        if crop.width < 4 or crop.height < 4 or crop.width > 1400 or crop.height > 160:
            skipped("crop_size_limit")
            continue
        matches = 0
        contradicted = False
        # Tiny HUD crops need a surrounding canvas for text detection; feeding
        # a 20px-high strip makes some OCR detectors resize/segment it badly.
        for scale in (2, 3, 4):
            check_current(cancelled)
            resized = crop.resize((crop.width * scale, crop.height * scale), Image.Resampling.BICUBIC)
            sample = Image.new("RGB", (resized.width + 32, resized.height + 32), (24, 24, 24))
            sample.paste(resized, (16, 16))
            line_reader = getattr(backend, "recognize_line", None)
            if callable(line_reader):
                lines = line_reader(resized)
                boxes = [dict(text=t, score=s, left=0, top=0, bottom=resized.height) for t, s in lines]
            else:
                _, values = backend.extract_text_with_boxes(sample)
                boxes = [_box_value(b) for b in values]
            # Missing/weak boxes cannot be silently dropped to manufacture a match.
            try:
                minimum = .85 if proof.get("control") is True else .9
                valid = bool(boxes) and all(math.isfinite(float(b.get("score") or 0))
                    and float(b.get("score") or 0) >= minimum for b in boxes)
                boxes.sort(key=lambda b: float(b.get("left") or 0))
                centers = [(float(b.get("top") or 0) + float(b.get("bottom") or 0)) / 2 for b in boxes]
                valid &= max(centers, default=0) - min(centers, default=0) <= sample.height * .4
                actual_text = "".join(str(b.get("text") or "") for b in boxes)
                actual = (label_key(actual_text) if proof.get("validation_scope") == "relic_stat_label"
                          else text_key(actual_text, control=proof.get("control") is True))
            except (ValueError, TypeError):
                valid, actual, actual_text = False, "", ""
            quality_valid = bool(valid)
            numeric_syntax_valid = not NUMBER_LITERAL.fullmatch(expected) or bool(NUMBER_LITERAL.fullmatch(actual))
            valid = valid and numeric_syntax_valid
            if trace is not None:
                trace.append({"stage": "reread_attempt", "key": proof_key(proof),
                              "expected": expected, "actual": actual, "scale": scale, "valid": bool(valid),
                              "actual_raw": actual_text,
                              "quality_valid": quality_valid, "numeric_syntax_valid": numeric_syntax_valid,
                              "bounds": bounds, "image_size": image.size, "sample_size": sample.size,
                              "scores": [round(float(b.get("score") or 0), 3) for b in boxes]})
            if valid and actual != expected:
                contradicted = True
                break
            if valid:
                matches += 1
                if matches == 2:
                    break
        if matches == 2 and not contradicted:
            confirmed.add(proof_key(proof))
        if trace is not None:
            trace.append({"stage": "proof_decision", "key": proof_key(proof), "matches": matches,
                          "reason": "exact_twice" if matches == 2 and not contradicted else
                                    "high_confidence_contradiction" if contradicted else "insufficient_exact_matches"})
    return confirmed


def verify_name_regions(backend, image, boxes, catalog, *, cancelled=None, read=None):
    """Re-read canonical label pixels, not a second expensive full screenshot.

    Both reads must agree on a complete canonical name at high confidence.
    This validates printed text only, not 3D identity or scene continuity;
    live_refresh still checks a later independent capture before handoff.
    Merged rows, aliases, prose and uncertain readings keep the multi-frame gate.
    """
    from PIL import Image

    from ..data import normalize_lookup_text
    from ..detectors.names import label_fields_only, read_name_label

    candidates = []
    for box in boxes:
        records, _ = read_name_label(str(box.get("text") or ""), catalog)
        record = records[0] if len(records) == 1 else None
        if (record and float(box.get("score") or 0) >= .90
                and normalize_lookup_text(record["name"]) in normalize_lookup_text(box["text"])):
            left, top = max(0, math.floor(box["left"] - 8)), max(0, math.floor(box["top"] - 6))
            right, bottom = min(image.width, math.ceil(box["right"] + 8)), min(image.height, math.ceil(box["bottom"] + 6))
            if right > left and bottom > top:
                candidates.append((box, record, (left, top, right, bottom)))
    stats = {"attempted": len(candidates), "confirmed": 0, "batches": 0, "seconds": 0.0,
             "rejected": 0}
    if not candidates:
        return boxes, stats
    started = time.monotonic()
    # Bounded atlases retain native pixels or enlarge them, including English
    # collab names. A long label must never trigger the backend's downsampling.
    pending = list(candidates[:32])
    while pending:
        check_current(cancelled)
        batch, y = [], 24
        while pending:
            candidate = pending[0]
            left, top, right, bottom = candidate[2]
            scale = min(2.0, 1400 / (right - left))
            if scale < 1:
                pending.pop(0)
                continue
            h = math.ceil((bottom - top) * scale)
            if batch and y + h + 24 > 1500:
                break
            pending.pop(0)
            batch.append((*candidate, scale, y, h))
            y += h + 24
        if not batch:
            break
        atlas = Image.new("RGB", (max(math.ceil((b[2][2] - b[2][0]) * b[3]) for b in batch) + 48, y), "black")
        for _, _, bounds, scale, atlas_y, h in batch:
            crop = image.crop(bounds)
            atlas.paste(crop.resize((math.ceil(crop.width * scale), h), Image.Resampling.LANCZOS), (24, atlas_y))
        try:
            _, values = read(atlas, "name_verification") if read else backend.extract_text_with_boxes(atlas)
            check_current(cancelled)
        except ObservationSuperseded:
            raise
        except Exception:
            continue  # No corroboration: ordinary multi-frame confirmation.
        stats["batches"] += 1
        for original, record, _, _, atlas_y, h in batch:
            lines = [_box_value(value) for value in values
                     if atlas_y <= (float(_box_value(value).get("top") or 0) + float(_box_value(value).get("bottom") or 0)) / 2 < atlas_y + h]
            # One complete name can be accompanied by a separate level/path
            # line. Never join split fragments to manufacture a name.
            lines = [line for line in lines if str(line.get("text") or "").strip()]
            name_lines = []
            fields_valid = True
            for line in lines:
                reread, _ = read_name_label(str(line.get("text") or ""), catalog)
                if (len(reread) == 1 and reread[0]["id"] == record["id"]
                        and float(line.get("score") or 0) >= .90
                        and normalize_lookup_text(record["name"]) in normalize_lookup_text(line["text"])):
                    name_lines.append(line)
                elif not label_fields_only(line["text"]):
                    fields_valid = False
            if len(name_lines) == 1 and fields_valid:
                original["name_validation"] = {
                    "entity_id": record["id"], "method": "native_and_enlarged_label_agree",
                    "scores": [float(original["score"]), float(name_lines[0]["score"])],
                }
                stats["confirmed"] += 1
            else:
                stats["rejected"] += 1
    stats["seconds"] = round(time.monotonic() - started, 3)
    return boxes, stats
