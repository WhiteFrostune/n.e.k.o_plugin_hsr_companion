"""Request-facing readiness status and follow-up registration policy (L3)."""
from __future__ import annotations

from .contracts import QueryPlan
from .readiness import evaluate_readiness


def answer_status(plan: QueryPlan, visual: dict, observer: dict, followup: dict | None = None) -> dict:
    evaluated = evaluate_readiness(plan, visual)
    ready = evaluated.confirmed
    missing = plan.fields - ready
    resource_labels = evaluated.resource_labels
    partial_resources = bool("resource" in plan.fields and plan.required_resource_labels & resource_labels)
    equipment_kinds = {f.get("kind") for f in (visual.get("screen_facts") or {}).get("facts") or []
                       if visual.get("fresh") and f.get("scope") == "equipment"}
    partial_equipment = bool(plan.required_equipment_kinds and equipment_kinds)
    if not plan.screen:
        state = "not_requested"
    elif not observer.get("enabled") or observer.get("state") in {"paused", "stopped"}:
        state = "paused"
    elif not missing:
        state = "ready"
    elif ready or partial_resources or partial_equipment:
        state = "partial"
    elif observer.get("state") == "error":
        state = "capture_failed"
    else:
        state = "unconfirmed"
    return {"state": state, "confirmed_fields": sorted(ready), "missing_fields": sorted(missing),
            "freshness": visual.get("freshness") or "fresh",
            "age_seconds": visual.get("age_seconds"),
            "age_label": visual.get("age_label") or "",
            "resource_fields": {"confirmed": sorted(plan.required_resource_labels & resource_labels),
                                "missing": sorted(plan.required_resource_labels - resource_labels)},
            "equipment_fields": evaluated.equipment,
            "applicable": evaluated.applicable,
            "followup_registered": bool((followup or {}).get("pending")),
            "connection": "not_checked",
            "instruction": "state 只描述请求画面字段的可答程度，不代表角色评价、正文交付或宿主发声完成；服务完成另看 request_completion。"
                           "这也不是助手连接状态；unconfirmed/partial 不证明断连、切页或游戏加载。"
                           "只答已确认部分；只有 followup_registered=true 才能说明已登记补答，不能保证宿主一定发声。"}


def needs_followup(plan: QueryPlan, visual: dict, observer: dict, *, completion=None) -> bool:
    """Missing service evidence, not merely missing numbers or a slow worker."""
    if not observer.get("enabled") or observer.get("state") in {"paused", "stopped", "waiting_for_game"}:
        return False
    from .request_completion import evaluate_request
    return evaluate_request(plan, visual, completion=completion).can_wait
