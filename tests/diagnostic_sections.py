"""Tests deliberately consume only F2 floor-owned fields; no legacy fallback."""


def section(value, key):
    value = value.get("snapshot", value)
    floor, metric = {
        "checks": (5, "checks"),
        "last_query": (5, "last_query"),
        "service_request": (5, "service_request"),
        "prepare_calls": (5, "prepare_calls"),
        "explicit_followup": (5, "followup"),
        "panel_witness": (5, "panel_witness"),
        "answer_deliveries": (5, "deliveries"),
        "message_path_available": (5, "message_path_available"),
        "speech": (5, "speech"),
        "equipment_handoffs": (5, "equipment_handoffs"),
        "diagnostic_read": (5, "diagnostic_read"),
        "sources": (5, "sources"),
        "latest_request_health": (5, "latest_request_health"),
        "current_runtime_health": (5, "current_runtime_health"),
        "observation_health": (4, "observation_health"),
        "review": (4, "review"),
        "current_observation": (4, "current_observation"),
        "observer": (4, "observer"),
        "page_control_trace": (4, "page_control_trace"),
        "revocation_trace": (4, "revocation_trace"),
        "frame_comparisons": (4, "frame_comparisons"),
        "vision_trace": (3, "vision_trace"),
        "query_timings": (2, "query_timings"),
        "session": (1, "session"),
        "build_identity": (1, "build"),
        "assistant": (1, "assistant"),
    }[key]
    return next(f["metrics"][metric] for f in value["floors"] if f["floor"] == floor)
