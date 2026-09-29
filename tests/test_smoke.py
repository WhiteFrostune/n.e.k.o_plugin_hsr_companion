from __future__ import annotations

import json
from pathlib import Path

from plugin.plugins.hsr_companion.data import ExternalKnowledgeCatalog
from plugin.plugins.hsr_companion.detectors import analyze_layout
from plugin.plugins.hsr_companion.runtime import CompanionRuntime


ROOT = Path(__file__).resolve().parents[1]


def test_catalog_and_runtime_smoke() -> None:
    catalog = ExternalKnowledgeCatalog(
        registry=json.loads(
            (ROOT / "resources" / "character_registry.json").read_text(encoding="utf-8")
        ),
        root=ROOT / ".missing-test-data-pack",
    )
    assert catalog.resolve_character("爻光")["id"] == "character.yaoguang"
    assert catalog.resolve_character("不存在的角色") is None
    result = analyze_layout(
        "角色详情\n爻光\n光锥\n遗器",
        [
            {"text": "角色详情", "score": 0.9, "left": 1, "top": 1, "right": 50, "bottom": 20},
            {"text": "爻光", "score": 0.9, "left": 10, "top": 30, "right": 50, "bottom": 50},
            {"text": "光锥", "score": 0.9, "left": 10, "top": 60, "right": 50, "bottom": 80},
            {"text": "遗器", "score": 0.9, "left": 10, "top": 90, "right": 50, "bottom": 110},
        ],
        catalog,
        width=1280,
        height=720,
    )
    assert result["scene"]["primary_state"] == "menu"
    assert result["characters"][0]["entity_id"] == "character.yaoguang"
    assert CompanionRuntime().snapshot()["stable_scene"] == "unknown"
