"""Bind the existing host model's semantic relation to a public user event.

No keyword classifier, second model, raw-message retention or deadline renewal.
The model labels intent; SDK message identity prevents stale/replayed consent.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class UserActivity:
    stamp: float
    digest: str | None


def message_digest(text):
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest() if isinstance(text, str) and 0 < len(text) <= 4096 else None


def activity_stamp(activity):
    return activity.stamp if isinstance(activity, UserActivity) else activity


def message_allows(owner, request, activity):
    if getattr(owner, "_followup_relation_hold", None) == request.request_id:
        return False
    stamp = activity_stamp(activity)
    if stamp is None or stamp <= request.asked_at:
        return True
    confirmation = getattr(owner, "_followup_confirmation", None)
    return bool(isinstance(activity, UserActivity) and activity.digest and confirmation ==
                (request.request_id, activity.stamp, activity.digest))


def relation_contract(request):
    return {"request_id": request.request_id,
        "relations": ["confirm", "cancel", "new_request", "uncertain"],
        "instruction": "用户接话时由你结合本题含义判断关系，用同一hsr_companion_prepare的pending_reply传原request_id、relation和本条player_message原话。"
                       "同意继续等是confirm；明确不等/取消是cancel；另一个问题正常调用新需求（替换原待办）。"
                       "不确定传uncertain，不自行猜。confirm不启动读屏、不延长截止时间，不把确认说成新任务。"}
