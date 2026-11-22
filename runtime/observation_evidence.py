"""One current-evidence contract for chat, passive context and the panel.

Never promote model-supplied names, profile entries or historical corrections
into screen identities. Scene and identity have independent confirmation gates.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..game_state import GAME_STATE_LABELS
from .contracts import FRESH_LIVE_AGE_SECONDS
from .screen_facts import public_screen_facts

VISUAL_TTL_SECONDS = 60
_UNAVAILABLE = {
    "updating": "正在核对当前界面，尚未取得本次查询的可用结果；这不证明切页、游戏加载或连接故障，不能沿用失效事实。",
    "paused": "观察已暂停，无法确认当前画面；请查看插件面板中的暂停原因。",
    "stopped": "自动观察未开启，无法确认当前画面。",
    "waiting": "正在等待游戏画面，无法确认当前角色或场景。",
    "error": "读取游戏画面失败，无法确认当前角色或场景。",
    "expired": "上次画面已经过期，不能当作当前画面。",
    "missing": "尚未收到游戏画面，无法确认当前角色或场景。",
    "invalidated": "上次观察已失效，等待新的游戏画面。",
}


_IDENTITY_FAILURE_MESSAGES = {
    "insufficient_confirmations": "在时限内未取得足够的姓名确认。",
    "confirmation_reset": "确认期间姓名依据中断，未攒够有效读数。",
    "yielded_for_query": "确认期间的整屏扫描为提问让位，未完成核对。",
    "ocr_timeout": "确认期间有文字读取超时，未完成核对。",
}


def with_identity_confirmation(owner, evidence):
    """Add identity status without supplying an identity or changing evidence freshness."""
    if "identity_confirmation" in evidence:
        evidence = {key: value for key, value in evidence.items() if key != "identity_confirmation"}
    observer = getattr(owner, "_observer_status", {})
    if (evidence.get("can_answer_character_identity") and evidence.get("characters_complete") is not False
            or not observer.get("enabled") or observer.get("state") not in {"observing", "updating"}
            or getattr(owner, "_observer_stop_requested", False)
            or getattr(owner, "_observation_pending_reason", "").startswith(("capture_error", "capture_unavailable"))):
        return evidence
    confirmation = getattr(getattr(owner, "_runtime", None), "identity_confirmation", None)
    state = confirmation(page=evidence.get("page_type")) if confirmation is not None else None
    if state is None:
        return evidence
    if state["state"] == "failed":
        state["message"] = _IDENTITY_FAILURE_MESSAGES[state["reason"]]
    return {**evidence, "identity_confirmation": state}


def public_visual_evidence(
    analysis: object,
    observer: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    value = analysis if isinstance(analysis, dict) else {}
    timestamp = str(value.get("observed_at") or "")
    try:
        captured = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if captured.tzinfo is None:
            captured = captured.replace(tzinfo=timezone.utc)
        age = ((now or datetime.now(timezone.utc)) - captured).total_seconds()
    except ValueError:
        age = -1.0
    source = str(value.get("source") or "")
    automatic = source.startswith(("assistant", "plugin_compatibility", "auto_"))
    unavailable = ""
    if automatic and observer.get("state") in _UNAVAILABLE:
        unavailable = str(observer["state"])
    elif value.get("invalidated"):
        unavailable = "invalidated"
    elif not value:
        unavailable = "missing"
    elif not 0 <= age <= VISUAL_TTL_SECONDS:
        unavailable = "expired"
    elif not value.get("ok"):
        unavailable = "error"

    if unavailable:
        freshness = "unavailable"
    elif value.get("freshness") in {"fresh", "stale"}:
        freshness = value["freshness"]
    else:
        freshness = "stale" if age > FRESH_LIVE_AGE_SECONDS else "fresh"
    age_seconds = round(max(0.0, age), 1) if age >= 0 else None
    age_label = (f"这是 {max(1, int(round(age_seconds)))} 秒前的画面，现在可能变了"
                 if freshness == "stale" and age_seconds is not None else "")

    page = dict(value.get("page") or {})
    raw_scene = dict(value.get("scene") or {})
    runtime = dict(value.get("runtime") or {})
    scene_confirmed = bool(
        not unavailable
        and raw_scene.get("verified")
        and raw_scene.get("confidence") != "low"
        and raw_scene.get("primary_state") != "unknown"
        and runtime.get("stable_scene") == raw_scene.get("primary_state")
    )
    scene_name = str(raw_scene.get("primary_state")) if scene_confirmed else "unknown"
    stable_ids = set(runtime.get("stable_character_ids") or [])
    characters = (
        []
        if unavailable
        else [
            {key: item.get(key) for key in ("entity_id", "name", "identity_scope", "evidence", "slot") if key in item}
            for item in value.get("characters") or []
            if isinstance(item, dict) and item.get("entity_id") in stable_ids
        ]
    )
    page_characters = []
    if not unavailable:
        seen_names = set()
        for item in value.get("characters") or []:
            if (not isinstance(item, dict) or item.get("entity_id") in seen_names
                    or item.get("identity_scope") not in {"screen_label", "detail_subject"}
                    or item.get("confidence") not in {"high", "mixed"}):
                continue
            seen_names.add(item.get("entity_id"))
            page_characters.append({
                **{key: item.get(key) for key in ("entity_id", "name", "identity_scope", "evidence", "region")
                   if key in item},
                "source": "current_page_name",
                "confirmed_on_current_frame": True,
            })
    recognition = dict(value.get("recognition") or {})
    team = []
    if (
        not unavailable
        and page.get("page_type") in {"exploration_hud", "map_or_quest"}
        and len(recognition.get("team_rows") or []) == 4
    ):
        for slot in range(1, 5):
            candidates = [item for item in characters if item.get("slot") == slot]
            team.append(
                {
                    "slot": slot,
                    "confirmed": len(candidates) == 1,
                    "character": candidates[0] if len(candidates) == 1 else None,
                }
            )
    selection = dict(value.get("selection") or {})
    selected = next(
        (item["character"] for item in team if item["slot"] == selection.get("slot") and item["confirmed"]), None
    )
    controlled = (
        selected if scene_confirmed and scene_name == "exploration" and selection.get("verified") is True else None
    )
    if unavailable:
        status, message = unavailable, _UNAVAILABLE[unavailable]
    elif recognition.get("navigation_only_query"):
        has_fields = bool((value.get("screen_facts") or {}).get("facts"))
        status = "confirmed" if has_fields else "not_recognized"
        message = ("已读取当前地点与任务区域；本次没有查询角色，缺失字段仍未知。" if has_fields else
                   "已读取本次新画面，但尚无可靠的地点或任务字段；不能沿用上一页。")
    elif recognition.get("phase") == "scene_only":
        status, message = "recognizing_characters", "已确认当前场景，角色仍在识别；不能使用上一页的角色名单。"
    elif recognition.get("phase") == "scene_only_complete":
        status, message = "scene_not_recognized", "已读取本次新画面，但没有足够的场景控件证据；不能沿用上一页。"
    elif characters:
        status, message = "confirmed", "已确认界面角色名称或头像；不代表当前操控角色、剧情登场人物或账号拥有关系。"
    elif value.get("characters"):
        status, message = "confirming", "已读到角色线索，正在等待新的画面确认；暂时不能报出确定身份。"
    elif (
        not recognition.get("text_reader")
        and (recognition.get("portrait_templates") or {}).get("reason") == "unsupported_portrait_page"
    ):
        status, message = "unsupported_page", "当前页面未取得受支持的角色身份依据，无法确认角色。"
    elif (recognition.get("text_reader") or {}).get("relic_priority"):
        status, message = "not_recognized", "已读到当前遗器词条；角色归属尚未在本帧确认。"
    else:
        status, message = "not_recognized", "当前画面没有可靠的角色名字或头像识别结果，无法确认角色。"
    failure_reason = unavailable
    if not failure_reason and recognition.get("phase") == "scene_only":
        failure_reason = "character_analysis_pending"
    if not failure_reason and recognition.get("phase") == "scene_only_complete":
        failure_reason = "no_validated_scene_control"
    if not failure_reason and not characters and not recognition.get("navigation_only_query"):
        if value.get("characters"):
            failure_reason = "confirmation_pending"
        elif (value.get("ocr") or {}).get("error"):
            failure_reason = "ocr_failed"
        elif (recognition.get("text_reader") or {}).get("relic_priority"):
            failure_reason = "character_pending"
        elif (recognition.get("text_reader") or {}).get("full_frame_covered") is False:
            failure_reason = "ocr_incomplete"
        else:
            failure_reason = "no_validated_name"
    reader = recognition.get("text_reader") or {}
    context = value.get("character_context") or {}
    priority_note = ""
    if reader.get("relic_priority") and not characters:
        if page_characters:
            priority_note = (
                "本次遗器词条已完整读到；page_characters 只是当前页面显示的角色名，"
                "未在本帧确认为当前操控角色。可据此做带条件的遗器评价，必须说明来源。")
        elif context.get("name"):
            priority_note = (
                f"本次遗器词条已完整读到；角色沿用上一轮完整识别的 {context['name']}，"
                "但未在本帧确认。可据此做带条件的评价，必须说明来源。")
        else:
            priority_note = (
                "本次遗器词条已完整读到，但角色未在本帧确认：先报词条，不评价适配；"
                "可以问玩家这件是给谁穿的，或说明角色尚未确认。")
    # Only the name-region reader knows the member positions; a full-frame
    # read keeps its existing partial semantics and must not be relabeled here.
    roster = {key: value[key] for key in
              ("team_members_expected", "team_members_read", "characters_complete",
               "team_roster_known", "team_slots") if key in value}
    if roster.get("team_roster_known") is False:
        roster_note = (
            f"编队页名单未完全确认：只读到 {roster['team_members_read']}/"
            f"{roster['team_members_expected']} 位，另有位置是空位还是漏读未确认；"
            "不当作完整队伍，也不断言空位。"
        )
    elif roster.get("characters_complete") is False:
        roster_note = (
            f"编队页只读到 {roster['team_members_read']}/{roster['team_members_expected']} 位队员；"
            "名单不完整，不得当作完整队伍回答，必须说明可能还有未识别队员，空位与漏识别不可区分。"
        )
    else:
        roster_note = ""
    display_message = message
    if age_label and not message.startswith(age_label):
        display_message = age_label + "：" + message
    return {
        "status": status,
        "message": display_message,
        "fresh": not bool(unavailable),
        "freshness": freshness,
        "age_seconds": age_seconds,
        "age_label": age_label,
        "read_scope": ("region" if recognition.get("region_query") else
                       "navigation" if recognition.get("navigation_only_query") else
                       "scene" if recognition.get("scene_only_query") or recognition.get("phase") == "scene_only" else "full"),
        "observed_at": timestamp,
        "valid_for_seconds": VISUAL_TTL_SECONDS,
        "page_type": str(page.get("page_type") or "unknown") if scene_confirmed else "unknown",
        "scene": {
            "primary_state": scene_name,
            "label": str(page.get("label") or GAME_STATE_LABELS.get(scene_name, GAME_STATE_LABELS["unknown"]))
            if scene_confirmed
            else GAME_STATE_LABELS["unknown"],
            "verified": scene_confirmed,
            "reason": unavailable or ("confirmed" if scene_confirmed else "scene_not_confirmed"),
        },
        "characters": characters,
        "page_characters": page_characters,
        "character_context": context or None,
        "screen_facts": public_screen_facts(
            value.get("screen_facts"), fresh=not bool(unavailable), observed_at=timestamp,
            serial=int(value.get("observation_serial") or 0),
        ),
        "team": team,
        "controlled_character": controlled,
        "controlled_character_status": "confirmed" if controlled else "selected_slot_unconfirmed",
        "can_answer_character_identity": bool(characters),
        "identity_list_complete": False,
        "unrecognized_does_not_mean_empty_slot": True,
        "failure_reason": failure_reason,
        "text_coverage_complete": (recognition.get("text_reader") or {}).get("full_frame_covered"),
        "equipment_complete": bool(reader.get("relic_priority")),
        **roster,
        "instruction": (
            "当前画面身份只允许引用本结果 characters 中的姓名，并保留 identity_scope 的范围；"
            "characters 为空时必须明确说‘目前无法确认角色’，不能从数据库搜索命中、玩家档案、"
            "历史纠错、旧对话或自行提供的候选名字补出当前角色；场景 unknown 时不得沿用上次场景。"
            "不得承诺恢复画面后一定能认出角色；只说明实际观察状态和已确认结果。"
            "队伍成员不等于操控角色；只有 controlled_character 非空才允许回答正在操控谁。"
            "screen_label 只证明界面显示该名字，不证明角色出现在3D场景；team_selection 是编队页显示对象，"
            "detail_subject 是详情展示对象，banner_label 是卡池展示对象，均不证明已拥有或已出战。"
            "不能自行把识别失败归因于视角、未加载、玩家没动或游戏没打开；只能依据返回的状态说明。"
            "转动镜头不是切页；重新核对或像素变化不证明界面跳转、游戏加载或连接未完成，不得以这些理由搪塞。"
            "名单可能漏识别；只认出三人不代表第四个位置空着，未认出某人不代表其不在画面。"
            "玩家说出名字只是玩家提供的线索，不证明你看到了此人或其刚加入队伍。"
            "页面状态不证明任务完成、挑战胜负或角色获得；没有相应事件证据不得自行庆祝。"
            "没有带来源的配队分析时，不能断言阵容强力、有几个问题或有几个调整方案。"
            "快速切页或角色仍在识别时只说已确认的页面；场景未确认就说无法确认页面类型，绝不能随机挑选场景。"
            "visible_text 仅是当前画面可见文字，可按原文转述；heading_candidate 不是已确认标题，不能据此猜页面类型或地点。"
            "没有本次工具返回的 followup.pending=true，禁止承诺稍后补答；未知页面不等于游戏正在加载。"
            "这些事实只描述 observed_at 时刻的画面，不保证玩家此刻仍停留在该页；不要说‘一直盯着’或承诺即时识别。"
            + roster_note
            + priority_note
            + ("若 freshness=stale，必须原样说明 age_label 的秒数，不得说成现在。"
               if freshness == "stale" else "")
        ),
    }
