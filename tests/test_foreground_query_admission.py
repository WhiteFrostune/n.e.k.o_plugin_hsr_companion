"""A keeps a single native turn and expires only request-owned work."""
import asyncio
import time
from types import SimpleNamespace

import pytest
from PIL import Image
from plugin.plugins.hsr_companion.adapters.native_text import ObservationSuperseded, QueryPreempted, read_native_text
from plugin.plugins.hsr_companion.adapters.text_verification import proof_key
from plugin.plugins.hsr_companion.runtime import vision_scheduler as scheduler
from plugin.plugins.hsr_companion.runtime.live_refresh import _revalidate_guard_text
from plugin.plugins.hsr_companion.tests.test_delivery_fix_20261012 import frame, picture
from plugin.plugins.hsr_companion.tests.test_review_scope_progress_20261020 import A, evidence, setup


def test_demand_yields_during_cache_attempt_and_stops_at_expiry(monkeypatch):
    now = [10.]
    monkeypatch.setattr(scheduler, "time", SimpleNamespace(monotonic=lambda: now[0]))
    owner = SimpleNamespace(_visual_query_waiters=0)
    with scheduler.query_demand(owner, 18., regional=True):
        assert scheduler.query_yield_requested(owner, 0, "full")
        now[0] = 18.
        assert not scheduler.query_yield_requested(owner, 0, "full")
    assert owner._foreground_region_demand is None


@pytest.mark.parametrize("expired", [False, True])
def test_checkpoint_does_not_run_expired_proof_but_protects_live_answer(monkeypatch, expired):
    now = [10.]
    monkeypatch.setattr(scheduler, "time", SimpleNamespace(monotonic=lambda: now[0]))

    async def run():
        owner, queued, native_calls = setup()
        packet = frame(picture())
        with scheduler.query_demand(owner, 18., regional=False):
            task = asyncio.create_task(_revalidate_guard_text(owner, packet, {proof_key(A)[0]},
                                                             evidence=evidence([A]), cooperative=True))
            await asyncio.sleep(0)
            future, callback = queued[0]
            now[0] = 18. if expired else 17.
            try:
                future.set_result(callback())
            except ObservationSuperseded as exc:
                future.set_exception(exc)
            result = await task
        assert result.state == ("busy" if expired else "confirmed")
        assert len(native_calls) == (0 if expired else 1)

    asyncio.run(run())


def test_background_probe_exposes_a_checkpoint_between_overlapping_parts():
    calls = []
    backend = SimpleNamespace(extract_text_with_boxes=lambda image: (calls.append(image.size) or "", []))
    with pytest.raises(QueryPreempted):
        read_native_text(backend, Image.new("RGB", (2560, 1600)), on_progress=lambda boxes: None,
                         split_background=True, yield_requested=lambda: bool(calls))
    assert calls == [(448, 304)]


def test_region_capture_occurs_after_existing_worker_releases():
    async def run():
        owner = SimpleNamespace()
        order = []

        async def capture():
            order.append("capture")
            return ("fresh-frame",), {}

        async with scheduler.vision_turn(owner, "producer"):
            query = asyncio.create_task(scheduler.run_query_worker(owner, lambda data, **_: data,
                                                deadline=time.monotonic()+3, prepare=capture))
            await asyncio.sleep(0)
            assert not order
        assert await query == "fresh-frame"
        assert order == ["capture"]

    asyncio.run(run())


def test_native_crop_detector_profile_is_restored_after_failure():
    from plugin.plugins.hsr_companion.adapters.upright_ocr import UprightOCRBackend

    backend = UprightOCRBackend.__new__(UprightOCRBackend)
    detector = SimpleNamespace(limit_type="min", limit_side_len=736)
    backend.engine = SimpleNamespace(text_det=detector)

    def broken_extract(image):
        assert detector.limit_side_len == 512
        raise RuntimeError("synthetic OCR failure")

    backend.extract_text_with_boxes = broken_extract
    with pytest.raises(RuntimeError, match="synthetic OCR failure"):
        backend.extract_native_crops_with_boxes(Image.new("RGB", (640, 400)))
    assert detector.limit_side_len == 736


def test_followup_review_keeps_original_deadline_and_retires_old_request(monkeypatch):
    from plugin.plugins.hsr_companion.runtime.followup_read import _review_scope

    monkeypatch.setattr(scheduler, "time", SimpleNamespace(monotonic=lambda: 10.))
    request = SimpleNamespace(request_id="first", expires_at=18.)
    owner = SimpleNamespace(_pending_answer=request)
    with _review_scope(owner):
        first = scheduler.review_demand.get()
    request.expires_at = 20.
    with _review_scope(owner):
        assert scheduler.review_demand.get() is first
        assert first.deadline == 18.
    owner._pending_answer = SimpleNamespace(request_id="new", expires_at=25.)
    with _review_scope(owner):
        assert not scheduler.review_demand.get().expired()
        assert first.expired()
