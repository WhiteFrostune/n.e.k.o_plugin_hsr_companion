"""Bounded proof-lifecycle metadata, never OCR strings, coordinates or pixels."""
from __future__ import annotations

import math

STAGES = {"staged", "validated", "committed", "retained", "delivery_rejected"}
ORIGINS = {"native_layout", "same_capture_preview", "scene_reader", "navigation_reader", "snapshot", "committed"}
CONDITIONS = {"accepted", "navigation", "location_heading", "task_stack", "chat_control", "other_layout"}
COUNTS = {"control_count", "input_count", "proved_count", "heading_count", "navigation_count", "status_count",
          "chat_count", "stack_count", "selection_serial", "epoch", "revision", "attempts"}


def sanitize_trace(rows):
    result = []
    if not isinstance(rows, (list, tuple)):
        return result
    for row in rows[-8:]:
        if not isinstance(row, dict):
            continue
        def code(key, allowed):
            item = row.get(key)
            return item if isinstance(item, str) and item in allowed else "unknown"
        value = {"stage": code("stage", STAGES), "origin": code("origin", ORIGINS),
                 "condition": code("condition", CONDITIONS)}
        for key in COUNTS:
            number = row.get(key)
            if type(number) is int and number >= 0 or type(number) is float and math.isfinite(number) and number >= 0:
                value[key] = min(1000000, int(number))
        if "revalidated" in row:
            value["revalidated"] = row["revalidated"] is True
        result.append(value)
    return result


def public_control_trace(owner):
    return sanitize_trace(getattr(owner, "_page_control_trace", []))


def record_controls(owner, stage, *, analysis=None, controls=None, origin=None):
    analysis = analysis or {}
    recognition = analysis.get("recognition") or {}
    metadata = recognition.get("control_trace") or {}
    selected = (recognition.get("scene_regions") or []) if controls is None else controls
    row = {**metadata, "stage": stage, "origin": origin or metadata.get("origin"),
           "control_count": len(selected), "epoch": getattr(owner, "_observation_epoch", 0),
           "revision": getattr(owner, "_observation_serial", 0),
           "selection_serial": (analysis.get("screen_facts") or {}).get("observation_serial",
                                analysis.get("observation_serial", getattr(owner, "_observation_serial", 0)))}
    row = sanitize_trace([row])[0]
    history = public_control_trace(owner)
    # Repeated query polling must not evict the producer/commit provenance.
    if history and {k: v for k, v in history[-1].items() if k != "attempts"} == row:
        history[-1]["attempts"] = min(1000000, history[-1].get("attempts", 1) + 1)
    else:
        history.append(row)
    owner._page_control_trace = history[-8:]
