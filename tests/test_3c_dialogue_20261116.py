"""3C dialogue red tests from the 2026-11-16 real conversation.

The A read passed; these target the remaining three behaviors:
- the natural next-item follow-up "那这件呢" inherits the previous rows detail;
- a panel with values but no confirmed character still reports the values and
  asks who it is, without inventing a fit conclusion;
- a stale-page wait ends with a calm "稍等" notice and a delayed follow-up keeps
  the full rows detail.
"""
import json
import time
from datetime import datetime
from pathlib import Path

from plugin.plugins.hsr_companion.data import ExternalKnowledgeCatalog
from plugin.plugins.hsr_companion.runtime.answer_message import render_followup
from plugin.plugins.hsr_companion.runtime.followup_delivery import FOLLOWUP_SECONDS
from plugin.plugins.hsr_companion.runtime.guidance import GuidanceCatalog, build_guidance, plan_guidance
from plugin.plugins.hsr_companion.runtime.query_plan import build_query_plan
from plugin.plugins.hsr_companion.tests.test_experience_fixes import box
from plugin.plugins.hsr_companion.tests.test_screen_facts import facts

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime.fromisoformat("2026-11-16T19:43:00+08:00")


def previous_rows():
    return {"at": time.monotonic() - 5, "kind": "build", "request_focus": "equipment",
            "equipment_kinds": ["head"], "guide_topics": ["relic"],
            "reply_detail": "full", "answer_parts": ["rows"], "character_ids": []}


def test_next_item_followup_inherits_previous_rows_detail():
    book = GuidanceCatalog(json.loads((ROOT / "resources/guidance.cn-4.6.json").read_text(encoding="utf-8")))
    catalog = ExternalKnowledgeCatalog(
        registry=json.loads((ROOT / "resources/character_registry.json").read_text(encoding="utf-8")),
        root=ROOT / "dist" / "test-3c-dialogue")
    base = build_query_plan("那这件呢", "read_screen", screen_fields=("equipment",))
    selected = plan_guidance("那这件呢", base, book=book, catalog=catalog, previous=previous_rows(),
                             now=NOW)
    plan = selected.observation_plan(base)
    assert plan.response_detail == "full"
    assert {"main_stat", "sub_stat"} <= plan.required_equipment_kinds


def test_new_focus_does_not_inherit_rows_detail():
    book = GuidanceCatalog(json.loads((ROOT / "resources/guidance.cn-4.6.json").read_text(encoding="utf-8")))
    catalog = ExternalKnowledgeCatalog(
        registry=json.loads((ROOT / "resources/character_registry.json").read_text(encoding="utf-8")),
        root=ROOT / "dist" / "test-3c-dialogue")
    base = build_query_plan("这件给谁用", "assess_relic")
    selected = plan_guidance("这件给谁用", base, book=book, catalog=catalog, previous=previous_rows(),
                             now=NOW)
    assert "rows" not in selected.answer_parts


def test_panel_without_character_reports_values_then_asks():
    book = GuidanceCatalog(json.loads((ROOT / "resources/guidance.cn-4.6.json").read_text(encoding="utf-8")))
    catalog = ExternalKnowledgeCatalog(
        registry=json.loads((ROOT / "resources/character_registry.json").read_text(encoding="utf-8")),
        root=ROOT / "dist" / "test-3c-dialogue")
    query = "评价一下当前角色面板"
    base = build_query_plan(query, "assess_panel")
    selected = plan_guidance(query, base, book=book, catalog=catalog,
                             consultation={"scope": "current_panel", "subject_state": "current",
                                           "answer_parts": ["assessment"]}, now=NOW)
    panel = facts([box("等级80", 20, 40), box("生命值 4890", 20, 80), box("防御力 1029", 20, 120)],
                  page="character_detail")
    result = build_guidance(selected, query, book=book, catalog=catalog, profile={}, now=NOW,
                            visual={"fresh": True, "observed_at": "capture", "characters": [],
                                    "screen_facts": panel})
    conclusion = (result.get("reply_plan") or {}).get("conclusion") or ""
    assert "4890" in conclusion or "生命" in conclusion or "面板" in conclusion
    assert "角色" not in conclusion or "这位" in conclusion


def test_first_wait_message_is_calm_not_cursor_instruction(tmp_path):
    from plugin.plugins.hsr_companion.runtime.service_contract import response_contract
    from plugin.plugins.hsr_companion.tests.test_current_observation import plugin_fixture
    owner, _, _ = plugin_fixture(tmp_path)
    contract = response_contract(owner, 1, channel="direct")
    assert "稍等" in contract["instruction"]
    assert "不要让玩家移动光标" in contract["instruction"]
    assert "停一下" not in contract["instruction"]
    assert "对不起" not in contract["instruction"]


def test_final_followup_timeout_has_no_cursor_or_apology():
    text, receipt = render_followup({}, plan=build_query_plan("那这件呢", "read_screen",
                                                              screen_fields=("equipment",)), ready=False)
    assert "光标" not in text
    assert "对不起" not in text
    assert receipt["mode"] == "timeout_notice"


def test_delayed_followup_keeps_full_rows_detail():
    plan = build_query_plan("那这件呢", "read_screen", screen_fields=("equipment",), response_detail="full")
    data = {"guidance_answer": {"reply_plan": {"mode": "full", "enabled": True}},
            "equipment_answer": {
                "name": "伶人的弯月舞靴", "slot": "脚部", "level": "+15",
                "main_stat": {"label": "主词条·速度", "value": "25"},
                "sub_stats": [{"label": "副词条·生命值", "value": "76"},
                              {"label": "副词条·防御力", "value": "57"},
                              {"label": "副词条·暴击率", "value": "6.1%"},
                              {"label": "副词条·暴击伤害", "value": "12.3%"}],
                "state": "confirmed", "complete": True, "sub_stats_complete": True}}
    text, receipt = render_followup(data, plan=plan, ready=True)
    for value in ("25", "76", "57", "6.1%", "12.3%"):
        assert value in text
    assert receipt["complete"] is True


def test_followup_total_wait_has_a_cap():
    assert 0 < FOLLOWUP_SECONDS <= 35.0
