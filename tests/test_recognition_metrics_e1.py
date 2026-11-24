
import json
from types import SimpleNamespace

from plugin.plugins.hsr_companion.runtime.diagnostics import build_diagnostic_report
from plugin.plugins.hsr_companion.runtime.engine import CompanionRuntime
from plugin.plugins.hsr_companion.runtime.recognition_metrics import (
    PASS_LIMIT,
    observation_health,
    record_full,
    record_late_discard,
)
from plugin.plugins.hsr_companion.tests.diagnostic_sections import section as diagnostic_section


def actor(entity_id):
    return {"entity_id": entity_id, "identity_scope": "team_slot", "slot": 1}


def menu(*characters):
    return {"scene": {"verified": True, "primary_state": "menu", "confidence": "high"}, "characters": characters}


def test_identity_history_keeps_cause_without_changing_confirmation():
    runtime = CompanionRuntime()
    runtime.ingest(menu(actor("private-first")))
    assert runtime.identity_diagnostics()["tracked_counts"] == [1]
    runtime.ingest(menu(actor("private-second")))
    assert runtime.identity_diagnostics()["recent_resets"][-1]["reason"] == "slot_replaced"
    assert runtime.identity_diagnostics()["tracked_counts"] == [1]
    runtime.ingest(menu(actor("private-second")))
    assert runtime.snapshot()["stable_character_ids"] == ["private-second"]
    runtime.ingest(menu())
    runtime.ingest(menu())
    resets = runtime.identity_diagnostics()["recent_resets"]
    assert [row["reason"] for row in resets] == ["slot_replaced", "empty_character_list", "missing_frame_limit"]
    assert runtime.snapshot()["stable_character_ids"] == []
    assert "private-" not in json.dumps(runtime.identity_diagnostics())
    runtime.ingest(menu(actor("private-third")))
    runtime.invalidate_observation(reason="new_page_title")
    assert runtime.identity_diagnostics()["recent_resets"][-1]["reason"] == "new_page_title"


def test_observation_metrics_export_is_bounded_and_distinguishes_discard_types():
    owner = SimpleNamespace(
        _patrol_status={
            "decision": "wait",
            "reason": "demand",
            "fallback": True,
            "checks": 4,
            "suppressed": 1,
            "raw": "private text",
        }
    )
    for seq in range(12):
        record_full(
            owner,
            job_id=seq,
            state="parsed",
            analysis={"recognition": {"processing_seconds": 13.7}, "private": "ignore"},
        )
    record_full(owner, job_id=11, state="yielded", checkpoint="post_ocr")
    record_late_discard(owner)
    payload = observation_health(owner, {"age_seconds": 59.5, "characters": [{"name": "private name"}]})
    assert payload["discarded_for_query"] == payload["discarded_at_30s_gate"] == 1
    assert len(payload["recent_fulls"]) == PASS_LIMIT
    assert payload["recent_fulls"][-1]["processing_seconds"] == 13.7
    assert payload["recent_fulls"][-1]["state"] == "yielded"
    assert payload["patrol"]["fallback"] is True
    assert payload["current_age_seconds"] == 59.5
    report = build_diagnostic_report(
        observer={},
        assistant={},
        evidence={},
        data_pack={},
        avatar_pack={},
        last_tool={},
        speech={},
        message_path={},
        observation_health=payload,
    )
    assert diagnostic_section(json.loads(report["json_text"]), 'observation_health') == payload
    assert "private" not in report["copy_text"]
