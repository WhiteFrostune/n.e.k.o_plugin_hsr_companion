"""Red replay of the three real model consultations from 2026-11-16.

The current code rejects all three at the prepare contract. The corrected
behavior is expected: current-screen reads/evaluations are normalized, while
explicit subjects and non-current scopes still fail as before.
"""
import asyncio
import importlib

import pytest
from plugin.plugins.hsr_companion.tests.test_current_observation import plugin_fixture
from plugin.plugins.hsr_companion.tests.test_prepare_failure_trace import last, resolver


def missing():
    return {"fresh": False, "screen_facts": {"facts": []}, "characters": [],
            "controlled_character": None, "scene": {"label": "未知", "verified": False},
            "page_type": "unknown", "observed_at": None, "instruction": ""}


CASES = [
    ("panel", "评价一下当前角色面板", "assess_panel", None, None,
     {"scope": "current_panel", "subject_state": "specified", "answer_parts": ["assessment"]}),
    ("relic_a", "这件遗器的主词条和副词条分别是什么？", "read_screen", ["equipment"], "full",
     {"scope": "current_relic", "subject_state": "specified", "topics": ["relic"], "answer_parts": ["rows"]}),
    ("relic_b", "现在呢", "read_screen", ["equipment"], "full",
     {"scope": "current_relic", "subject_state": "inherit", "topics": ["relic"], "answer_parts": ["rows"]}),
]

BOUNDARY = [
    ("explicit_subject", "这件遗器的主词条和副词条分别是什么？", "read_screen", ["equipment"], "full",
     {"scope": "current_relic", "subject_state": "specified", "character": "真珠",
      "topics": ["relic"], "answer_parts": ["rows"]}),
    ("reference_build", "给真珠配装", "build_guide", None, None,
     {"scope": "reference_build", "subject_state": "specified"}),
    ("saved_cone", "我存的光锥", "assess_cone", None, None,
     {"scope": "saved_cone", "subject_state": "specified"}),
]


def prepare(owner, monkeypatch, query, intent, fields, detail, consultation):
    resolver(monkeypatch, owner, missing())
    module = importlib.import_module(type(owner).__module__)
    monkeypatch.setattr(module, "arm_followup", lambda *args, **kwargs: {"pending": True, "reason": "waiting"})

    async def run():
        kwargs = {"query": query, "intent": intent, "consultation": consultation}
        if fields is not None:
            kwargs["screen_fields"] = fields
        if detail is not None:
            kwargs["response_detail"] = detail
        return await owner.hsr_companion_prepare(**kwargs)

    return asyncio.run(run())


@pytest.mark.parametrize("name,query,intent,fields,detail,consultation", CASES)
def test_current_screen_model_args_are_corrected(
        tmp_path, monkeypatch, name, query, intent, fields, detail, consultation):
    owner, values, sent = plugin_fixture(tmp_path)
    result = prepare(owner, monkeypatch, query, intent, fields, detail, consultation)
    record = last(owner)
    assert not result.get("is_error"), result
    assert record["outcome"] != "failed" and record["error_code"] is None
    assert record.get("corrections"), record
    assert {item.get("reason") for item in record["corrections"]} & {
        "read_screen_current_scope_dropped", "current_screen_subject_inferred"}
    assert not sent


@pytest.mark.parametrize("name,query,intent,fields,detail,consultation", BOUNDARY)
def test_explicit_subject_and_non_current_scopes_still_fail(
        tmp_path, monkeypatch, name, query, intent, fields, detail, consultation):
    owner, values, sent = plugin_fixture(tmp_path)
    result = prepare(owner, monkeypatch, query, intent, fields, detail, consultation)
    record = last(owner)
    assert result.get("is_error"), result
    assert record["outcome"] == "failed" and record["error_code"] in {
        "invalid_consultation_subject", "scope_intent_conflict"}
    assert not record.get("corrections"), record
