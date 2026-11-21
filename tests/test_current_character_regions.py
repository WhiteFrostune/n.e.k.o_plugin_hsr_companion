"""S1 current-frame regional reads; synthetic OCR is not live acceptance."""
import asyncio
import copy
import threading
import time
from datetime import datetime, timezone
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image
from plugin.plugins.hsr_companion.runtime import live_refresh, vision_scheduler
from plugin.plugins.hsr_companion.runtime.observation_store import ObservationSnapshot
from plugin.plugins.hsr_companion.runtime.query_plan import build_query_plan
from plugin.plugins.hsr_companion.runtime.region_query import read_query_regions
from plugin.plugins.hsr_companion.tests.test_current_observation import plugin_fixture
from plugin.plugins.hsr_companion.tests.test_delivery_fix_20261012 import frame

CONTROLS = [
    {"text": "角色详情", "control": True, "region": {"left": .05, "top": .03, "right": .18, "bottom": .08}},
    {"text": "光锥", "control": True, "region": {"left": .10, "top": .20, "right": .18, "bottom": .24}},
    {"text": "行迹", "control": True, "region": {"left": .10, "top": .30, "right": .18, "bottom": .34}},
]
ROLE = {"entity_id": "character.silverwolf", "name": "银狼", "identity_scope": "detail_subject",
        "evidence": "character_detail_name_region", "region_precision": "label", "raw_label": "银狼",
        "region": {"left": .78, "top": .10, "right": .90, "bottom": .16}, "confidence": "high",
        "name_validation": {"entity_id": "character.silverwolf", "method": "native_and_enlarged_label_agree", "scores": [.99, .99]}}


def raw_character():
    return {"ok": True, "page": {"page_type": "character_detail", "scene": "menu", "label": "角色详情", "confidence": "high", "supported": True},
            "scene": {"primary_state": "menu", "verified": True, "confidence": "high", "evidence": ["current controls"]},
            "characters": [copy.deepcopy(ROLE)],
            "screen_facts": {"facts": [{"kind": "stat", "scope": "attributes", "label": "防御力", "value": "3335",
               "evidence": [{"text": "防御力3335", "region": {"left": .72, "top": .40, "right": .95, "bottom": .45}}]}], "visible_text": []},
            "recognition": {"scene_regions": copy.deepcopy(CONTROLS), "region_query": True,
                "text_reader": {"full_frame_covered": False}}}


def owner_at_character(tmp_path):
    owner, values, sent = plugin_fixture(tmp_path)
    image = Image.new("RGB", (640, 400), "black")
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    payload = buffer.getvalue()
    captures = []

    def capture(since):
        item = frame(payload)
        item.frame_seq = len(captures) + 1
        captures.append(item)
        return item

    reads = []
    def read(data, *, fields, cancelled):
        assert not cancelled()
        reads.append((data, set(fields)))
        return raw_character()

    owner._assistant_client = SimpleNamespace(fetch_frame=capture)
    owner._vision = SimpleNamespace(read_character_detail=read, read_region=lambda *a, **k: [])
    owner._answer_query_generation = 1
    return owner, captures, reads


@pytest.mark.parametrize("fields", [["characters"], ["attributes"], ["characters", "attributes"]])
def test_no_commit_reads_current_character_and_keeps_real_capture_time(tmp_path, fields):
    owner, captures, reads = owner_at_character(tmp_path)
    plan = build_query_plan("", "read_screen", screen_fields=fields)
    result = asyncio.run(read_query_regions(owner, plan, time.monotonic() + .7))
    assert result is not None
    assert result["read_scope"] == "region"
    assert [a["entity_id"] for a in result["characters"]] == [ROLE["entity_id"]]
    assert result["observed_at"] == datetime.fromtimestamp(captures[0].timestamp, timezone.utc).isoformat()
    assert result["reuse"]["validated_at"] != result["observed_at"]
    assert len(reads) == 1
    assert ObservationSnapshot.select(owner, result, fields=plan.fields).matches(owner)
    assert owner._accepted_frame_page_regions


def test_role_query_does_not_wait_for_unrelated_full_producer(tmp_path):
    owner, _, reads = owner_at_character(tmp_path)

    async def run():
        owner._observer_task = asyncio.create_task(asyncio.sleep(5))
        try:
            plan = build_query_plan("", "read_screen", screen_fields=["characters"])
            started = time.monotonic()
            result = await live_refresh.refresh_for_query(owner, timeout=.7, query_plan=plan)
            assert result is not None and result["characters"]
            assert time.monotonic() - started < .7
            assert any(s["stage"] == "region_query_selected" for s in owner._last_refresh_diagnostics["stages"])
        finally:
            owner._observer_task.cancel()
            await asyncio.gather(owner._observer_task, return_exceptions=True)

    asyncio.run(run())
    assert len(reads) == 1


def test_active_region_request_preempts_even_a_new_full_pass():
    owner = SimpleNamespace(_visual_query_waiters=1, _query_refresh_generation=1,
                            _query_refresh_deadline=time.monotonic()+1, _region_query_waiters=1)
    assert vision_scheduler.query_yield_requested(owner, 1, "full")


def test_query_worker_cancellation_retains_native_work_until_it_really_exits(monkeypatch):
    from plugin.plugins.hsr_companion.runtime.vision_scheduler import run_query_worker
    owner = SimpleNamespace()
    entered, release = threading.Event(), threading.Event()

    def native(*, cancelled):
        entered.set()
        assert release.wait(3)
        return "finished"

    async def run():
        task = asyncio.create_task(run_query_worker(owner, native, deadline=time.monotonic()+1))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            retained = owner._draining_vision_worker
            assert not retained.done()
            release.set()
            assert await retained == "finished"
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("change", ["stop", "question", "epoch"])
def test_late_region_read_cannot_survive_lifecycle_change(tmp_path, change):
    owner, _, _ = owner_at_character(tmp_path)
    original = owner._vision.read_character_detail

    def read(*a, **kw):
        result = original(*a, **kw)
        if change == "stop":
            owner._observer_stop_requested = True
        if change == "question":
            owner._answer_query_generation += 1
        if change == "epoch":
            owner._observation_epoch = getattr(owner, "_observation_epoch", 0) + 1
        return result

    owner._vision.read_character_detail = read
    plan = build_query_plan("", "read_screen", screen_fields=["characters"])
    assert asyncio.run(read_query_regions(owner, plan, time.monotonic()+.7)) is None


@pytest.mark.parametrize("change", ["pixels", "pid", "capture"])
def test_capture_change_before_review_rejects_current_packet(tmp_path, change):
    owner, captures, _ = owner_at_character(tmp_path)
    original = owner._assistant_client.fetch_frame
    def capture(since):
        item = original(since)
        if len(captures) > 1:
            if change == "pixels":
                buffer = BytesIO()
                Image.new("RGB", (640, 400), "white").save(buffer, format="PNG")
                item.image_bytes = buffer.getvalue()
            if change == "pid":
                item.game["pid"] = 2
            if change == "capture":
                owner._capture_failure_revision = 1
        return item
    owner._assistant_client.fetch_frame = capture
    plan = build_query_plan("", "read_screen", screen_fields=["characters"])
    assert asyncio.run(read_query_regions(owner, plan, time.monotonic()+.7)) is None


@pytest.mark.parametrize("change", ["epoch", "question", "capture", "identity", "proof"])
def test_admitted_region_is_still_revoked_at_delivery(tmp_path, change):
    owner, _, _ = owner_at_character(tmp_path)
    plan = build_query_plan("", "read_screen", screen_fields=["characters"])
    result = asyncio.run(read_query_regions(owner, plan, time.monotonic()+.7))
    assert result["characters"]
    if change == "identity":
        result["characters"][0]["entity_id"] = "character.fake"
    elif change == "proof":
        result["reuse"]["reviewed_coordinates"] = []
    else:
        name = {"epoch": "_observation_epoch", "question": "_answer_query_generation",
                "capture": "_capture_failure_revision"}[change]
        setattr(owner, name, getattr(owner, name, 0)+1)
    assert not ObservationSnapshot.select(owner, result, fields=plan.fields).matches(owner)


def test_partial_native_packet_never_claims_identity_complete(tmp_path):
    owner, _, _ = owner_at_character(tmp_path)
    def missing(*a, **kw):
        value = raw_character()
        value["characters"][0]["name_validation"] = {}
        return value
    owner._vision.read_character_detail = missing
    plan = build_query_plan("", "read_screen", screen_fields=["characters"])
    result = asyncio.run(read_query_regions(owner, plan, time.monotonic()+.7))
    assert result is not None and not result["characters"]


def test_next_native_read_waits_for_cancelled_thread(tmp_path):
    from plugin.plugins.hsr_companion.runtime.vision_scheduler import run_query_worker
    owner = SimpleNamespace()
    entered, release = threading.Event(), threading.Event()
    calls = []
    def first(*, cancelled):
        entered.set()
        assert release.wait(3)
    def second(*, cancelled):
        calls.append("second")
    async def run():
        a = asyncio.create_task(run_query_worker(owner, first, deadline=time.monotonic()+2))
        assert await asyncio.to_thread(entered.wait, 1)
        a.cancel()
        await asyncio.gather(a, return_exceptions=True)
        b = asyncio.create_task(run_query_worker(owner, second, deadline=time.monotonic()+2))
        try:
            await asyncio.sleep(.03)
            assert calls == []
            release.set()
            await b
            assert calls == ["second"]
        finally:
            release.set()
            await asyncio.gather(b, return_exceptions=True)
    asyncio.run(run())


def test_formal_tool_returns_current_identity_not_player_roster(tmp_path):
    owner, _, _ = owner_at_character(tmp_path)
    async def run():
        owner._observer_task = asyncio.create_task(asyncio.sleep(5))
        try:
            result = await owner.hsr_companion_prepare(query="你看到的是谁哇", intent="read_screen",
                                                     screen_fields=["characters"])
            assert result["screen_identity_answer"]["allowed"]
            assert result["screen_identity_answer"]["names"] == ["银狼"]
            assert result["player_context"] == {}
        finally:
            owner._observer_task.cancel()
            await asyncio.gather(owner._observer_task, return_exceptions=True)
    asyncio.run(run())


def test_native_reader_reuses_existing_parser_and_double_scale_name_gate(tmp_path):
    from plugin.plugins.hsr_companion.detectors.character_regions import read_character_detail
    owner, _, _ = owner_at_character(tmp_path)
    def box(text, left, top, right, bottom, score=.99):
        return dict(text=text, left=left, top=top, right=right, bottom=bottom, score=score)
    class Backend:
        def __init__(self):
            self.sizes = []
        def extract_text_with_boxes(self, image):
            self.sizes.append(image.size)
            n = len(self.sizes)
            if n == 1:
                return "", [box("角色详情", 30, 10, 110, 28), box("光锥", 60, 80, 100, 96),
                            box("行迹", 60, 120, 100, 136), box("银狼", 500, 40, 560, 60)]
            if n == 2:
                return "", [box("银狼", 6, 6, 66, 26)]
            if n == 3:
                return "", [box("银狼", 40, 36, 160, 76)]
            if n == 4:
                return "", [box("防御力", 10, 15, 60, 35), box("3335", 180, 15, 230, 35)]
            raise AssertionError("Unexpected extra OCR pass")
    backend = Backend()
    buffer = BytesIO()
    Image.new("RGB", (640, 400), "black").save(buffer, format="PNG")
    result = read_character_detail(backend, buffer.getvalue(), owner._catalog,
                                   fields={"characters", "attributes"})
    assert result["characters"][0]["entity_id"] == "character.silverwolf"
    assert result["characters"][0]["name_validation"]["method"] == "native_and_enlarged_label_agree"
    assert any(f.get("label") == "防御力" and f.get("value") == "3335" for f in result["screen_facts"]["facts"])
    assert len(backend.sizes) == 4
    assert backend.sizes[1][0] < 640
    assert not result["recognition"]["text_reader"]["full_frame_covered"]



def test_region_review_cannot_renew_old_global_fact_leases(tmp_path):
    owner, _, _ = owner_at_character(tmp_path)
    sentinel = {"identity": (0, 0), "regions": {(.72, .4, .95, .45): 123}}
    owner._live_field_verification = copy.deepcopy(sentinel)
    plan = build_query_plan("", "read_screen", screen_fields=["attributes"])
    result = asyncio.run(read_query_regions(owner, plan, time.monotonic()+.7))
    assert result["screen_facts"]["facts"]
    assert owner._live_field_verification == sentinel


def test_later_role_commit_rejects_old_detached_identity(tmp_path):
    owner, _, _ = owner_at_character(tmp_path)
    plan = build_query_plan("", "read_screen", screen_fields=["characters"])
    result = asyncio.run(read_query_regions(owner, plan, time.monotonic()+.7))
    assert result["characters"]
    owner._observation_serial += 1
    replacement = copy.deepcopy(owner._scene_preview)
    replacement["characters"] = []
    owner._scene_preview = replacement
    assert not ObservationSnapshot.select(owner, result, fields=plan.fields).matches(owner)


def test_query_takes_next_checkpoint_instead_of_finishing_background_tiles(tmp_path):
    from plugin.plugins.hsr_companion.adapters.native_text import QueryPreempted, read_native_text
    from plugin.plugins.hsr_companion.runtime.vision_scheduler import run_query_worker, vision_turn
    owner, _, _ = owner_at_character(tmp_path)
    entered, release = threading.Event(), threading.Event()
    calls = []
    class Backend:
        def extract_text_with_boxes(self, image):
            calls.append(image.size)
            entered.set()
            assert release.wait(3)
            time.sleep(.04)  # Measured synthetic native work, not a live latency claim.
            return "", []
    backend = Backend()
    async def producer():
        async with vision_turn(owner, "producer"):
            worker = asyncio.create_task(asyncio.to_thread(read_native_text, backend,
                Image.new("RGB", (2560, 1440)), yield_requested=lambda:
                    vision_scheduler.query_yield_requested(owner, 1, "full")))
            owner._draining_vision_worker = worker
            return await asyncio.shield(worker)
    async def run():
        task = asyncio.create_task(producer())
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            owner._visual_query_waiters = owner._region_query_waiters = 1
            owner._query_refresh_generation = 1
            owner._query_refresh_deadline = time.monotonic()+2
            query = asyncio.create_task(run_query_worker(owner, lambda **kw: "current role",
                                                       deadline=time.monotonic()+2))
            release.set()
            assert await query == "current role"
            with pytest.raises(QueryPreempted):
                await task
            assert len(calls) == 1  # Remaining native tiles must not delay this query.
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())



def test_detached_patch_update_cannot_revalidate_global_guard():
    from plugin.plugins.hsr_companion.runtime.delivery_scope import update_checked_patches
    area = {"region": dict(left=.72, top=.4, right=.95, bottom=.45)}
    key = (.72, .4, .95, .45)
    old, new = (key, "old pixels"), (key, "new pixels")
    snapshot = {"detached_region": True, "epoch": 0,
                "evidence": {"screen_facts": {"observation_serial": 1}}, "guard": ([area], (old,))}
    owner = SimpleNamespace(_observation_epoch=0, _observation_serial=1,
        _accepted_frame_delivery_guard=([area], (old,)), _reusable_observations={"full": snapshot})
    update_checked_patches(owner, (new,), selected_snapshot=snapshot)
    assert snapshot["guard"][1] == (new,)
    assert owner._accepted_frame_delivery_guard[1] == (old,)
