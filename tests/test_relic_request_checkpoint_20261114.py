"""Replay a late question against the actual producer, with controlled OCR costs."""

import asyncio
import threading
import time
from datetime import timedelta
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw
from plugin.plugins.hsr_companion.adapters.local_vision import LocalVisionAdapter
from plugin.plugins.hsr_companion.runtime import live_refresh
from plugin.plugins.hsr_companion.runtime.equipment import equipment_card
from plugin.plugins.hsr_companion.runtime.query_plan import build_query_plan
from plugin.plugins.hsr_companion.runtime.vision_trace import public_trace
from plugin.plugins.hsr_companion.tests.test_current_observation import plugin_fixture
from plugin.plugins.hsr_companion.tests.test_experience_fixes import box
from plugin.plugins.hsr_companion.tests.test_relic_fields import relic_boxes


@pytest.mark.parametrize("scenario", ["complete", "partial", "switched"])
def test_late_relic_question_does_not_wait_for_unrelated_line_reread(tmp_path, monkeypatch, scenario):
    owner, _, _ = plugin_fixture(tmp_path)
    input_stream = BytesIO()
    Image.new("RGB", (1280, 800), (40, 48, 54)).save(input_stream, format="PNG")
    data = input_stream.getvalue()
    missing = scenario == "partial"
    changed = Image.new("RGB", (1280, 800), (40, 48, 54))
    ImageDraw.Draw(changed).rectangle((0, 0, 600, 100), fill="white")
    target_stream = BytesIO()
    changed.save(target_stream, format="PNG")
    target_data = target_stream.getvalue() if scenario == "switched" else data
    rows = [box("遗器详情", 20, 40), *relic_boxes()]
    if missing:
        rows.pop(-2)  # Keep the label, so row order cannot promote a substat.
    entered, release, reread = threading.Event(), threading.Event(), threading.Event()
    finish_reread = threading.Event()
    calls = []

    class Backend:
        def extract_text_with_boxes(self, image):
            calls.append(image.size)
            if image.size == (768, 480):
                return "", []
            if image.size == (1280, 800):
                entered.set()
                assert release.wait(3)
                return "", rows
            # The existing stat-table check starts at the detail column.
            if image.height == 220 and image.width <= 325:
                return "", []
            reread.set()
            assert finish_reread.wait(5)
            return "", []

    owner._vision = LocalVisionAdapter(owner._catalog)
    backend = Backend()
    monkeypatch.setattr(owner._vision, "_get_backend", lambda: backend)
    real_clock = time.monotonic
    clock_offset = 0.
    monkeypatch.setattr(live_refresh, "time", SimpleNamespace(
        monotonic=lambda: real_clock() + clock_offset, time=time.time))
    monkeypatch.setattr(live_refresh, "LIVE_POLL_SECONDS", .01)

    def frame(_seq=0):
        return SimpleNamespace(image_bytes=target_data, frame_seq=22, timestamp=time.time(),
                               game={"running": True, "pid": 1},
                               capture={"state": "capturing", "isolated_window": True})

    owner._assistant_client = SimpleNamespace(fetch_frame=frame)
    # Hold the observer's source constant during OCR; simulate the switch on
    # the independent validation capture so the old rows actually get parsed.
    async def validation_frame(_owner):
        if scenario == "switched":
            # The new frame has no matching title in the old proof region.
            monkeypatch.setattr(backend, "recognize_line", lambda image: [], raising=False)
        return frame()

    owner._assistant_client.fetch_frame = lambda _seq: SimpleNamespace(
        image_bytes=data, frame_seq=21, timestamp=time.time(),
        game={"running": True, "pid": 1}, capture={"state": "capturing", "isolated_window": True})
    monkeypatch.setattr(live_refresh, "fetch_recent_frame", validation_frame)
    owner._analysis_capture_at = (live_refresh.datetime.now(live_refresh.timezone.utc) - timedelta(seconds=14)).isoformat()
    owner._analysis_input_frame_seq = 21
    query = build_query_plan("这件遗器的主词条和副词条分别是什么？", "read_screen", screen_fields=["equipment"],
                             response_detail="full")

    async def run():
        nonlocal clock_offset

        async def produce():
            value = await live_refresh.guarded_analysis(owner, data, helper=True)
            if value is not None:
                await owner._apply_local_analysis(value, image_fingerprint="offline", source="assistant_local_ocr",
                                                  observed_at=owner._analysis_capture_at)

        producer = asyncio.create_task(produce())
        owner._observer_task = producer
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            clock_offset = 13.75
            waiter = asyncio.create_task(live_refresh.refresh_for_query(
                owner, timeout=2, query_plan=query, **query.refresh_args()))
            async def release_at_join():
                while not any(s["stage"] == "query_joined_worker"
                              for s in getattr(owner, "_active_refresh_trace", {}).get("stages", [])):
                    await asyncio.sleep(.005)
                release.set()
            join = asyncio.create_task(release_at_join())
            value = await waiter
            await join
            audit = owner._last_refresh_diagnostics
            assert any(13.5 <= s["worker_elapsed_seconds"] < 14.2 for s in audit["stages"]
                       if s["stage"] == "query_joined_worker")
            events = {r["stage"]: r for r in public_trace(owner) if r["stage"].startswith("relic_")}
            if scenario == "switched":
                assert value is None and not reread.is_set()
                assert events["relic_parsed"]["sub_stats"] == 4
                assert events["relic_verified"]["outcome"] == "rejected"
                assert "relic_committed" not in events
                return
            assert value is not None, (audit, calls, reread.is_set())
            assert not reread.is_set()
            card = equipment_card(value)
            assert card["complete"] is not missing
            assert len(card["sub_stats"]) == (3 if missing else 4)
            assert [events[s]["sub_stats"] for s in ("relic_parsed", "relic_verified", "relic_committed")] == [3 if missing else 4] * 3
            assert len({r["job_id"] for r in events.values()}) == 1
            assert {r["frame_seq"] for r in events.values()} == {21}
            assert events["relic_verified"]["verification_frame_seq"] == 22
        finally:
            release.set()
            finish_reread.set()
            await producer

    asyncio.run(run())
