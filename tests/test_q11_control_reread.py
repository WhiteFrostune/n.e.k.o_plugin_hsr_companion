"""Q11 only: native control rereads, not live-game acceptance."""
import asyncio
import copy
import json
import time
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw
from plugin.plugins.hsr_companion.adapters.text_verification import verify_text_regions
from plugin.plugins.hsr_companion.runtime import current_regions as compact
from plugin.plugins.hsr_companion.runtime import rejection_trace
from plugin.plugins.hsr_companion.runtime.query_plan import build_query_plan
from plugin.plugins.hsr_companion.tests.test_current_character_regions import owner_at_character, raw_character
from plugin.plugins.hsr_companion.tests.test_delivery_fix_20261012 import frame


def picture(glow=False):
    image = Image.new("RGB", (640, 400), "black")
    if glow:
        ImageDraw.Draw(image).rectangle((34, 14, 40, 20), fill="white")
    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


class ControlBackend:
    def __init__(self, lines=None):
        self.lines = iter(lines or [[("角色详情", .99)]] * 32)
        self.calls = []

    def recognize_line(self, image):
        self.calls.append(image.size)
        return next(self.lines, [])


def setup_case(tmp_path, monkeypatch, *, backend=None, confirmed_name=True, real_review=False):
    owner, _, _ = owner_at_character(tmp_path)
    source, latest = picture(), picture(True)
    captures = []
    native = backend or ControlBackend()
    rereads, reviews, published = [], [], []

    def capture(since):
        item = frame(source if not captures else latest)
        item.frame_seq = len(captures) + 1
        captures.append(item)
        return item

    def read(data, *, fields, cancelled):
        assert not cancelled()
        value = raw_character()
        if not confirmed_name:
            value["characters"][0]["name_validation"] = {}
        return value

    def verify(payload, proofs, *, cancelled=None, trace=None):
        rereads.append((payload, copy.deepcopy(proofs)))
        return verify_text_regions(native, payload, proofs, cancelled=cancelled, trace=trace)

    async def review(o, snapshot, **kwargs):
        reviews.append(snapshot)
        return {"page_type": "character_detail", "reuse": {}}

    async def publish(value):
        published.append(value)

    owner._assistant_client = SimpleNamespace(fetch_frame=capture)
    owner._vision = SimpleNamespace(read_character_detail=read, verify_text_regions=verify)
    owner._publish_scene_preview = publish
    if not real_review:
        monkeypatch.setattr(compact, "review_snapshot", review)
        monkeypatch.setattr(compact, "checked_receipt", lambda *a, **k: True)
        monkeypatch.setattr(compact.ObservationSnapshot, "select", lambda *a, **k: SimpleNamespace(matches=lambda *a: True))
        monkeypatch.setattr(compact, "_page_guard", lambda *a: None)
    return SimpleNamespace(owner=owner, source=source, latest=latest, backend=native,
                           captures=captures, rereads=rereads, reviews=reviews, published=published)


def run_case(case, seconds=8, fields=("characters",)):
    return asyncio.run(compact.read_current_character(case.owner,
        build_query_plan("", "read_screen", screen_fields=fields), time.monotonic()+seconds))


def test_glowing_control_with_same_text_reaches_original_review(tmp_path, monkeypatch):
    case = setup_case(tmp_path, monkeypatch)
    result = run_case(case)
    assert result is not None
    assert case.reviews and case.published
    assert len(case.rereads) == 1 and case.rereads[0][0] == case.latest
    assert len(case.rereads[0][1]) == 1
    assert len(case.backend.calls) == 2
    assert not [e for e in rejection_trace.snapshot(case.owner)["events"] if e["branch_id"] == "Q11"]


@pytest.mark.parametrize("lines", [
    [[("遗器详情", .99)]],
    [[], [], []],
    [[("角色详情", .849)]] * 3,
    [[("角色详情", .99)], [("遗器详情", .99)]],
])
def test_changed_unreadable_weak_or_contradicted_control_remains_rejected(tmp_path, monkeypatch, lines):
    case = setup_case(tmp_path, monkeypatch, backend=ControlBackend(lines))
    assert run_case(case) is None
    assert not case.reviews and not case.published
    assert any(e["branch_id"] == "Q11" for e in rejection_trace.snapshot(case.owner)["events"])


def test_glow_fallback_keeps_native_name_gate_and_source_time(tmp_path, monkeypatch):
    case = setup_case(tmp_path, monkeypatch, confirmed_name=False)
    assert run_case(case) is not None
    assert case.reviews
    evidence = case.reviews[0]["evidence"]
    assert not evidence["characters"]
    from datetime import datetime, timezone
    assert evidence["observed_at"] == datetime.fromtimestamp(case.captures[0].timestamp, timezone.utc).isoformat()


def q11_event(owner):
    return next(e for e in rejection_trace.snapshot(owner)["events"] if e["branch_id"] == "Q11")


def test_real_snapshot_reviewer_still_admits_reproved_controls(tmp_path, monkeypatch):
    case = setup_case(tmp_path, monkeypatch, real_review=True)
    result = run_case(case)
    assert result and result["characters"]
    assert result["characters"][0]["entity_id"] == "character.silverwolf"
    assert result["reuse"]["reviewed_coordinates"]
    assert case.rereads and all(payload == case.latest for payload, _ in case.rereads)


@pytest.mark.parametrize("result_lines, expected", [
    ([[('遗器详情', .99)]], "contradiction"),
    ([[], [], []], "unreadable"),
    ([[('角色详情', .849)]] * 3, "unreadable"),
    ([[('角色详情', .99)], [('遗器详情', .99)]], "contradiction"),
])
def test_rejection_exports_minimal_rule_region_purpose_and_result(tmp_path, monkeypatch, result_lines, expected):
    from plugin.plugins.hsr_companion.runtime.diagnostics import build_diagnostic_report
    from plugin.plugins.hsr_companion.tests.test_diagnostics import inputs
    case = setup_case(tmp_path, monkeypatch, backend=ControlBackend(result_lines))
    assert run_case(case) is None
    event = q11_event(case.owner)
    assert event["q11"] == {"pixel_rule": "mean", "region_index": 0,
                            "region_purpose": "page_control", "text_review": expected}
    report = build_diagnostic_report(**{**inputs(), "rejection_trace": rejection_trace.snapshot(case.owner)})
    exported = json.loads(report["copy_text"].split("\n", 5)[5])
    assert exported == report["snapshot"]
    floor = next(f for f in exported["floors"] if f["floor"] == 4)
    row = next(e for e in floor["recent_events"] if e.get("branch_id") == "Q11")
    assert row["q11"] == event["q11"]
    assert "角色详情" not in json.dumps(row, ensure_ascii=False)
    assert "遗器详情" not in json.dumps(row, ensure_ascii=False)
    assert len(report["copy_text"].encode("utf-8")) <= 128 * 1024


def test_control_score_threshold_and_two_of_three_scales_are_unchanged(tmp_path, monkeypatch):
    backend = ControlBackend([[('角色详情', .84)], [('角色详情', .85)], [('角色详情', .85)]])
    case = setup_case(tmp_path, monkeypatch, backend=backend)
    assert run_case(case) is not None
    assert len(backend.calls) == 3


def test_missing_verifier_still_rejects(tmp_path, monkeypatch):
    case = setup_case(tmp_path, monkeypatch)
    del case.owner._vision.verify_text_regions
    assert run_case(case) is None
    assert q11_event(case.owner)["q11"]["text_review"] == "unavailable"
    assert not case.reviews


def test_unavailable_scheduler_never_starts_an_extra_reader(tmp_path, monkeypatch):
    case = setup_case(tmp_path, monkeypatch)
    capture = case.owner._assistant_client.fetch_frame
    def closing(since):
        result = capture(since)
        if len(case.captures) == 2:
            case.owner._vision_closing = True
        return result
    case.owner._assistant_client.fetch_frame = closing
    assert run_case(case) is None
    assert q11_event(case.owner)["q11"]["text_review"] == "not_scheduled"
    assert not case.backend.calls and not case.reviews


@pytest.mark.parametrize("change", ["deadline", "question", "epoch", "capture", "stop"])
def test_successful_reread_does_not_override_deadline_or_lifecycle(tmp_path, monkeypatch, change):
    case = setup_case(tmp_path, monkeypatch)
    real_time = time.monotonic
    clock = SimpleNamespace(value=real_time())
    monkeypatch.setattr(compact, "time", SimpleNamespace(monotonic=lambda: clock.value, time=time.time))
    verify = case.owner._vision.verify_text_regions
    def changed(*args, **kwargs):
        result = verify(*args, **kwargs)
        if change == "deadline":
            clock.value += 9
        elif change == "stop":
            case.owner._observer_stop_requested = True
        else:
            attribute = {"question": "_answer_query_generation", "epoch": "_observation_epoch",
                         "capture": "_capture_failure_revision"}[change]
            setattr(case.owner, attribute, getattr(case.owner, attribute, 0)+1)
        return result
    case.owner._vision.verify_text_regions = changed
    assert run_case(case) is None
    assert q11_event(case.owner)["q11"]["text_review"] == ("deadline" if change == "deadline" else "superseded")
    assert not case.reviews and not case.published
    assert time.monotonic is real_time


def test_reread_wait_uses_only_original_remaining_deadline(tmp_path, monkeypatch):
    case = setup_case(tmp_path, monkeypatch)
    real_worker = compact.run_query_worker
    original_wait = asyncio.wait_for
    seen = []
    async def wait(awaitable, timeout):
        seen.append(timeout)
        awaitable.close()
        raise TimeoutError
    # run_query_worker for the initial packet remains real; this replacement
    # applies only to the new bounded reread await, not a wall-clock sleep.
    monkeypatch.setattr(compact.asyncio, "wait_for", wait)
    assert run_case(case) is None
    assert len(seen) == 1 and 0 < seen[0] <= 8
    assert not case.rereads and not case.reviews
    assert q11_event(case.owner)["q11"]["text_review"] == "not_scheduled"
    assert compact.run_query_worker is real_worker
    monkeypatch.setattr(compact.asyncio, "wait_for", original_wait)


def test_pixel_explanation_failure_cannot_change_success(tmp_path, monkeypatch):
    case = setup_case(tmp_path, monkeypatch)
    def broken(*a):
        raise RuntimeError("PRIVATE_EXPLANATION")
    monkeypatch.setattr(compact, "region_change_detail", broken)
    assert run_case(case) is not None
    assert case.reviews


@pytest.mark.parametrize("broken", [False, True])
def test_recorder_failure_does_not_change_reread_rejection(tmp_path, monkeypatch, broken):
    case = setup_case(tmp_path, monkeypatch, backend=ControlBackend([[('遗器详情', .99)]]))
    if broken:
        def fail(*a, **k):
            raise RuntimeError("PRIVATE_RECORDER")
        monkeypatch.setattr(rejection_trace, "_record", fail)
    assert run_case(case) is None
    assert not case.reviews and not case.published


def test_q11_details_have_closed_schema_and_do_not_merge_different_boxes():
    owner = SimpleNamespace()
    detail = {"pixel_rule": "mean", "region_index": 0, "region_purpose": "page_control", "text_review": "unreadable"}
    for index in [0, 1, 0]:
        rejection_trace.emit(owner, "Q11", "compact", "reject", "controls_changed", q11={**detail, "region_index": index})
    events = rejection_trace.snapshot(owner)["events"]
    assert len(events) == 2 and events[0]["count"] == 2 and events[1]["count"] == 1
    secret = "PRIVATE_TEXT_CHARACTER_IMAGE_HASH_CREDENTIAL"
    rejection_trace.emit(owner, "Q11", "compact", "reject", "controls_changed",
        q11={"pixel_rule":secret, "region_index":True, "region_purpose":secret,
             "text_review":secret, "text":secret, "image":secret, "digest":secret})
    result = rejection_trace.sanitize(rejection_trace.snapshot(owner))
    assert secret not in json.dumps(result)
    assert result["events"][-1]["q11"] == {"pixel_rule":"unmapped_code", "region_index":None,
          "region_purpose":"unmapped_code", "text_review":"unmapped_code"}


def test_pixel_rule_helper_keeps_single_digit_glyph_detection():
    from plugin.plugins.hsr_companion.runtime.query_reuse import region_change_detail, regions_changed
    patch = ((0.,0.,1.,1.), (64,8), bytes(512))
    weak = bytearray(512)
    weak[:5] = bytes([32]*5)
    assert region_change_detail((patch,), ((patch[0],patch[1],bytes(weak)),)) is None
    weak[:8] = bytes([32]*8)
    changed = ((patch[0],patch[1],bytes(weak)),)
    assert regions_changed((patch,),changed)
    assert region_change_detail((patch,),changed) == {"pixel_rule":"cell_cluster","region_index":0}
    strong = bytearray(512)
    strong[:3] = bytes([200]*3)
    changed = ((patch[0],patch[1],bytes(strong)),)
    assert regions_changed((patch,),changed)
    assert region_change_detail((patch,),changed)["pixel_rule"] == "cell_strong"


def test_unconfirmed_name_stays_absent_with_real_snapshot_review(tmp_path, monkeypatch):
    case = setup_case(tmp_path, monkeypatch, confirmed_name=False, real_review=True)
    result = run_case(case)
    assert result is not None and not result["characters"]


def test_control_reproof_cannot_approve_an_old_attribute_digit(tmp_path, monkeypatch):
    import numpy as np
    class Digits(ControlBackend):
        def recognize_line(self, image):
            self.calls.append(image.size)
            pixels = np.asarray(image.convert("RGB"))
            if np.any((pixels[:,:,1] > 150) & (pixels[:,:,0] < 20) & (pixels[:,:,2] < 20)):
                return [("防御力3336", .99)]  # Source packet says 3335.
            return [("角色详情", .99)]
    case = setup_case(tmp_path, monkeypatch, real_review=True, backend=Digits())
    image = Image.open(BytesIO(case.latest)).convert("RGB")
    ImageDraw.Draw(image).rectangle((465,162,475,173),fill=(0,255,0))
    out = BytesIO()
    image.save(out,format="PNG")
    case.latest = out.getvalue()
    capture = case.owner._assistant_client.fetch_frame
    def current(since):
        result = capture(since)
        if len(case.captures) > 1:
            result.image_bytes = case.latest
        return result
    case.owner._assistant_client.fetch_frame = current
    result = run_case(case, fields=("characters","attributes"))
    assert not [e for e in rejection_trace.snapshot(case.owner)["events"] if e["branch_id"] == "Q11"]
    assert result is None or not any(f.get("kind")=="stat" and f.get("value")=="3335"
                                     for f in result["screen_facts"]["facts"])


def test_control_reread_never_overlaps_another_native_worker(tmp_path, monkeypatch):
    import threading

    from plugin.plugins.hsr_companion.runtime.vision_scheduler import run_query_worker
    case = setup_case(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    other_entered = threading.Event()
    verify = case.owner._vision.verify_text_regions

    def blocked(*args, **kwargs):
        assert case.owner._vision_schedule_role == "query"
        entered.set()
        assert release.wait(3), "controlled worker was not released"
        return verify(*args, **kwargs)

    def other(*, cancelled):
        assert release.is_set()
        other_entered.set()
        return None

    async def run():
        queued = asyncio.Event()
        class WatchedLock:
            def __init__(self):
                self.lock = asyncio.Lock()
            async def acquire(self):
                if self.lock.locked():
                    queued.set()
                await self.lock.acquire()
            def release(self):
                self.lock.release()
        case.owner._vision_schedule_lock = WatchedLock()
        task = asyncio.create_task(compact.read_current_character(case.owner,
            build_query_plan("", "read_screen", screen_fields=["characters"]),time.monotonic()+8))
        competitor = None
        try:
            assert await asyncio.to_thread(entered.wait,2)
            competitor = asyncio.create_task(run_query_worker(case.owner,other,deadline=time.monotonic()+8))
            await queued.wait()
            assert not other_entered.is_set()
            release.set()
            assert await task is not None
            await competitor
            assert other_entered.is_set()
        finally:
            release.set()
            await asyncio.gather(task, *([competitor] if competitor else []), return_exceptions=True)
    case.owner._vision.verify_text_regions = blocked
    asyncio.run(run())


def test_verifier_limit_remains_sixteen_not_an_extra_batch(tmp_path, monkeypatch):
    from plugin.plugins.hsr_companion.tests.test_current_character_regions import CONTROLS
    case = setup_case(tmp_path, monkeypatch)
    proofs = [{**CONTROLS[0],"region":{**CONTROLS[0]["region"],"left":.01+i*.001},"text":"角色详情"}
              for i in range(17)]
    case.backend.lines = iter([[('角色详情',.99)]]*40)
    async def run():
        latest = frame(case.latest)
        return await compact._reread_controls(case.owner,latest,proofs,time.monotonic()+8,
            case.owner._answer_query_generation,getattr(case.owner,"_observation_epoch",0),
            getattr(case.owner,"_capture_failure_revision",0))
    assert asyncio.run(run()) == "proof_limit"
    assert len(case.backend.calls) == 32  # Two successful scales * 16, no second batch.


def test_verifier_exception_records_only_error_enum(tmp_path, monkeypatch):
    case = setup_case(tmp_path, monkeypatch)
    def fail(*a, **k):
        raise RuntimeError("PRIVATE_OCR_TEXT_TOKEN")
    case.owner._vision.verify_text_regions = fail
    assert run_case(case) is None
    event = q11_event(case.owner)
    assert event["q11"]["text_review"] == "error"
    assert "PRIVATE" not in json.dumps(rejection_trace.snapshot(case.owner))


def test_expired_before_reread_does_not_call_ocr(tmp_path, monkeypatch):
    case = setup_case(tmp_path, monkeypatch)
    real_monotonic = time.monotonic
    clock = SimpleNamespace(value=real_monotonic())
    monkeypatch.setattr(compact,"time",SimpleNamespace(monotonic=lambda:clock.value,time=time.time))
    capture = case.owner._assistant_client.fetch_frame
    def expired(since):
        result = capture(since)
        if len(case.captures)==2:
            clock.value += 9
        return result
    case.owner._assistant_client.fetch_frame = expired
    assert run_case(case) is None
    assert q11_event(case.owner)["q11"]["text_review"] == "deadline"
    assert not case.rereads and not case.reviews
    assert time.monotonic is real_monotonic


def test_old_latest_frame_is_not_reread_after_queue(tmp_path, monkeypatch):
    case = setup_case(tmp_path, monkeypatch)
    capture = case.owner._assistant_client.fetch_frame
    def stale(since):
        result = capture(since)
        if len(case.captures)==2:
            result.timestamp -= 3
        return result
    # The final fetch normally filters this before Q11; explicitly injecting a
    # stale frame here checks the additional scheduled-reread preparation guard.
    async def fetch(o, **kw):
        return stale(0)
    monkeypatch.setattr(compact,"fetch_recent_frame",fetch)
    assert run_case(case) is None
    assert q11_event(case.owner)["q11"]["text_review"] == "frame_stale"
    assert not case.rereads


def test_oversize_control_keeps_existing_verifier_skip(tmp_path, monkeypatch):
    case = setup_case(tmp_path,monkeypatch)
    proof={"text":"角色详情","control":True,"region":{"left":.01,"top":.01,"right":.99,"bottom":.99}}
    async def run():
        return await compact._reread_controls(case.owner,frame(case.latest),[proof],time.monotonic()+8,
            case.owner._answer_query_generation,getattr(case.owner,"_observation_epoch",0),
            getattr(case.owner,"_capture_failure_revision",0))
    assert asyncio.run(run()) == "skipped"
    assert not case.backend.calls


@pytest.mark.parametrize("kind", ["missing_regions","region_count","geometry","empty_pixels","mean","strong_fraction"])
def test_pixel_rule_labels_explain_original_boolean(kind):
    from plugin.plugins.hsr_companion.runtime.query_reuse import region_change_detail, regions_changed
    patch=((0.,0.,1.,1.),(64,8),bytes(512))
    before=(patch,)
    if kind=="missing_regions":
        before,after=(),()
    elif kind=="region_count":
        after=()
    elif kind=="geometry":
        after=(((.1,0.,1.,1.),patch[1],patch[2]),)
    elif kind=="empty_pixels":
        before=after=((patch[0],(0,0),b""),)
    else:
        values=bytes([255]*512) if kind=="mean" else bytes([32]*12+[0]*500)
        after=((patch[0],patch[1],values),)
    assert regions_changed(before,after)
    assert region_change_detail(before,after)["pixel_rule"] == kind
