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
    "reward": ("只识别到结算或奖励相关页面，不证明任务完成、挑战成功或已领取奖励；不得庆祝完成、胜利或编造奖励。"),
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
                f"数据库确认角色：{'、'.join(names) if names else '无'}（仅界面名称，不是外观或操控角色判定）",
                f"角色实体 ID：{'、'.join(entity_ids) if entity_ids else '无'}",
            ]
        )
        scope_labels = {
            "screen_label": "界面显示的数据库角色名，位置与用途尚未确认，不证明3D外观、操控、出战或拥有关系",
            "hud_label": "HUD 名称，不证明当前操控角色或账号拥有关系",
            "hud_portrait": "HUD 头像，不证明当前操控角色或账号拥有关系",
            "dialogue_speaker": "剧情说话者名称，不证明出战角色",
            "detail_subject": "详情页展示对象，不证明当前操控角色",
            "team_selection": "编队页名称，不证明已出战或账号拥有",
            "banner_label": "跃迁页名称，不证明已获得角色",
            "reward_label": "奖励页名称，不据此写入账号档案",
        }
        for item in payload.get("identities") or []:
            if isinstance(item, dict):
                scope = scope_labels.get(str(item.get("identity_scope") or ""), "界面名称，身份范围未确认")
                fact_lines.append(f"{item.get('name') or ''}的证据范围：{scope}")
        instruction = (
            "只确认上述界面角色名，不代表看懂角色外观。可以围绕已确认名称自然说一句简短的话；"
            "必须原样使用角色名，不得改名、猜测其他角色；不得据此断言当前操控角色、账号拥有关系或剧情登场人物。"
        )
    elif event.kind == "exploration_idle":
        fact_lines.append("玩家已在探索状态停留一段时间，近期没有更高优先级事件。")
        instruction = (
            "可以用符合你人格的一句轻松短句陪伴玩家；不要假装看见了具体地点、任务、敌人或宝箱，也不要连续追问。"
        )
    else:
        fact_lines.append(f"事件：{event.kind}")

    # Recognizing names updates perception; it is not itself a reason to speak.
    # A roster cue queued just before a user query can otherwise arrive after
    # the answer and turn one location question into two unrelated replies.
    should_respond = bool(proactive_enabled and event.proactive and event.kind != "characters_identified")
    if not should_respond:
        instruction = "把这条信息只作为静默上下文，不需要立即发言。"

    text = "\n".join(
        [
            "[星铁陪玩事件｜插件已验证]",
            *fact_lines,
            f"数据库版本：{catalog_revision or '最小角色注册表'}",
            instruction,
            (
                "这只是触发信号，不是开口时的实时画面。准备主动提及场景或角色前，先调用 "
                "hsr_companion_prepare 查询当前画面（只问场景可用 intent=game_state）；"
                "以新查询结果为准，若已切页或无法确认，放弃旧事件的场景和名单，不播报旧页、不随机补一个场景。"
            ) if should_respond else "",
            "识别名单不保证完整：未识别不等于空位或角色不在场。页面状态不证明任务完成或获得角色；没有可靠分析时不得评价配队强弱或编造问题与方案。",
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
            "arbitration_scope": "plugin_before_submission" if should_respond else "passive_context",
            "image_stored": False,
            "requires_current_observation": should_respond,
        },
    )


__all__ = ["EventDelivery", "build_event_delivery"]
