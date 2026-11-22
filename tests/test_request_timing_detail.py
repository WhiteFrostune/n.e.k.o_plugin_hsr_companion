"""Diagnostic spans must not change callbacks, ordering, deadlines or values."""
import asyncio
import json
import threading
from collections import deque
from types import SimpleNamespace

import pytest
from plugin.plugins.hsr_companion.runtime import timing_detail as detail
from plugin.plugins.hsr_companion.runtime.vision_trace import measure

RID = "d" * 32


def test_records_only_fixed_timing_and_status_fields():
    owner = SimpleNamespace(_vision=SimpleNamespace())
    with detail.capture(owner, RID):
        with detail.phase("region_page", owner, query="private utterance", text="private OCR", value=123,
                          request_id=RID, job_id=4, frame_seq=9):
            pass
    value = detail.snapshot(owner)
    assert value["request_id"] == RID and value["state"] == "finished"
    assert len(value["events"]) == 2 and value["events"][-1]["duration_ms"] >= 0
    assert value["events"][-1]["job_id"] == 4 and value["events"][-1]["frame_seq"] == 9
    assert "private" not in json.dumps(value) and "123" not in json.dumps(value)


def test_already_running_native_span_is_adopted_with_a_negative_start():
    adapter = SimpleNamespace()
    owner = SimpleNamespace(_vision=adapter)
    entered, release = threading.Event(), threading.Event()

    def native():
        with measure(adapter, "ocr_call", job_id=8, operation="extract_text_with_boxes"):
            entered.set()
            assert release.wait(3)

    thread = threading.Thread(target=native)
    thread.start()
    try:
        assert entered.wait(1)
        with detail.capture(owner, RID):
            release.set()
            thread.join(2)
        assert not thread.is_alive()
    finally:
        release.set()
        thread.join(2)
    events = detail.snapshot(owner)["events"]
    starts = [e for e in events if e["event"] == "started"]
    assert starts[0]["adopted"] and starts[0]["after_ms"] <= 0
    assert starts[0]["job_id"] == 8
    assert events[-1]["event"] == "returned" and events[-1]["span"] == starts[0]["span"]


def test_late_native_completion_survives_closed_request_scope():
    adapter = SimpleNamespace()
    owner = SimpleNamespace(_vision=adapter)
    with detail.capture(owner, RID):
        native = detail.phase("ocr_call", adapter)
        native.__enter__()
    native.__exit__(None, None, None)
    value = detail.snapshot(owner)
    assert value["state"] == "finished" and value["events"][-1]["event"] == "returned"


@pytest.mark.parametrize("failure", [RuntimeError("secret"), TimeoutError("secret"), asyncio.CancelledError()])
def test_preserves_exception_identity_and_cancellation(failure):
    owner = SimpleNamespace()
    with detail.capture(owner, RID), pytest.raises(type(failure)) as caught:
        with detail.phase("region_exit", owner):
            raise failure
    assert caught.value is failure
    result = detail.snapshot(owner)
    assert "secret" not in json.dumps(result)
    assert result["events"][-1]["event"] in {"error", "timeout", "cancelled"}


def test_bound_is_explicit_and_does_not_grow_legacy_ring():
    owner = SimpleNamespace(_vision_events=deque(maxlen=48))
    with detail.capture(owner, RID):
        for _ in range(detail.MAX_EVENTS):
            with measure(owner, "query_exit"):
                pass
    result = detail.snapshot(owner)
    assert len(result["events"]) == detail.MAX_EVENTS and result["dropped_events"] > 0
    assert owner._vision_events.maxlen == 48


def test_timing_failure_cannot_replace_success_or_retry_it(monkeypatch):
    owner = SimpleNamespace()
    calls = []

    def failed(*args, **kwargs):
        raise OSError("private path")

    monkeypatch.setattr(detail._Timeline, "add", failed)
    with detail.capture(owner, RID):
        with detail.phase("region_packet", owner):
            calls.append(1)
    assert calls == [1]
    assert not owner._timing_detail_state.active


def test_concurrent_request_ids_do_not_replace_each_other():
    owner = SimpleNamespace()
    first = None
    with detail.capture(owner, RID):
        first = owner._last_query_timing
        with detail.phase("region_page", owner):
            pass
        with detail.capture(owner, "a" * 32):
            with detail.phase("region_exit", owner):
                pass
        with detail.phase("region_page", owner):
            pass
    assert first.request_id == RID
    assert sum(e["stage"] == "region_page" for e in first.events) == 4


def test_queued_callback_keeps_request_sink_without_changing_its_return():
    owner = SimpleNamespace()
    with detail.capture(owner, RID):
        callback = detail.timed_callback("checkpoint_review", owner)(lambda value: value)
    # A producer thread has no request context; the queued callback retains
    # only its diagnostic destination, not a new request/deadline.
    returned = []
    thread = threading.Thread(target=lambda: returned.append(callback(42)))
    thread.start()
    thread.join(2)
    assert returned == [42]
    events = detail.snapshot(owner)["events"]
    assert events[-1]["stage"] == "checkpoint_review" and events[-1]["event"] == "returned"
    assert detail._CAPTURE.get() is None


def test_unknown_stage_does_not_evaluate_or_mutate_application_values():
    owner = SimpleNamespace()
    with detail.capture(owner, RID):
        with detail.phase("unregistered", owner, role=["private"]):
            result = object()
    assert result is not None and detail.snapshot(owner)["events"] == []
