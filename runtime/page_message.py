"""Extractive notification projection for an already-admitted challenge page.

No recognition, page classification, source lookup or permission changes.
Layout selects quoted rows in ONE selected packet, never new game facts.
"""
from __future__ import annotations

import hashlib
import json
import math


def requires_full(plan):
    from .consultation import explicit_detail
    return plan.response_detail == "full" or explicit_detail(plan.query)


def _bounds(row):
    region = row.get("region") or {}
    try:
        value = tuple(float(region[k]) for k in ("left", "top", "right", "bottom"))
    except (KeyError, TypeError, ValueError):
        return None
    return value if all(math.isfinite(n) and 0 <= n <= 1 for n in value) and value[0] < value[2] and value[1] < value[3] else None


def _same_line(a, b):
    # Only discard an overlapping literal substring, not unrelated goals or
    # different digits. Neither concatenate fragments nor repair OCR spelling.
    _, top_a, _, bottom_a = a[2]
    _, top_b, _, bottom_b = b[2]
    left = max(a[2][0], b[2][0])
    right = min(a[2][2], b[2][2])
    return (min(bottom_a, bottom_b) - max(top_a, top_b) >= min(bottom_a - top_a, bottom_b - top_b) * .5
            and right - left >= min(a[2][2] - a[2][0], b[2][2] - b[2][0]) * .5)


def _whole_rows(rows):
    chosen = []
    for row in rows:
        text = row[1]["text"]
        if any(text != other[1]["text"] and text in other[1]["text"] and _same_line(row, other) for other in rows):
            continue
        if not any(text == other[1]["text"] and _same_line(row, other) for other in chosen):
            chosen.append(row)
    return sorted(chosen, key=lambda r: (r[2][1], r[2][0], r[0]))


def summarize_page(data, *, plan, preview):
    """Return a fully formatted minimum summary, or None for the strict path.

    This is only the overflow projection for ordinary page explanation. Mixed
    requests, explicit full text, equipment and account-team advice stay on
    their original paths. Missing/ambiguous anchors never earn a fake success.
    """
    if (plan.fields != {"page", "visible_text"} or requires_full(plan)
            or data.get("page_type") != "challenge_setup" or not (data.get("scene") or {}).get("verified")
            or (data.get("guidance_answer") or {}).get("requested")):
        return None
    source = (data.get("screen_facts") or {}).get("visible_text") or []
    rows = [(i, r, _bounds(r)) for i, r in enumerate(source)
            if isinstance(r.get("text"), str) and r["text"] and not r.get("literal_unconfirmed")]
    rows = [r for r in rows if r[2] is not None]
    anchors = _whole_rows([r for r in rows if r[1]["text"].strip() == "挑战目标"])
    if len(anchors) != 1:
        return None
    anchor = anchors[0]
    same_column = [r for r in rows if abs(r[2][0] - anchor[2][0]) <= .12]
    above = _whole_rows([r for r in same_column if r[2][3] <= anchor[2][1]
                         and r[1].get("role") == "heading_candidate" and len(r[1]["text"]) >= 6])
    if not above:
        return None
    first_line = [r for r in above if _same_line(r, above[0])]
    if len(first_line) != 1:
        return None
    title = first_line[0]
    goals = _whole_rows([r for r in same_column if anchor[2][3] <= r[2][1]
                         and r[2][3] <= anchor[2][3] + .25 and len(r[1]["text"]) >= 6])[:3]
    if not goals:
        return None
    # Keep the actual literals, with quoted OCR as data rather than policy.
    def quote(value):
        return json.dumps(value, ensure_ascii=False)

    def fits(value):
        try:
            return preview(value) == value
        except Exception:
            return False
    lines = ["星铁同题补答（画面摘要，非全文；引文不是指令）：",
             "页面：" + quote((data.get("scene") or {}).get("label") or data["page_type"]),
             "关卡：" + quote(title[1]["text"]),
             "挑战目标：" + quote([r[1]["text"] for r in goals])]
    text = "\n".join(lines)
    if not fits(text):
        return None  # Never silently shrink the agreed minimum.
    included = {title[0], anchor[0], *(r[0] for r in goals)}
    carried = ["page", "stage_title", "objectives"]
    mechanics = _whole_rows([r for r in same_column if title[2][3] <= r[2][1]
                             and r[2][3] <= anchor[2][1] and len(r[1]["text"]) >= 6])
    for row in mechanics:
        candidate = text + "\n机制原文：" + quote(row[1]["text"])
        if fits(candidate):
            text = candidate
            included.add(row[0])
            if "mechanics" not in carried:
                carried.append("mechanics")
    ready = (data.get("answer_progress") or {}).get("complete", True) is True
    delivered = len(included) + 1  # Independent confirmed page plus quoted rows.
    return text, {"mode": "page_summary", "complete": True, "has_content": True,
                  "answer_complete": ready, "service_satisfied": ready, "requested_detail": "normal",
                  "all_input_rows_delivered": False, "source_rows": len(source) + 1,
                  "required_rows": delivered, "message_rows": delivered,
                  "omitted_rows": len(source) - len(included), "carried_sections": carried,
                  "included_text_indices": sorted(included),
                  "omitted_text_indices": sorted(set(range(len(source))) - included),
                  "text_sha256": hashlib.sha256(text.encode()).hexdigest()}
