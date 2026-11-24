"""F1 has presentation and recording effects only; no recognition permissions."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from plugin.plugins.hsr_companion.runtime import diagnostic_session as session
from plugin.plugins.hsr_companion.runtime.diagnostic_export import export_diagnostic
from plugin.plugins.hsr_companion.runtime.diagnostic_snapshot import MAX_BYTES, seal
from plugin.plugins.hsr_companion.runtime.diagnostics import build_diagnostic_report
from plugin.plugins.hsr_companion.tests.diagnostic_sections import section as diagnostic_section
from plugin.plugins.hsr_companion.tests.test_current_observation import plugin_fixture
from plugin.plugins.hsr_companion.tests.test_diagnostics import inputs
from plugin.sdk.plugin import Err, Ok

AT = "2026-11-24T12:02:47+00:00"
OLD = "2026-11-24T11:59:37+00:00"
ID = "a" * 32


def make(**extra):
    values = inputs()
    values.update(extra)
    return build_diagnostic_report(**values)


def test_single_canonical_object_floors_copy_and_export(tmp_path):
    report = make(query_timings={"state": "finished", "request_id": ID, "duration_ms": 7477.4, "events": []})
    canonical = report["snapshot"]
    assert canonical["schema_version"] == 2
    assert report["diagnostic_id"] == canonical["diagnostic_id"]
    assert [f["floor"] for f in canonical["floors"]] == [5, 4, 3, 2, 1]
    prefix, body = report["copy_text"].split("\n", 5)[:5], report["copy_text"].split("\n", 5)[5]
    assert len(prefix) == 5 and prefix[0].startswith("5F") and prefix[-1].startswith("1F")
    assert prefix == [f["summary"] for f in canonical["floors"]]
    assert json.loads(body) == canonical == json.loads(report["json_text"])
    assert "query_timings" not in report and "query_timings" not in canonical
    saved = export_diagnostic(tmp_path, report)
    assert saved["diagnostic_id"] == report["diagnostic_id"]
    assert json.loads(Path(saved["path"]).read_text(encoding="utf-8")) == canonical


def test_no_records_are_yellow_null_not_zero_or_success():
    s = make(observer={"enabled": False, "state": "stopped"})["snapshot"]
    assert diagnostic_section(s, 'current_runtime_health')["reason"] == "not_running"
    assert diagnostic_section(s, 'current_runtime_health')["level"] == "yellow"
    assert s["floors"][1]["health"]["level"] == "yellow"
    assert s["floors"][2]["metrics"]["detector_duration_ms"] is None
    assert diagnostic_section(s, 'query_timings')["duration_ms"] is None
    assert diagnostic_section(s, 'panel_witness')["state"] == "not_observed"
    assert diagnostic_section(s, 'session')["cross_process_restarts"] is None
    assert diagnostic_section(s, 'session')["restart_coverage"] == "coverage_unknown"


def test_old_audit_does_not_become_current_request_or_runtime_health():
    s = make(
        service_request={"request_id": ID, "started_at": AT, "state": "started"},
        last_tool={"at": OLD, "refresh_diagnostics": {"outcome": "ready", "wait_seconds": 0.01}},
        prepare_calls={
            "records": [
                {"request_id": ID, "started_at": AT, "outcome": "failed", "error_code": "screen_intent_conflict"}
            ]
        },
    )["snapshot"]
    assert diagnostic_section(s, 'last_query')["source_relation"] == "historical"
    assert diagnostic_section(s, 'latest_request_health')["level"] == "red"
    assert diagnostic_section(s, 'latest_request_health')["reason"] == "screen_intent_conflict"
    assert diagnostic_section(s, 'current_runtime_health')["level"] == "yellow"


def test_new_service_request_supersedes_older_prepare_record_without_inventing_success():
    s = make(
        service_request={"request_id": ID, "started_at": AT, "state": "started"},
        prepare_calls={
            "records": [{"request_id": "b" * 32, "started_at": OLD, "outcome": "returned", "query_state": "ready"}]
        },
    )["snapshot"]
    assert s["floors"][0]["metrics"]["selected_request_id"] == ID
    assert diagnostic_section(s, 'latest_request_health')["level"] == "yellow"


def test_fresh_runtime_can_coexist_with_failed_request():
    s = make(
        evidence={"fresh": True, "observed_at": AT},
        prepare_calls={
            "records": [{"request_id": ID, "started_at": AT, "outcome": "failed", "error_code": "internal_exception"}]
        },
    )["snapshot"]
    assert diagnostic_section(s, 'latest_request_health')["level"] == "red"
    assert diagnostic_section(s, 'current_runtime_health')["level"] == "green"


def test_witness_is_bounded_private_and_non_authoritative():
    s = make(
        panel_witness={
            "mount_id": ID,
            "panel_version": "0.9.7",
            "secret": "PRIVATE",
            "events": [
                {
                    "status_key": "panel.confirming",
                    "tone": "warning",
                    "reason": "ocr_timeout",
                    "sequence": i,
                    "rendered_at": AT,
                    "context_at": OLD,
                    "context_session_id": ID,
                    "message": "PRIVATE",
                }
                for i in range(30)
            ],
        }
    )["snapshot"]
    w = diagnostic_section(s, 'panel_witness')
    assert len(w["events"]) == 16 and w["events"][0]["sequence"] == 14
    assert w["events"][-1]["context_at"] == OLD and w["events"][-1]["rendered_at"] == AT
    assert w["authority"] == "display_only_not_answer_permission"
    assert "PRIVATE" not in json.dumps(s)


def test_build_sources_are_separate_and_do_not_repair_version():
    s = make(
        source_build={"version": "0.9.7", "revision": "0.9.7-source", "commit": "a" * 40},
        installation_stamp={"version": "0.9.8", "revision": "0.9.8-e2", "commit": "b" * 40},
        panel_witness={"panel_version": "0.9.7", "events": []},
    )["snapshot"]
    b = diagnostic_section(s, 'build_identity')
    assert b["source_constants"]["version"] == "0.9.7"
    assert b["installation_stamp"]["version"] == "0.9.8"
    assert b["panel_version"] == "0.9.7" and b["mismatch"]
    assert s["floors"][-1]["health"]["reason"] == "build_mismatch"


def test_unknown_codes_are_counted_without_exporting_free_strings():
    s = make(
        last_tool={
            "refresh_diagnostics": {
                "outcome": "SECRET",
                "stages": [{"stage": "SECRET", "reason": "SECRET", "error_type": "SecretError"}],
            }
        },
        observation_health={"patrol": {"decision": "SECRET", "reason": "SECRET"}},
        panel_witness={"events": [{"status_key": "SECRET", "reason": "SECRET"}]},
    )["snapshot"]
    assert "SECRET" not in json.dumps(s) and "SecretError" not in json.dumps(s)
    assert s["unmapped_codes"]["count"] >= 5
    assert diagnostic_section(s, 'last_query')["stages"][0]["stage"] == "unmapped_code"


def test_default_projection_excludes_chat_game_names_ids_paths_and_hashes():
    secret = "SECRET_USER_CHAT_OCR_NAME_UID_TOKEN_C:/private"
    s = make(
        prepare_calls={
            "records": [
                {
                    "request_id": ID,
                    "started_at": AT,
                    "stage": "pending_reply",
                    "outcome": "returned",
                    "arguments": {
                        "query": {"type": "str", "length": 40, "sha256": "c" * 64},
                        "pending_reply": {
                            "type": "dict",
                            "fields": {
                                "relation": {"type": "str", "value": "confirm"},
                                "player_message": {"type": "str", "value": secret, "sha256": "d" * 64},
                            },
                        },
                    },
                    "context": {"subject_ids": ["character.secret"], "execution_intent": "assess_relic"},
                    "origin": [{"file": "C:/private", "line": 1}],
                    "selection": {"evidence_sha256": "e" * 64},
                }
            ]
        },
        vision_trace=[{"stage": "region_plan_missing", "raw": secret}],
        observation_health={"recent_fulls": [{"job_id": 2, "state": "parsed", "name": secret}], "raw": secret},
    )
    output = s["copy_text"]
    assert secret not in output and "character.secret" not in output
    assert "sha256" not in output and "digest" not in output and "C:/private" not in output
    assert '"confirm"' in output and "assess_relic" in output
    assert "delivery_value_trace" not in output


def test_maximum_timing_data_is_bounded_by_utf8_bytes_without_sliced_json():
    events = [
        {
            "stage": "ocr_call",
            "event": "returned",
            "duration_ms": 3.2,
            "after_ms": i,
            "operation": "extract_text_with_boxes",
        }
        for i in range(256)
    ]
    report = make(query_timings={"state": "finished", "events": events, "request_id": ID})
    assert len(report["copy_text"].encode("utf-8")) <= MAX_BYTES
    assert json.loads(report["copy_text"].split("\n", 5)[5]) == report["snapshot"]
    assert report["snapshot"]["size_control"]["state"] in {"complete", "size_limited"}


def test_oversized_unicode_is_explicitly_size_limited_not_silently_truncated():
    s = copy.deepcopy(make()["snapshot"])
    diagnostic_section(s, 'query_timings')["events"] = [
        {"stage": "ocr_call", "event": "returned", "safe_test_padding": "界" * 3000} for _ in range(256)
    ]
    report = seal(s)
    assert report["snapshot"]["size_control"]["state"] == "size_limited"
    assert len(report["copy_text"].encode("utf-8")) <= MAX_BYTES
    assert json.loads(report["json_text"])["diagnostic_id"] == report["diagnostic_id"]


def test_export_rejects_copy_snapshot_id_mismatch(tmp_path):
    report = make()
    report["diagnostic_id"] = "b" * 32
    with pytest.raises(ValueError):
        export_diagnostic(tmp_path, report)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
@pytest.mark.parametrize("broken", [False, True])
def test_recording_preserves_return_exception_and_cancellation(monkeypatch, outcome, broken):
    owner = SimpleNamespace()
    result = object()
    if broken:

        def fail(*a, **k):
            raise ValueError("private recorder failure")

        monkeypatch.setattr(session, "note", fail)

    @session.observe_enable
    async def business(o):
        if outcome == "error":
            raise RuntimeError("original business exception")
        if outcome == "cancel":
            raise asyncio.CancelledError()
        return result

    if outcome == "success":
        assert asyncio.run(business(owner)) is result
    else:
        with pytest.raises(RuntimeError if outcome == "error" else asyncio.CancelledError):
            asyncio.run(business(owner))


def test_first_enable_success_records_stages_once_and_keeps_bounded_history():
    owner = SimpleNamespace()

    @session.observe_enable
    async def business(o):
        session.safe_note(o, "initialization", "completed")
        with session.step(o, "assistant_connection") as r:
            r["outcome"] = "connected"
        session.safe_note(o, "first_observation", "started")
        session.safe_note(o, "first_observation", "completed")
        session.safe_note(o, "first_commit", "started")
        session.safe_note(o, "first_commit", "completed")
        return Ok({"PRIVATE": "not recorded"})

    asyncio.run(business(owner))
    for _ in range(100):
        session.safe_note(owner, "first_commit", "completed")
    s = session.snapshot(owner)
    stages = {e["stage"]: e for e in s["attempts"][-1]["stages"]}
    assert stages["assistant_connection"]["outcome"] == "connected"
    assert stages["first_commit"]["outcome"] == "completed"
    assert s["attempts"][-1]["state"] == "accepted"
    assert len([e for e in s["recent_events"] if e["stage"] == "first_commit" and e["outcome"] == "completed"]) == 1
    assert "PRIVATE" not in json.dumps(s)
    for _ in range(12):
        asyncio.run(business(owner))
    assert len(session.snapshot(owner)["attempts"]) == 4
    assert len(session.snapshot(owner)["recent_events"]) <= 64


def test_first_enable_caught_failure_and_mid_stage_error_have_safe_causes():
    owner = SimpleNamespace()

    @session.observe_enable
    async def failed(o):
        session.safe_note(o, "initialization", "failed", error=RuntimeError("PRIVATE TOKEN"))
        return Err("PRIVATE")

    asyncio.run(failed(owner))
    assert session.snapshot(owner)["attempts"][-1]["state"] == "failed"
    with pytest.raises(TypeError):
        with session.step(owner, "assistant_connection"):
            raise TypeError("PRIVATE PATH")
    s = session.snapshot(owner)
    assert s["attempts"][-1]["stages"][-1]["exception_type"] == "TypeError"
    assert "PRIVATE" not in json.dumps(s)


def test_diagnostic_entry_is_read_only_includes_timing_and_does_not_read_value_trace(tmp_path, monkeypatch):
    owner, values, sent = plugin_fixture(tmp_path)
    owner._avatars = SimpleNamespace(status=lambda: {"installed": True})
    owner._message_path_ready = lambda: {"available": True}

    async def health():
        return {"connected": True}

    owner._assistant_status = health
    from plugin.plugins.hsr_companion.runtime import service_contract

    def forbidden(*a, **k):
        raise AssertionError("must not read values")

    monkeypatch.setattr(service_contract, "delivery_value_trace", forbidden)
    before = copy.deepcopy(values)
    report = asyncio.run(owner.diagnose_companion()).value["diagnostic"]
    assert diagnostic_section(report["snapshot"], 'query_timings')["state"] == "not_recorded"
    assert values == before and not sent


def test_export_selected_id_reuses_snapshot_without_second_diagnostic(tmp_path):
    owner, _, _ = plugin_fixture(tmp_path)
    report = make()
    owner._diagnostic_report_cache = report
    owner.data_path = lambda *parts: tmp_path.joinpath(*parts)

    async def forbidden():
        raise AssertionError("must not regenerate")

    owner.diagnose_companion = forbidden
    response = asyncio.run(owner.export_companion_diagnostic(diagnostic_id=report["diagnostic_id"]))
    assert response.value["export"]["diagnostic_id"] == report["diagnostic_id"]
    assert isinstance(asyncio.run(owner.export_companion_diagnostic(diagnostic_id="b" * 32)), Err)


def test_source_sampling_has_time_and_safe_failure_class_without_business_side_effects():
    from plugin.plugins.hsr_companion.runtime.diagnostic_reader import diagnostic_inputs

    async def bad():
        raise RuntimeError("PRIVATE TOKEN PATH")

    async def ready():
        return {}

    owner = SimpleNamespace(_assistant_status=bad, _read_tool_audit=ready, _current_visual_evidence=ready)
    samples = {}
    _, _, _, incomplete = asyncio.run(diagnostic_inputs(owner, samples=samples))
    report = make(source_samples=samples)
    assert incomplete == ["assistant"]
    assert diagnostic_section(report["snapshot"], 'sources')["assistant"]["outcome"] == "error"
    assert diagnostic_section(report["snapshot"], 'sources')["assistant"]["duration_ms"] >= 0
    assert "PRIVATE" not in report["copy_text"]


def test_same_process_instances_have_separate_sessions_and_no_cross_process_claim():
    first, second = SimpleNamespace(), SimpleNamespace()
    session.initialize(first, constructed=True)
    session.initialize(second, constructed=True)
    a, b = session.snapshot(first), session.snapshot(second)
    assert a["session_id"] != b["session_id"] and a["pid"] == b["pid"]
    assert b["same_process_instance_rebuilds"] == a["same_process_instance_rebuilds"] + 1
    assert b["cross_process_restarts"] is None
    lazy = session.snapshot(SimpleNamespace())
    assert lazy["instance_started_at"] is None
    assert lazy["instance_start_source"] == "first_diagnostic_use"


def test_actual_enable_preserves_success_and_failure_returns_when_recorder_breaks(tmp_path, monkeypatch):
    owner, values, _ = plugin_fixture(tmp_path)
    owner._observer_settings = {}
    owner._start_data_pack_task = lambda: None
    owner._start_observer_task = lambda: None

    async def settings():
        return {"proactive_enabled": False, "idle_chatter_enabled": False}

    async def companion(reason):
        return {"session": {}}

    async def operation(*a):
        return {}

    owner._read_observer_settings = settings
    owner._start_companion_session = companion
    owner._set_user_operation = operation

    def broken(*a, **k):
        raise OSError("diagnostic unavailable")

    monkeypatch.setattr(session, "note", broken)
    result = asyncio.run(owner.start_auto_observation())
    assert isinstance(result, Ok) and values["observer_settings"]["enabled"] is True

    async def fails():
        raise RuntimeError("original enable failure")

    owner._read_observer_settings = fails
    assert isinstance(asyncio.run(owner.start_auto_observation()), Err)


def test_size_boundary_uses_utf8_bytes_and_returns_explicit_limit(monkeypatch):
    from plugin.plugins.hsr_companion.runtime import diagnostic_snapshot as module

    source = copy.deepcopy(make()["snapshot"])
    source["padding"] = "界" * 20
    baseline = seal(copy.deepcopy(source))
    count = len(baseline["copy_text"].encode("utf-8"))
    assert count > len(baseline["copy_text"])
    monkeypatch.setattr(module, "TARGET_BYTES", count + 10000)
    # The limit number has the same digit width in the wire representation.
    source = copy.deepcopy(make()["snapshot"])
    source["padding"] = "界" * 1000
    baseline = seal(copy.deepcopy(source))
    exact = len(baseline["copy_text"].encode("utf-8"))
    monkeypatch.setattr(module, "MAX_BYTES", exact)
    accepted = seal(copy.deepcopy(source))
    assert len(accepted["copy_text"].encode("utf-8")) <= exact
    source["padding"] += "界" * 1000
    limited = seal(source)
    assert limited["snapshot"]["size_control"]["state"] == "size_limited"
    assert len(limited["copy_text"].encode("utf-8")) <= exact
    assert json.loads(limited["json_text"]) == limited["snapshot"]


def test_source_alignment_compares_instants_not_timezone_string_order():
    s = make(
        service_request={"request_id": ID, "state": "started", "started_at": "2026-11-24T20:00:00+08:00"},
        last_tool={"at": "2026-11-24T12:01:00+00:00"},
    )["snapshot"]
    assert diagnostic_section(s, 'last_query')["source_relation"] == "unmatched"


def test_old_pending_request_id_is_kept_but_message_is_not():
    old = "b" * 32
    s = make(
        prepare_calls={
            "records": [
                {
                    "request_id": ID,
                    "started_at": AT,
                    "last_completed_stage": "pending_reply",
                    "arguments": {
                        "pending_reply": {
                            "presence": "provided",
                            "type": "dict",
                            "fields": {
                                "request_id": {"type": "str", "value": old},
                                "relation": {"type": "str", "value": "confirm"},
                                "player_message": {"type": "str", "sha256": "c" * 64},
                            },
                        }
                    },
                }
            ]
        }
    )["snapshot"]
    path = s["floors"][0]["metrics"]["pending_reply_path"]
    assert path["present"] and path["old_request_id"] == old and path["relation"] == "confirm"
    assert "player_message" not in json.dumps(s) and "sha256" not in json.dumps(s)


def test_confirming_identity_is_yellow_even_if_independent_page_is_fresh():
    s = make(
        evidence={"fresh": True, "observed_at": AT},
        observation_health={
            "identity": {
                "confirmation": {
                    "state": "pending",
                    "reason": "insufficient_confirmations",
                    "elapsed_seconds": 12,
                    "limit_seconds": 70,
                }
            }
        },
    )["snapshot"]
    assert s["floors"][1]["health"]["level"] == "yellow"
    assert s["floors"][1]["health"]["reason"] == "confirming_identity"


def test_enable_error_is_red_in_floor_one_without_changing_observer():
    owner = SimpleNamespace()
    session.begin_enable(owner)
    session.safe_note(owner, "initialization", "failed", error=RuntimeError("PRIVATE"))
    s = make(session=session.snapshot(owner))["snapshot"]
    assert s["floors"][-1]["health"]["level"] == "red"
    assert s["floors"][-1]["health"]["reason"] == "enable_stage_failed"
    assert diagnostic_section(s, 'observer')["state"] == "observing"


def test_five_summary_lines_include_numbers_without_inventing_missing_metrics():
    report = make(query_timings={"state": "finished", "duration_ms": 7500, "events": []})
    lines = report["copy_text"].split("\n", 5)[:5]
    assert "7500" in lines[0]
    assert "提交年龄=未记录" in lines[1]
    assert "最长已记原生OCR=未记录" in lines[3]
