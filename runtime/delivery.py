"""Turn verified Star Rail events into host delivery plans.

The runtime decides *what happened*.  This module decides whether that event is
merely context or an appropriate moment for N.E.K.O. to speak.  Keeping the two
steps separate prevents every OCR refresh from becoming a voice interruption.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .contracts import CompanionEvent


@dataclass(frozen=True)
class EventDelivery:
    text: str
    ai_behavior: str
    priority: int
    coalesce_key: str
    metadata: dict[str, Any] = field(default_factory=dict)


_SCENE_LABELS = {
    "combat": "战斗",
    "story": "剧情 / 对话",
    "exploration": "探索 / 跑图",
    "menu": "菜单 / 养成",
    "team_setup": "队伍编成 / 挑战准备",
    "warp": "跃迁 / 抽卡",
    "reward": "结算 / 奖励",
    "loading": "登录 / 加载",
    "other": "其他星铁画面",
}

_SCENE_INSTRUCTIONS = {
    "combat": (
        "玩家刚进入战斗。请用符合你人格的一句简短陪伴自然接入；"
        "不要编造敌人、队伍、血量、技能或战况，玩家没问时不要展开长篇攻略。"
    ),
    "team_setup": (
        "玩家正在队伍编成或挑战准备界面。可以简短表示你在陪着，或自然询问是否需要配队建议；"
        "不要假定画面上未被数据库确认的角色。"
    ),
    "warp": ("玩家进入了跃迁界面。可以自然说一句轻松的陪伴或祝好运；不要声称已经抽到任何角色或物品。"),
    "reward": (
        "玩家进入结算或奖励画面。可以自然回应这一阶段已经结束；未识别到具体奖励时，不得编造奖励、胜负或完成结果。"
    ),
}


def build_event_delivery(
    event: CompanionEvent,
    *,
    catalog_revision: str,
    proactive_enabled: bool,
) -> EventDelivery:
    """Build a bounded host message from one already-arbitrated event."""

    payload = dict(event.payload or {})
    fact_lines: list[str] = []
    instruction = "把这条信息只作为静默上下文，不需要立即发言。"

    if event.kind == "scene_changed":
        current = str(payload.get("current") or "other")
        previous = str(payload.get("previous") or "unknown")
        fact_lines.extend(
            [
                f"数据库约束的本地界面状态：{_SCENE_LABELS.get(current, current)}",
                f"上一稳定状态：{_SCENE_LABELS.get(previous, previous)}",
            ]
        )
        instruction = _SCENE_INSTRUCTIONS.get(current, instruction)
    elif event.kind == "characters_identified":
        names = [str(name).strip() for name in payload.get("names") or [] if str(name).strip()]
        entity_ids = [str(entity_id).strip() for entity_id in payload.get("entity_ids") or [] if str(entity_id).strip()]
        fact_lines.extend(
            [
                f"数据库确认角色：{'、'.join(names) if names else '无'}",
                f"角色实体 ID：{'、'.join(entity_ids) if entity_ids else '无'}",
            ]
        )
        instruction = (
            "这些名称经过插件数据库实体校验。可以围绕已确认角色自然说一句简短的话；"
            "必须原样使用角色名，不得改名、猜测其他角色，也不要仅凭角色身份编造当前队伍或剧情。"
        )
    elif event.kind == "exploration_idle":
        fact_lines.append("玩家已在探索状态停留一段时间，近期没有更高优先级事件。")
        instruction = (
            "可以用符合你人格的一句轻松短句陪伴玩家；不要假装看见了具体地点、任务、敌人或宝箱，也不要连续追问。"
        )
    else:
        fact_lines.append(f"事件：{event.kind}")

    should_respond = bool(proactive_enabled and event.proactive)
    if not should_respond:
        instruction = "把这条信息只作为静默上下文，不需要立即发言。"

    text = "\n".join(
        [
            "[星铁陪玩事件｜插件已验证]",
            *fact_lines,
            f"数据库版本：{catalog_revision or '最小角色注册表'}",
            instruction,
        ]
    )
    return EventDelivery(
        text=text,
        ai_behavior="respond" if should_respond else "read",
        priority=event.priority if should_respond else min(event.priority, 3),
        coalesce_key="hsr_proactive_event" if should_respond else "hsr_local_observation",
        metadata={
            "kind": event.kind,
            "event_id": event.event_id,
            "catalog_revision": catalog_revision,
            "proactive_requested": should_respond,
            "delivery_intent": "realtime_cue" if should_respond else "passive_context",
            "interrupt_policy": "drop_if_busy",
            "image_stored": False,
        },
    )


__all__ = ["EventDelivery", "build_event_delivery"]
