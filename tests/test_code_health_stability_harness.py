"""Acceptance harness correctness only; not another recognition trial."""
import asyncio
import copy
import time
from types import SimpleNamespace

import pytest

from plugin.plugins.hsr_companion.scripts import verify_code_health_stability as harness


@pytest.mark.parametrize("context,expected", [
    ({"plugin": {"runtime_enabled": False}, "state": {}}, False),
    ({"state": {"observer": {"enabled": True}}}, True),
    ({"state": {"observer": {"enabled": False}}}, False),
])
def test_observer_state_uses_current_response(context, expected):
    assert harness.observer_enabled(context) is expected


def test_unknown_observer_state_is_not_assumed_stopped():
    with pytest.raises(RuntimeError, match="Observer state unavailable"):
        harness.observer_enabled({"state": {}})


def test_packet_projection_excludes_archives_without_changing_actual_answer():
    packet = {"player_context": {"uid": "private"}, "guidance_answer": {
        "imported_builds": {"uid": "private"}, "completion": {"evaluation_complete": True},
        "reply_plan": {"conclusion": "本题结论"}}, "equipment_answer": {"main_stat": {"value": "25"}}}
    before = copy.deepcopy(packet)
    selected = harness.packet_view(packet)
    assert "private" not in str(selected)
    assert selected["guidance_answer"]["reply_plan"] == packet["guidance_answer"]["reply_plan"]
    assert selected["equipment_answer"] == packet["equipment_answer"] and packet == before


@pytest.mark.parametrize("partial_owner", [True, False])
def test_cleanup_attempts_restore_even_if_worker_and_adapter_fail(monkeypatch, partial_owner):
    from plugin.plugins.hsr_companion.runtime import live_refresh
    calls = []

    async def failed_drain(owner):
        calls.append("drain")
        raise RuntimeError("controlled worker failure")

    def failed_close():
        calls.append("close")
        raise RuntimeError("controlled adapter failure")

    def action(host, name):
        calls.append(name)

    monkeypatch.setattr(live_refresh, "drain_analysis_worker", failed_drain)
    monkeypatch.setattr(harness, "stop_owned", lambda process: calls.append("stop_owned"))
    monkeypatch.setattr(harness, "ui_action", action)
    monkeypatch.setattr(harness, "live_context", lambda: {"state": {"observer": {"enabled": True}}})
    owner = SimpleNamespace(_pending_answer=None) if partial_owner else None
    result = {"restored": False}
    asyncio.run(harness.cleanup(owner, SimpleNamespace(close=failed_close), None,
        enabled=True, pause_attempted=True, result=result))
    assert calls[-2:] == ["stop_owned", "start_auto_observation"]
    assert result["restored"] and result["cleanup_errors"]


def test_numeric_readiness_does_not_certify_evaluation_or_cat_acceptance():
    row = {"packet": {"query_answer": {"state": "ready"},
                      "request_completion": {"service_ready": False}}, "followups": []}
    result = harness.verdict(row)
    assert result["direct_ready"] and not result["service_completed"]
    assert not result["followup_submitted_complete"] and not result["cat_acceptance"]
    assert result["truth_check"] == "pending_human_confirmation"


def test_transition_cannot_be_triggered_by_frame_changes_without_human_ack(tmp_path):
    owner = SimpleNamespace(_D_abort_marker=tmp_path / "abort")
    events = []

    async def run():
        task = asyncio.create_task(harness.acknowledged_transition(owner, tmp_path, "attributes",
            lambda kind, **kw: events.append((kind, kw))))
        await asyncio.sleep(.02)
        assert events and not task.done()
        # A freshly read page or arbitrary animation cannot substitute for this
        # marker, which the operator writes only after the user's reply.
        (tmp_path / "attributes_ack").touch()
        await asyncio.wait_for(task, 1)

    asyncio.run(run())


def test_transition_abort_is_preserved_without_waiting_for_destination(tmp_path):
    owner = SimpleNamespace(_D_abort_marker=tmp_path / "abort")
    owner._D_abort_marker.touch()
    before = time.monotonic()
    with pytest.raises(RuntimeError, match="Operator aborted"):
        asyncio.run(harness.acknowledged_transition(owner, tmp_path, "relic", lambda *a, **kw: None))
    assert time.monotonic() - before < 1
