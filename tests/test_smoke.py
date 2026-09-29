from __future__ import annotations

import json
from pathlib import Path

from plugin.plugins.hsr_companion.core import CharacterRegistry
from plugin.plugins.hsr_companion.data import ExternalKnowledgeCatalog
from plugin.plugins.hsr_companion.detectors import analyze_layout
from plugin.plugins.hsr_companion.runtime import CompanionRuntime
from plugin.plugins.hsr_companion.session import (
    build_companion_context,
    remember_character_correction,
    resolve_character_candidate,
    sanitize_character_corrections,
    start_session,
)

ROOT = Path(__file__).resolve().parents[1]


def _registry() -> CharacterRegistry:
    return CharacterRegistry(json.loads((ROOT / "resources" / "character_registry.json").read_text(encoding="utf-8")))


def test_catalog_and_runtime_smoke() -> None:
    catalog = ExternalKnowledgeCatalog(
        registry=json.loads((ROOT / "resources" / "character_registry.json").read_text(encoding="utf-8")),
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


def test_market_package_contains_license_and_auditable_sources() -> None:
    assert (ROOT / "LICENSE.md").is_file()
    assert (ROOT / "THIRD_PARTY_NOTICES.md").is_file()
    assert (ROOT / "resources" / "licenses" / "StarRailRes-AGPL-3.0.txt").is_file()
    sources = json.loads((ROOT / "resources" / "sources.json").read_text(encoding="utf-8"))
    starrailres = [item for item in sources["sources"] if str(item.get("source_id") or "").startswith("starrailres_")]
    assert starrailres
    assert all(item["license_id"] == "AGPL-3.0-only" for item in starrailres)
    assert all(item.get("source_url") for item in starrailres)


def test_legacy_real_character_rewrite_is_disabled() -> None:
    registry = _registry()
    corrections, disabled = sanitize_character_corrections(
        [
            {
                "correction_id": "legacy",
                "observed_name": "火花",
                "observed_key": "火花",
                "canonical_name": "花火",
                "modality": "chat",
                "scope": "current_session",
                "scope_value": "session-1",
                "active": True,
            }
        ],
        registry=registry,
    )
    resolved = resolve_character_candidate(
        "火花",
        registry=registry,
        corrections=corrections,
        modality="chat",
        session_id="session-1",
    )

    assert disabled == 1
    assert corrections[0]["active"] is False
    assert resolved["status"] == "registry"
    assert resolved["canonical_name"] == "火花"


def test_real_character_correction_becomes_ambiguity_not_rewrite() -> None:
    registry = _registry()
    corrections, record = remember_character_correction(
        [],
        observed_name="花火",
        canonical_name="火花",
        modality="voice",
        registry=registry,
        session_id="session-1",
    )
    resolved = resolve_character_candidate(
        "花火",
        registry=registry,
        corrections=corrections,
        modality="voice",
        session_id="session-1",
    )

    assert record["scope"] == "confirmation_only"
    assert resolved["status"] == "needs_confirmation"
    assert resolved["canonical_name"] == ""
    assert resolved["alternatives"] == ["花火", "火花"]


def test_context_does_not_replay_historical_correction_as_current_fact() -> None:
    registry = _registry()
    session = start_session({}, reason="market-test")
    corrections, _ = remember_character_correction(
        [],
        observed_name="药光",
        canonical_name="爻光",
        modality="voice",
        registry=registry,
        session_id=session["session_id"],
    )
    context = build_companion_context(
        session=session,
        profile={"owned_characters": ["爻光"]},
        pending={},
        corrections=corrections,
        registry_meta=registry.meta,
        registry_count=len(registry.records),
        catalog_meta={"verification_status": "market-test"},
    )

    assert "药光 → 爻光" not in context
    assert "历史纠错不是当前画面事实" in context


def test_runtime_clears_character_after_verified_empty_frames() -> None:
    runtime = CompanionRuntime(confirmation_frames=2, global_rate_seconds=0)
    character_page = {
        "scene": {
            "primary_state": "menu",
            "confidence": "high",
            "evidence": ["OCR：光锥", "OCR：遗器"],
            "verified": True,
        },
        "characters": [{"entity_id": "character.yaoguang", "name": "爻光", "confidence": "high"}],
    }
    empty_menu = {**character_page, "characters": []}

    runtime.ingest(character_page, now=10.0)
    runtime.ingest(character_page, now=20.0)
    runtime.ingest(empty_menu, now=30.0)
    runtime.ingest(empty_menu, now=40.0)

    assert runtime.snapshot()["stable_character_ids"] == []
