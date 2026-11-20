"""R2 scheduling only: original D costs, synthetic frames and native readers.

No live capture/installation/cat acceptance. A learned scheduling baseline is
not a fact receipt and must never certify a role, number or equipment object.
"""
import asyncio
import copy
import hashlib
import json
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

import pytest
from plugin.plugins.hsr_companion.adapters.local_vision import LocalVisionAdapter, serial_ocr
from plugin.plugins.hsr_companion.adapters.text_reader import check_current
from plugin.plugins.hsr_companion.adapters.text_verification import proof_key
from plugin.plugins.hsr_companion.runtime import live_refresh, page_handoff
from plugin.plugins.hsr_companion.tests import test_bounded_page_handoff_20261021 as original
from plugin.plugins.hsr_companion.tests.test_delivery_fix_20261012 import frame, picture
from plugin.plugins.hsr_companion.tests.test_review_policy_20261017 import resident_owner

offline = original.offline
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def retained_costs(require_live_artifact):
    require_live_artifact("dist/roundtrip-d-live-20261021-2.json")
    source = (ROOT / "dist/roundtrip-d-live-20261021-2.json").read_bytes()
    assert hashlib.sha256(source).hexdigest() == "42b85cb008812fac790ecfb3aa862b8ebd695c64b55bd674df187a38adcef153"
    record = json.loads(source)
    work = [e for e in record["pipeline_trace"] if e["kind"] == "checkpoint_work"
            and e["callback"] == "queue_page_check.<locals>.check"]
    costs = [e["seconds"] for e in work[:6]]
    assert costs == [1.672, 1.422, 1.36, 1.281, 1.391, 1.438]
    return costs


def scheduler(pages=("character_detail",), *, costs=(), change_on_read=False, empty_controls=False):
    active = {"payload": picture("page"), "seq": 1}
    calls = []

    def capture(_):
        active["seq"] += 1
        value = frame(active["payload"])
        value.frame_seq = active["seq"]
        return value

    def read(payload, **kw):
        index = len(calls)
        calls.append(payload)
        if index < len(costs):
            time.sleep(costs[index])
        if change_on_read:
            active["payload"] = picture()
        value = original.preview(pages[min(index, len(pages) - 1)])
        if empty_controls:
            value["recognition"]["scene_regions"] = []
        return value

    def submit(callback):
        result = Future()
        result.set_result(callback())
        return result

    owner = SimpleNamespace(_observer_status={"enabled": True}, _visual_query_waiters=1,
        _query_refresh_generation=1, _answer_query_generation=1,
        _query_refresh_deadline=time.monotonic() + 35, _analysis_game_pid=1,
        _vision=SimpleNamespace(probe_scene=read, enqueue_review=submit),
        _assistant_client=SimpleNamespace(fetch_frame=capture))
    return owner, active, calls


def probe(owner, stop, *, source=None, expected="character_detail"):
    budget = page_handoff.check_budget(owner)
    budget["next_at"] = 0  # Accelerate cooldown, not the original request deadline.
    result = page_handoff.queue_page_check(owner, picture() if source is None else source,
        original.preview(expected), stop)
    return result.result() if result is not None else None


def test_six_new_acquisitions_of_same_checked_page_do_not_repeat_ocr(retained_costs, record_property):
    owner, _, calls = scheduler(costs=retained_costs)
    stop = threading.Event()
    deadline = owner._query_refresh_deadline
    results = [probe(owner, stop) for _ in range(6)]
    assert len(calls) == 1, "Confirmed same-page baseline still compares against the old source forever"
    assert results[0]["reason"] == "same_page"
    assert all(r["reason"] == "controls_unchanged" for r in results[1:])
    budget = page_handoff.check_budget(owner)
    assert budget["attempts"] == 1 and budget["native_calls"] == 1
    # Windows monotonic clock granularity can report 1.671 for a 1.672 sleep.
    # This measures accounting, not the request's unchanged 8/35s deadline.
    assert budget["native_seconds"] >= retained_costs[0] - .02
    assert owner._query_refresh_deadline == deadline
    record_property("same_page_cost", json.dumps({"probe_invocations": len(calls),
        "native_seconds": round(budget["native_seconds"], 3),
        "original_six_checkpoint_seconds": round(sum(retained_costs), 3), "new_frames": 6}))


def test_control_only_checks_do_not_spend_native_probe_opportunities():
    owner, active, calls = scheduler(pages=("challenge_setup",))
    active["payload"] = picture()
    stop = threading.Event()
    for _ in range(9):
        result = probe(owner, stop)
        assert result["reason"] == "controls_unchanged"
    assert page_handoff.check_budget(owner)["attempts"] == 0
    active["payload"] = picture("page")
    assert probe(owner, stop)["reason"] == "confirmed_transition"
    assert len(calls) == 1


def test_same_page_baseline_does_not_permanently_hide_later_real_switch():
    owner, active, calls = scheduler(pages=("character_detail", "challenge_setup"))
    stop = threading.Event()
    assert probe(owner, stop)["reason"] == "same_page"
    active["payload"] = picture()  # Changed relative to the CHECKED baseline, not source.
    result = probe(owner, stop)
    assert result["reason"] == "confirmed_transition", result
    assert result["to_page"] == "challenge_setup" and len(calls) == 2


def test_unknown_is_not_learned_as_a_stable_same_page():
    owner, _, calls = scheduler(pages=("unknown", "challenge_setup"))
    stop = threading.Event()
    assert probe(owner, stop)["reason"] == "unknown_page"
    assert probe(owner, stop)["reason"] == "confirmed_transition"
    assert len(calls) == 2


@pytest.mark.parametrize("cause", ["missing_controls", "controls_changed"])
def test_same_page_baseline_needs_its_own_stable_controls(cause):
    owner, _, _ = scheduler(empty_controls=cause == "missing_controls", change_on_read=cause == "controls_changed")
    result = probe(owner, threading.Event())
    assert result["reason"] == ("page_controls_missing" if cause == "missing_controls" else "controls_changed")


def test_learned_baseline_neither_commits_fields_nor_survives_new_producer():
    owner, active, calls = scheduler(pages=("character_detail", "challenge_setup"))
    old = threading.Event()
    assert probe(owner, old)["reason"] == "same_page"
    assert not getattr(owner, "_scene_preview", None)
    assert not getattr(owner, "_committed_observation", None)
    old.set()
    active["payload"] = picture()
    result = probe(owner, threading.Event(), source=picture("page"))
    assert result["reason"] == "confirmed_transition" and len(calls) == 2
    assert page_handoff.check_budget(owner)["attempts"] == 2  # Producer replacement does not reset cap.


def test_attempt_limit_is_native_calls_not_new_frame_count():
    owner, _, calls = scheduler(pages=("unknown",))
    stop = threading.Event()
    for _ in range(8):
        probe(owner, stop)
    assert len(calls) == page_handoff.MAX_PAGE_CHECKS == 6
    budget = page_handoff.check_budget(owner)
    assert budget["native_calls"] == 6 and budget["native_seconds"] >= 0


def test_possible_consumer_without_claim_does_not_reserve_a_timed_ocr_slot(offline, monkeypatch):
    owner = SimpleNamespace(_visual_query_waiters=1, _observation_serial=7,
        _observation_epoch=1, _last_full_commit_serial=7)
    waits = []
    real_wait_for = asyncio.wait_for

    async def track(awaitable, timeout):
        waits.append(timeout)
        # Retain the failure without wasting its speculative .75-second timer.
        if timeout == .75:
            awaitable.close()
            return None
        return await real_wait_for(awaitable, timeout)

    monkeypatch.setattr(asyncio, "wait_for", track)
    offline.run(live_refresh.wait_delivery_turn(owner))
    assert .75 not in waits, "An absent consumer still takes priority over producing the new page"


def test_actual_consumer_gets_the_turn_and_holds_native_admission(offline):
    owner = SimpleNamespace(_visual_query_waiters=1, _observation_serial=7,
        _observation_epoch=1, _last_full_commit_serial=7)

    async def run():
        admission = asyncio.create_task(live_refresh.wait_delivery_turn(owner))
        await asyncio.sleep(0)
        assert not admission.done()
        async with live_refresh.delivery_turn(owner):
            await asyncio.sleep(0)
            assert not admission.done()
            live_refresh.mark_delivery_verified(owner)
        await admission
        assert owner._delivery_seen_revision == (1, 7)
        assert owner._delivery_waiters == 0

    offline.run(run())


def test_public_tool_receives_new_page_with_original_recorded_probe_costs(
        tmp_path, offline, monkeypatch, retained_costs, record_property):
    """Actual serial/checkpoint/producer/cache/public-entry path; fake OCR only."""
    monkeypatch.setattr(page_handoff, "PAGE_CHECK_INTERVAL_SECONDS", .75)

    async def run():
        from plugin.plugins.hsr_companion.tests.test_chain_audit_20261017 import simple_raw
        owner, _, _, _ = await resident_owner(tmp_path)
        previous = copy.deepcopy(owner._committed_observation)
        current = simple_raw("attributes", "stat", "防御力", "3335")
        entered, release = threading.Event(), threading.Event()
        active = {"changed": False, "seq": 1}
        calls, threads = [], []

        class Vision(LocalVisionAdapter):
            @serial_ocr(cooperative=True)
            def analyze(self, payload, *, cancelled=None, on_scene=None, wants_relic=None, yield_requested=None):
                threads.append(threading.get_ident())
                if payload == picture():
                    on_scene(previous)
                    entered.set()
                    while not release.wait(.002):
                        check_current(cancelled)
                    calls.append("old_completed")
                    return copy.deepcopy(previous)
                on_scene(original.preview("character_detail"))
                calls.append("new_completed")
                return copy.deepcopy(current)

            @serial_ocr
            def probe_scene(self, payload, *, cancelled=None):
                index = calls.count("probe")
                calls.append("probe")
                threads.append(threading.get_ident())
                time.sleep(retained_costs[index])
                check_current(cancelled)
                return original.preview("unknown" if index == 0 else "character_detail")

            @serial_ocr
            def verify_text_regions(self, payload, proofs, **kw):
                return {proof_key(p) for p in proofs} if "new_completed" in calls else set()

        def capture(_):
            value = frame(picture("page") if active["changed"] else picture())
            value.frame_seq = active["seq"]
            return value

        def health():
            active["seq"] += 1
            return {"connected": True, "frame_seq": active["seq"], "game": {"pid": 1, "running": True},
                    "capture": {"state": "capturing", "isolated_window": True}}

        owner._vision = Vision(None)
        owner._assistant_client = SimpleNamespace(fetch_frame=capture, health=health)

        async def resident():
            assert await live_refresh.guarded_analysis(owner, picture(), helper=True) is None
            value = await live_refresh.guarded_analysis(owner, picture("page"), helper=True)
            assert value is not None
            await owner._apply_local_analysis(value, image_fingerprint="r2-new-frame", source="assistant_local_ocr", deliver_event=False)

        producer = asyncio.create_task(resident())
        assert await asyncio.to_thread(entered.wait, 1)
        active["changed"] = True
        try:
            result = await owner.hsr_companion_prepare(query="读取当前面板", intent="read_screen", screen_fields=["attributes"])
            await asyncio.wait_for(producer, 8)
            assert result["query_answer"]["state"] == "ready", result.get("query_timing")
            assert result["current_visual_evidence"]["page_type"] == current["page"]["page_type"]
            assert any(f["value"] == "3335" for f in result["current_visual_evidence"]["screen_facts"]["facts"])
            assert "old_completed" not in calls and calls.count("probe") == 2
            # The NEW producer may use a new executor thread, but its native
            # gate never overlaps the old producer's checkpoint work.
            assert threads[0] == threads[1] == threads[2]
            assert not result["followup"]["pending"]
            record_property("handoff_delivery", json.dumps({
                "query_seconds": result["query_timing"]["refresh_wait_seconds"],
                "field_state": result["query_answer"]["state"], "probe_invocations": calls.count("probe"),
                "native_seconds": round(owner._page_check_budget["native_seconds"], 3),
                "old_task_committed": "old_completed" in calls, "followup_pending": result["followup"]["pending"]}))
        finally:
            release.set()
            await asyncio.gather(producer, return_exceptions=True)
            await live_refresh.drain_analysis_worker(owner)

    offline.run(run())
