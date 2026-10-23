"""Bind the existing host model's semantic relation to a public user event.

No keyword classifier, second model, raw-message retention or deadline renewal.
The model labels intent; SDK message identity prevents stale/replayed consent.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
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


async def handle_pending_reply(owner, payload):
    from .chat_activity import latest_user_activity
    from .followup import cancel_followup

    rejected = {"accepted": False, "instruction": "未绑定当前待办，不确认已继续或取消；有新问题请正常调用统一入口。"}
    if not isinstance(payload, dict):
        return {**rejected, "reason": "invalid_relation"}
    request = getattr(owner, "_pending_answer", None)
    if (request is None or payload.get("request_id") != request.request_id
            or time.monotonic() >= request.expires_at or owner._target_character() != request.target):
        return {**rejected, "reason": "request_not_current"}
    relation = payload.get("relation")
    digest = message_digest(payload.get("player_message"))
    if relation not in {"confirm", "cancel", "new_request", "uncertain"} or not digest:
        return {**rejected, "reason": "invalid_relation"}
    # An in-flight cancel/confirmation cannot race submission while its SDK
    # identity is checked. Failure stays held inside the ORIGINAL deadline.
    owner._followup_relation_hold = request.request_id
    # The final speech guard may own the one activity reader. Hold that
    # request's next guard, let the in-flight read drain, then read identity.
    # No second reader, queued IPC backlog or renewal of the original budget.
    deadline = min(request.expires_at, time.monotonic() + 1.1)
    state, activity = "unavailable", None
    while time.monotonic() < deadline and getattr(owner, "_pending_answer", None) is request:
        remaining = deadline - time.monotonic()
        state, activity = await latest_user_activity(owner.bus, target=request.target, limit=8, include_identity=True,
            transport_timeout=min(1.0, max(.001, remaining)), wait_timeout=max(.001, remaining))
        if state != "unavailable":
            break
        await asyncio.sleep(min(.05, max(0, deadline - time.monotonic())))
    if state != "available":
        return {**rejected, "reason": "activity_unavailable"}
    if (getattr(owner, "_pending_answer", None) is not request or time.monotonic() >= request.expires_at
            or state != "available" or not isinstance(activity, UserActivity) or activity.digest != digest
            or activity.stamp <= request.asked_at or owner._target_character() != request.target):
        return {**rejected, "reason": "message_not_current"}
    if relation in {"cancel", "new_request"}:
        cancel_followup(owner, "user_cancelled" if relation == "cancel" else "new_request")
    elif relation == "confirm":
        owner._followup_relation_hold = None
        owner._followup_message_wait = None
        owner._followup_confirmation = (request.request_id, activity.stamp, activity.digest)
        owner._followup_status = {**getattr(owner, "_followup_status", {}),
            "request_id": request.request_id, "state": "pending", "reason": "confirmed_continue"}
    else:
        owner._followup_confirmation = None
    event = getattr(owner, "_visual_update_event", None)
    if event is not None:
        event.set()
    return {"accepted": True, "relation": relation, "request_id": request.request_id,
        "pending": relation not in {"cancel", "new_request"},
        "expires_in_seconds": round(max(0, request.expires_at - time.monotonic()), 3),
        "instruction": "仅更新原待办关系；未重启识别、未延长截止时间。交还话轮，不复述原问题；uncertain时先澄清。"}
