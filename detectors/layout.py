"""Conservative Star Rail page recognition using OCR layout anchors.

This is intentionally a supported-page classifier, not a general screenshot
guesser.  Character identity is attempted only inside page-specific regions;
unsupported frames remain ``unknown``.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any, Iterable

from ..data import normalize_lookup_text


PAGE_RULES: tuple[dict[str, Any], ...] = (
    {
        "page_type": "character_detail",
        "scene": "menu",
        "label": "角色详情",
        "strong": ("角色详情",),
        "anchors": ("光锥", "遗器", "行迹", "星魂", "属性"),
        "minimum": 2,
    },
    {
        "page_type": "team_setup",
        "scene": "team_setup",
        "label": "队伍编成",
        "strong": ("队伍编成", "快速编队"),
        "anchors": ("开始挑战", "支援角色", "出战", "队伍配置"),
        "minimum": 1,
    },
    {
        "page_type": "combat",
        "scene": "combat",
        "label": "回合制战斗",
        "strong": ("战技点", "我方回合", "敌方回合"),
        "anchors": ("终结技", "弱点击破", "行动序列", "剩余轮次"),
        "minimum": 1,
    },
    {
        "page_type": "story_dialogue",
        "scene": "story",
        "label": "剧情对话",
        "strong": ("对话记录", "选择你的回答"),
        "anchors": ("自动播放", "跳过剧情", "点击继续"),
        "minimum": 1,
    },
    {
        "page_type": "warp",
        "scene": "warp",
        "label": "跃迁",
        "strong": ("角色活动跃迁", "光锥活动跃迁", "跃迁记录"),
        "anchors": ("星轨专票", "跃迁", "十连跃迁"),
        "minimum": 1,
    },
    {
        "page_type": "reward",
        "scene": "reward",
        "label": "结算与奖励",
        "strong": ("挑战成功", "通关奖励", "获得角色"),
        "anchors": ("获得物品", "战利品", "领取奖励"),
        "minimum": 1,
    },
    {
        "page_type": "map_or_quest",
        "scene": "exploration",
        "label": "地图与任务",
        "strong": ("任务追踪", "当前任务"),
        "anchors": ("导航", "距离目标", "开启导航", "传送"),
        "minimum": 1,
    },
    {
        "page_type": "loading",
        "scene": "loading",
        "label": "登录与加载",
        "strong": ("正在加载", "点击进入"),
        "anchors": ("列车跃迁中", "数据加载"),
        "minimum": 1,
    },
)


def _box_dict(box: Any) -> dict[str, Any]:
    if is_dataclass(box):
        value = asdict(box)
    elif isinstance(box, dict):
        value = dict(box)
    else:
        value = {
            key: getattr(box, key, None)
            for key in ("text", "left", "top", "right", "bottom", "score")
        }
    return {
        "text": str(value.get("text") or "").strip(),
        "left": float(value.get("left") or 0.0),
        "top": float(value.get("top") or 0.0),
        "right": float(value.get("right") or 0.0),
        "bottom": float(value.get("bottom") or 0.0),
        "score": float(value.get("score") or 0.0),
    }


def classify_supported_page(
    text: str,
    boxes: Iterable[Any],
    *,
    width: int,
    height: int,
) -> dict[str, Any]:
    compact = normalize_lookup_text(text)
    candidates: list[tuple[int, dict[str, Any], list[str]]] = []
    for rule in PAGE_RULES:
        strong = [term for term in rule["strong"] if normalize_lookup_text(term) in compact]
        anchors = [term for term in rule["anchors"] if normalize_lookup_text(term) in compact]
        if not strong and len(anchors) < int(rule["minimum"]):
            continue
        score = len(strong) * 4 + len(anchors)
        candidates.append((score, rule, strong + anchors))
    if not candidates:
        return {
            "page_type": "unknown",
            "label": "暂不支持的画面",
            "scene": "unknown",
            "confidence": "low",
            "evidence": [],
            "classifier": "hsr_layout_ocr_v1",
            "supported": False,
            "image_size": [int(width), int(height)],
        }
    candidates.sort(key=lambda value: -value[0])
    score, rule, hits = candidates[0]
    return {
        "page_type": rule["page_type"],
        "label": rule["label"],
        "scene": rule["scene"],
        "confidence": "high" if score >= 4 or len(hits) >= 2 else "mixed",
        "evidence": [f"界面锚点：{term}" for term in hits[:6]],
        "classifier": "hsr_layout_ocr_v1",
        "supported": True,
        "image_size": [int(width), int(height)],
    }


def _box_allowed(page_type: str, box: dict[str, Any], *, width: int, height: int) -> bool:
    if box["score"] < 0.50 or not box["text"]:
        return False
    center_x = (box["left"] + box["right"]) / 2.0
    center_y = (box["top"] + box["bottom"]) / 2.0
    if page_type == "character_detail":
        return center_x <= width * 0.72 and center_y <= height * 0.55
    if page_type == "story_dialogue":
        return center_x <= width * 0.65 and center_y >= height * 0.42
    if page_type in {"team_setup", "warp", "reward"}:
        return True
    return False


def detect_characters_in_page(
    page: dict[str, Any],
    boxes: Iterable[Any],
    catalog: Any,
    *,
    width: int,
    height: int,
) -> list[dict[str, Any]]:
    page_type = str(page.get("page_type") or "unknown")
    if page_type not in {"character_detail", "team_setup", "story_dialogue", "warp", "reward"}:
        return []
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in boxes:
        box = _box_dict(raw)
        if not _box_allowed(page_type, box, width=width, height=height):
            continue
        for record in catalog.match_characters_in_text(box["text"]):
            record_id = str(record.get("id") or "")
            if not record_id or record_id in seen:
                continue
            seen.add(record_id)
            exact = normalize_lookup_text(record.get("name")) == normalize_lookup_text(box["text"])
            found.append(
                {
                    "entity_id": record_id,
                    "name": str(record.get("name") or ""),
                    "evidence": f"{page_type}_name_region",
                    "confidence": "high" if exact and box["score"] >= 0.70 else "mixed",
                    "ocr_score": round(float(box["score"]), 3),
                    "region": {
                        "left": round(box["left"] / max(width, 1), 4),
                        "top": round(box["top"] / max(height, 1), 4),
                        "right": round(box["right"] / max(width, 1), 4),
                        "bottom": round(box["bottom"] / max(height, 1), 4),
                    },
                }
            )
    found.sort(
        key=lambda item: (
            item["confidence"] != "high",
            -float(item.get("ocr_score") or 0.0),
            item["entity_id"],
        )
    )
    if page_type in {"character_detail", "story_dialogue"}:
        return found[:1]
    return found[:8]


def analyze_layout(
    text: str,
    boxes: Iterable[Any],
    catalog: Any,
    *,
    width: int,
    height: int,
) -> dict[str, Any]:
    materialized = [_box_dict(box) for box in boxes]
    page = classify_supported_page(text, materialized, width=width, height=height)
    return {
        "page": page,
        "scene": {
            "primary_state": page["scene"],
            "substate": page["page_type"],
            "confidence": page["confidence"],
            "evidence": list(page["evidence"]),
            "classifier": page["classifier"],
            "verified": bool(page["supported"]),
        },
        "characters": detect_characters_in_page(
            page,
            materialized,
            catalog,
            width=width,
            height=height,
        ),
        "boxes": materialized,
    }


__all__ = [
    "PAGE_RULES",
    "analyze_layout",
    "classify_supported_page",
    "detect_characters_in_page",
]
