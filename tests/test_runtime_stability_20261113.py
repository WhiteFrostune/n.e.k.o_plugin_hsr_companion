"""Fault injection at the same boundaries used by the observer and followup."""
import asyncio
import importlib
import json
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from plugin.plugins.hsr_companion.adapters.local_vision import LocalVisionAdapter
from plugin.plugins.hsr_companion.runtime import answer_message, live_refresh, vision_scheduler
from plugin.plugins.hsr_companion.runtime import followup_delivery as followup
from plugin.plugins.hsr_companion.runtime.diagnostics import build_diagnostic_report
from plugin.plugins.hsr_companion.runtime.guidance import plan_guidance
from plugin.plugins.hsr_companion.runtime.observation import public_visual_evidence
from plugin.plugins.hsr_companion.runtime.query_plan import build_query_plan
from plugin.plugins.hsr_companion.runtime.vision_trace import public_trace
from plugin.plugins.hsr_companion.tests import test_guidance as cases
from plugin.plugins.hsr_companion.tests.test_current_observation import analysis, plugin_fixture
from plugin.plugins.hsr_companion.tests.test_diagnostics import inputs
from plugin.plugins.hsr_companion.tests.test_page_followup import followup_owner
from plugin.plugins.hsr_companion.tests.test_scene_latency import png
from plugin.sdk.plugin import unwrap

entry = importlib.import_module("plugin.plugins.hsr_companion")
book, catalog = cases.book, cases.catalog


def retained_packet():
    """Synthetic relic frame with the same shape as a retained observation."""
    return {"fresh": True, "read_scope": "full", "page_type": "relic_detail",
            "scene": {"verified": True}, "observed_at": datetime.now(timezone.utc).isoformat(),
            "characters": [], "screen_facts": {"facts": [
                {"kind": "equipment_name", "scope": "equipment", "label": "遗器名称", "value": "测试遗器·手套"},
                {"kind": "equipment_slot", "scope": "equipment", "label": "遗器部位", "value": "手部"},
                {"kind": "main_stat", "scope": "equipment", "label": "主词条·生命值", "value": "705"},
                {"kind": "sub_stat", "scope": "equipment", "label": "副词条·速度", "value": "25"},
            ], "field_checks": [], "observation_serial": 28}}


def request(book, catalog, *, query=None, duration=.08):
    query = query or "评价这件遗器适合真珠吗"
    base = build_query_plan(query, "assess_relic")
    consultation = {"scope": "current_relic", "subject_state": "specified", "character": "真珠"}
    selected = plan_guidance(query, base, book=book, catalog=catalog, consultation=consultation)
    plan = selected.observation_plan(base)
    start = time.monotonic()
    return followup.AnswerRequest("synthetic", query, "猫娘", time.time(), start,
        start + duration, "unknown", False, False, True, False, plan=plan, guidance_plan=selected,
        guidance_profile={})


def test_stop_returns_while_native_worker_is_held_then_next_read_recovers(tmp_path, monkeypatch):
    owner, _, sent = plugin_fixture(tmp_path)
    owner._assistant_process = SimpleNamespace(stop=lambda: None)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def analyze(payload):
        calls.append(payload)
        if len(calls) == 1:
            entered.set()
            assert release.wait(4)
        return analysis(scene="menu")

    owner._vision = SimpleNamespace(analyze=analyze)
    monkeypatch.setattr(vision_scheduler, "OCR_DRAIN_SECONDS", .05)

    async def run():
        await owner._apply_local_analysis(analysis(scene="menu"), image_fingerprint="old", source="assistant_local_ocr")
        first = asyncio.create_task(live_refresh.guarded_analysis(owner, png(), helper=False))
        owner._observer_task = first
        assert await asyncio.to_thread(entered.wait, 1)
        try:
            stopped = unwrap(await asyncio.wait_for(owner.stop_auto_observation(), .75))
            assert stopped["observer"]["state"] == "stopped"
            assert first.cancelled()
            retained = owner._draining_vision_worker
            assert retained is not None and not retained.done()
            assert not (await owner._current_visual_evidence())["fresh"]
            with pytest.raises(vision_scheduler.VisionWorkerBusy):
                await asyncio.wait_for(vision_scheduler.analyze_image(owner, b"retry"), .4)
            assert len(calls) == 1
            assert any(e.get("outcome") == "timeout" for e in public_trace(owner) if e["stage"] == "ocr_drain")
            count = len(sent)
            release.set()
            await asyncio.wait_for(asyncio.shield(retained), 1)
            assert len(sent) == count
            assert not (await owner._current_visual_evidence())["fresh"]
            assert (await asyncio.wait_for(vision_scheduler.analyze_image(owner, b"new"), 1))["ok"]
            assert calls[-1] == b"new" and len(calls) == 2
        finally:
            release.set()
            await asyncio.gather(first, return_exceptions=True)
            await live_refresh.drain_analysis_worker(owner)

    asyncio.run(run())


def test_stop_while_manual_ocr_is_running_rejects_its_late_result(tmp_path, monkeypatch):
    owner, _, _ = plugin_fixture(tmp_path)
    owner._observer_task = None
    owner._assistant_process = SimpleNamespace(stop=lambda: None)
    entered, release = threading.Event(), threading.Event()

    def read(payload):
        entered.set()
        assert release.wait(4)
        return analysis()

    owner._vision = SimpleNamespace(analyze=read)
    monkeypatch.setattr(vision_scheduler, "OCR_DRAIN_SECONDS", .05)

    async def run():
        manual = asyncio.create_task(vision_scheduler.analyze_image(owner, png()))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            assert unwrap(await asyncio.wait_for(owner.stop_auto_observation(), .75))["observer"]["state"] == "stopped"
            assert not manual.done()
            release.set()
            with pytest.raises(RuntimeError, match="画面已失效"):
                await asyncio.wait_for(manual, 1)
        finally:
            release.set()
            await asyncio.gather(manual, return_exceptions=True)

    asyncio.run(run())


def test_read_only_trace_distinguishes_held_backend_return_and_verification_failure(monkeypatch):
    adapter = LocalVisionAdapter(SimpleNamespace())
    entered, release = threading.Event(), threading.Event()

    def read(_):
        entered.set()
        assert release.wait(4)
        return "", []

    monkeypatch.setattr(adapter, "_load_backend", lambda: SimpleNamespace(extract_text_with_boxes=read))
    owner = SimpleNamespace(_vision=adapter)
    proof = {"text": "secret-value", "region": {"left": .1, "top": .1, "right": .3, "bottom": .2}}
    from plugin.plugins.hsr_companion.adapters import text_verification

    def verify(backend, *args, **kwargs):
        backend.extract_text_with_boxes(None)
        raise OSError("secret-value")

    monkeypatch.setattr(text_verification, "verify_text_regions", verify)

    async def run():
        task = asyncio.create_task(asyncio.to_thread(adapter.verify_text_regions, b"private-image", [proof]))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            events = public_trace(owner)
            assert any(e["stage"] == "ocr_backend_setup" and e["outcome"] == "returned" for e in events)
            assert [e["outcome"] for e in events if e["stage"] == "ocr_call"] == ["started"]
            assert adapter.status()["state"] == "busy"
            release.set()
            with pytest.raises(OSError, match="secret-value"):
                await asyncio.wait_for(task, 1)
            report = json.loads(build_diagnostic_report(**inputs(), vision_trace=public_trace(owner))["json_text"])
            events = report["vision_trace"]
            assert [e["outcome"] for e in events if e["stage"] == "ocr_call"] == ["started", "returned"]
            assert any(e["stage"] == "ocr_verification" and e.get("error_type") == "OSError" for e in events)
            assert "secret-value" not in str(report) and "private-image" not in str(report)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_missing_fresh_frame_has_its_own_trace():
    owner = SimpleNamespace(_assistant_client=SimpleNamespace(fetch_frame=lambda _: None))
    assert asyncio.run(live_refresh.fetch_recent_frame(owner, wait_seconds=0)) is None
    assert public_trace(owner)[-1]["stage"] == "capture_no_fresh_frame"


@pytest.mark.parametrize("change", [None, "item", "cancel", "new_question"])
def test_followup_checks_current_request_and_item_after_slow_socket(book, catalog, monkeypatch, change):
    raw = {"ok": True, "page": {"page_type": "relic_detail"},
           "scene": {"verified": True, "primary_state": "menu"}, "characters": [],
           "runtime": {"stable_scene": "menu", "stable_character_ids": []},
           "observed_at": datetime.now(timezone.utc).isoformat(), "observation_serial": 28,
           "source": "assistant_local_ocr", "screen_facts": retained_packet()["screen_facts"]}
    value = public_visual_evidence(raw, {"enabled": True, "state": "observing"})
    owner, sent = followup_owner(monkeypatch, value=value)
    owner._guidance_book, owner._catalog = book, catalog
    owner._committed_observation = raw
    owner._answer_query_generation = 0
    owner._check_message_path = entry.HsrCompanionPlugin._check_message_path.__get__(owner)
    entered, release = threading.Event(), threading.Event()
    probes = []

    def probe():
        probes.append(threading.get_ident())
        entered.set()
        assert release.wait(4)
        return {"available": True}

    owner._message_path_ready = probe
    pending = request(book, catalog, duration=10)
    owner._pending_answer = pending
    monkeypatch.setattr(answer_message, "bridge_preview", lambda text: text)

    async def end_after_iteration(target, _):
        followup.cancel_followup(target, "test_cleanup")

    monkeypatch.setattr(followup, "_wait_for_update", end_after_iteration)

    async def run():
        task = asyncio.create_task(followup.deliver_followup(owner, pending))
        try:
            end = asyncio.get_running_loop().time() + 1
            while not entered.is_set() and asyncio.get_running_loop().time() < end:
                await asyncio.sleep(.005)
            assert entered.is_set() and not task.done()
            assert len(probes) == 1 and probes[0] != threading.get_ident()
            if change == "item":
                owner._observation_serial += 1
                raw["observation_serial"] = owner._observation_serial
                next(f for f in raw["screen_facts"]["facts"] if f["kind"] == "equipment_name")["value"] = "另一件遗器"
            elif change == "cancel":
                followup.cancel_followup(owner, "user_cancelled")
            elif change == "new_question":
                owner._answer_query_generation = 1
            release.set()
            await asyncio.wait_for(task, 1)
            assert len(sent) == int(change is None)
            if sent:
                assert sent[0]["_message_transport"] == {"available": True}
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
