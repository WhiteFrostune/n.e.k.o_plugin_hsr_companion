"""S0 observability only: no live capture, deadline, routing, or speech changes."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from plugin.plugins.hsr_companion.runtime import vision_trace
from plugin.plugins.hsr_companion.runtime.diagnostics import build_diagnostic_report
from plugin.plugins.hsr_companion.tests.test_diagnostics import inputs

REQUEST = "a" * 32


def test_measure_carries_request_job_and_frame_into_native_thread():
    owner, backend = SimpleNamespace(), SimpleNamespace()

    async def run():
        with vision_trace.measure(owner, "query_wait", request_id=REQUEST, job_id=7, frame_seq=42):
            def native():
                with vision_trace.measure(backend, "ocr_call"):
                    return "unchanged result"
            assert await asyncio.to_thread(native) == "unchanged result"
        with vision_trace.measure(owner, "outside"):
            pass

    asyncio.run(run())
    native_events = vision_trace.public_trace(backend)
    assert len(native_events) == 2
    assert all((e["request_id"], e["job_id"], e["frame_seq"]) == (REQUEST, 7, 42) for e in native_events)
    assert "request_id" not in vision_trace.public_trace(owner)[-1]


@pytest.mark.parametrize("error", [TimeoutError("private text"), ValueError("private text"), asyncio.CancelledError()])
def test_measured_exception_is_not_replaced_and_always_has_terminal_receipt(error):
    owner = SimpleNamespace()
    with pytest.raises(type(error)) as caught:
        with vision_trace.measure(owner, "region_read", request_id=REQUEST):
            raise error
    assert caught.value is error
    event = vision_trace.public_trace(owner)[-1]
    assert event["outcome"] == "error"
    assert event["error_type"] == type(error).__name__
    assert event["request_id"] == REQUEST
    assert "private text" not in json.dumps(event)


def test_export_retains_correlation_but_not_unknown_fields_or_bad_identifiers():
    owner = SimpleNamespace()
    with vision_trace.measure(owner, "query_wait", request_id=REQUEST, job_id=7, frame_seq=42):
        pass
    events = vision_trace.public_trace(owner)
    events.append({"stage": "query_wait", "request_id": "private text", "job_id": True,
                   "frame_seq": float("inf"), "chat": "private text"})
    result = json.loads(build_diagnostic_report(**inputs(), vision_trace=events)["copy_text"])
    assert result["vision_trace"][0]["request_id"] == REQUEST
    assert result["vision_trace"][0]["job_id"] == 7
    assert result["vision_trace"][0]["frame_seq"] == 42
    assert "request_id" not in result["vision_trace"][-1]
    assert "private text" not in json.dumps(result)


def test_operations_keep_concurrent_requests_separate_and_do_not_add_io():
    from plugin.plugins.hsr_companion.runtime.vision_trace import traced_operation

    first, second = SimpleNamespace(), SimpleNamespace()

    @traced_operation("region_read")
    async def run(owner, gate):
        await gate.wait()
        return owner

    async def scenario():
        gate = asyncio.Event()
        with vision_trace.measure(first, "query_wait", request_id=REQUEST):
            a = asyncio.create_task(run(first, gate))
        with vision_trace.measure(second, "query_wait", request_id="b" * 32):
            b = asyncio.create_task(run(second, gate))
        gate.set()
        assert await a is first
        assert await b is second

    asyncio.run(scenario())
    assert {e.get("request_id") for e in vision_trace.public_trace(first)} == {REQUEST}
    assert {e.get("request_id") for e in vision_trace.public_trace(second)} == {"b" * 32}


@pytest.mark.parametrize("raises", [False, True])
def test_missing_read_and_internal_exception_stay_distinct_without_changing_answer(monkeypatch, raises):
    from plugin.plugins.hsr_companion.runtime import live_refresh, region_query
    from plugin.plugins.hsr_companion.runtime.query_plan import build_query_plan

    async def current():
        return {"status": "updating", "fresh": False}

    async def no_cache(*args, **kwargs):
        return None

    def failed_plan(*args):
        raise ValueError("private text")

    owner = SimpleNamespace(_observer_status={"enabled": True, "state": "observing"},
        _vision=SimpleNamespace(read_region=lambda *a, **kw: []),
        _committed_observation={"page_type": "character_detail"},
        _current_visual_evidence=current, _push_current_visual_context=lambda _: None)
    monkeypatch.setattr(live_refresh, "try_reuse", no_cache)
    if raises:
        monkeypatch.setattr(region_query, "region_plan", failed_plan)

    async def run():
        owner._observer_task = asyncio.create_task(asyncio.sleep(1))
        try:
            plan = build_query_plan("", "read_screen", screen_fields=["characters"])
            with vision_trace.measure(owner, "query_wait", request_id=REQUEST):
                assert await live_refresh.refresh_for_query(owner, timeout=.02, query_plan=plan) is None
            assert owner._last_refresh_diagnostics["outcome"] == "timeout"
        finally:
            owner._observer_task.cancel()
            await asyncio.gather(owner._observer_task, return_exceptions=True)

    asyncio.run(run())
    events = vision_trace.public_trace(owner)
    if raises:
        assert any(e["stage"] == "region_read_unavailable" and e["error_type"] == "ValueError" for e in events)
    else:
        assert any(e["stage"] == "region_plan_missing" for e in events)
        assert not any(e["stage"] == "region_read_unavailable" for e in events)
    assert "private text" not in json.dumps(events)


def test_manual_internal_failure_keeps_failed_state_and_records_original_error_type():
    from plugin.plugins.hsr_companion.runtime.manual_refresh import wait_for_observation

    async def broken():
        raise OSError("private text")

    async def run():
        owner = SimpleNamespace(_observer_status={"enabled": True, "state": "observing"},
            _manual_refresh_status={"state": "running", "requested_at": "2026-01-01T00:00:00+00:00"},
            _current_visual_evidence=broken)
        owner._observer_task = asyncio.create_task(asyncio.sleep(1))
        try:
            await wait_for_observation(owner, 3)
            assert owner._manual_refresh_status["state"] == "failed"
            events = vision_trace.public_trace(owner)
            assert any(e["stage"] == "manual_refresh_error" and e["error_type"] == "OSError" for e in events)
            assert any(e["stage"] == "manual_refresh_finished" and e["outcome"] == "failed" for e in events)
            assert len({e["request_id"] for e in events}) == 1
            assert "private text" not in json.dumps(events)
        finally:
            owner._observer_task.cancel()
            await asyncio.gather(owner._observer_task, return_exceptions=True)

    asyncio.run(run())


def test_candidate_counts_keep_parse_verification_and_commit_joinable_without_values():
    owner = SimpleNamespace()
    sample = {"characters": [{"name": "private text"}], "screen_facts": {"facts": [
        {"kind": "main_stat", "value": "private text"}, {"kind": "sub_stat", "value": "private text"}]}}
    with vision_trace.measure(owner, "analysis_worker", request_id=REQUEST):
        vision_trace.bind_trace(job_id=7, frame_seq=42)
        for stage in ("candidate_parsed", "candidate_verified", "candidate_committed"):
            vision_trace.record_candidate(owner, stage, sample)
    events = [e for e in vision_trace.public_trace(owner) if e["stage"].startswith("candidate_")]
    assert len(events) == 3
    assert all((e["request_id"], e["job_id"], e["frame_seq"]) == (REQUEST, 7, 42) for e in events)
    assert all((e["main_stats"], e["sub_stats"], e["character_count"]) == (1, 1, 1) for e in events)
    assert "private text" not in json.dumps(events)


def test_recording_failure_never_replaces_original_result_or_exception():
    class BrokenTrace:
        def append(self, event):
            raise OSError("diagnostic storage unavailable")

    owner = SimpleNamespace(_vision_events=BrokenTrace())
    with vision_trace.measure(owner, "query_wait", request_id=REQUEST):
        result = object()
    assert result is not None
    error = ValueError("original error")
    with pytest.raises(ValueError) as caught:
        with vision_trace.measure(owner, "query_wait", request_id=REQUEST):
            raise error
    assert caught.value is error


def test_historical_samples_distinguish_missing_test_condition_and_real_tool_results():
    from pathlib import Path
    data = json.loads((Path(__file__).parent / "fixtures/stability/p8_baseline.json").read_text(encoding="utf-8"))
    cases = {row["case"]: row for row in data["cases"]}
    unavailable = cases["old097-unexpired"]
    assert not unavailable["condition_valid"] and not unavailable["formal_tool_invoked"]
    assert unavailable["outcome"] == "precondition_not_observed"
    for name in ("old097-expired", "p8-expired"):
        sample = cases[name]
        assert sample["formal_tool_invoked"] and sample["condition_valid"]
        assert sample["outcome"] == "timeout" and sample["delivered_character_count"] == 0
        assert any(s.get("reason") == "stale_expired" and s["age_seconds"] > 60 for s in sample["stages"])
    warm = cases["p8-unexpired"]
    assert warm["outcome"] == "reused" and warm["delivered_character_count"] == 1
    assert warm["followup"]["state"] == "not_armed"
    assert any(s["stage"] == "reuse_validated" and s["original_age_seconds"] < 60 for s in warm["stages"])
    assert all("source_record_sha256" in row for row in data["cases"])
