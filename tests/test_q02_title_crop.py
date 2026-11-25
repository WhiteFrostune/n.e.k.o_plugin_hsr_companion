"""Q02 title acquisition and bounded diagnostics; synthetic pixels are not live acceptance."""
import asyncio
import copy
import json
import time
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw
from plugin.plugins.hsr_companion.detectors import character_regions as reader
from plugin.plugins.hsr_companion.runtime import current_regions, rejection_trace
from plugin.plugins.hsr_companion.runtime.diagnostics import build_diagnostic_report
from plugin.plugins.hsr_companion.runtime.query_plan import build_query_plan
from plugin.plugins.hsr_companion.tests.test_current_character_regions import owner_at_character
from plugin.plugins.hsr_companion.tests.test_diagnostics import inputs
from plugin.plugins.hsr_companion.tests.test_s1_preemption_closeout import TOKENS, PixelBackend, layout_hint, picture

ID = "a" * 32


def hint(missing=False):
    value = layout_hint()
    if missing:
        value["page_controls"] = value["page_controls"][1:]
    return value


def run_reader(tmp_path, *, missing=False, backend=None, payload=None):
    owner, _, _ = owner_at_character(tmp_path)
    native = backend or PixelBackend()
    value = reader.read_character_detail(native, payload or picture(), owner._catalog,
                                         fields={"characters"}, layout_regions=hint(missing))
    return value, native


@pytest.mark.parametrize("missing", [False, True])
def test_cached_controls_always_acquire_current_title_in_one_atlas(tmp_path, missing):
    value, backend = run_reader(tmp_path, missing=missing)
    assert value is not None and value["page"]["page_type"] == "character_detail"
    assert value["scene"]["verified"]
    assert len(value["characters"]) == 1
    assert len(backend.calls) == 2  # One atlas, then unchanged independent name corroboration.
    assert "irrelevant-center" not in backend.calls[0]["tokens"]


@pytest.mark.parametrize("missing", [False, True])
def test_actual_page_change_and_absent_or_conflicting_title_still_rejected(tmp_path, missing):
    for title in ("背包", "遗器详情", ""):
        value, _ = run_reader(tmp_path, missing=missing, backend=PixelBackend(title=title))
        assert value is None


@pytest.mark.parametrize("missing", [False, True])
def test_weak_name_never_becomes_an_answer(tmp_path, missing):
    value, _ = run_reader(tmp_path, missing=missing, backend=PixelBackend(score=.89))
    assert value is not None and value["page"]["page_type"] == "character_detail"
    assert value["characters"] == []


def test_acquisition_deduplicates_and_keeps_existing_eight_control_limit():
    image = Image.new("RGB", (2560, 1600))
    h = hint(True)
    h["page_controls"] *= 3
    areas = reader._acquisition(image, h)
    controls = [r for kind, r in areas if kind == "control"]
    assert controls[0] == dict(zip(reader._KEYS, reader._CONTROL_AREAS[0]))
    assert len(controls) == 3
    h["page_controls"] = [{"region": dict(left=.20+i*.07, top=.4, right=.22+i*.07, bottom=.42)} for i in range(8)]
    controls = [r for kind, r in reader._acquisition(image, h) if kind == "control"]
    assert len(controls) <= 8
    assert controls[0] == dict(zip(reader._KEYS, reader._CONTROL_AREAS[0]))


def test_retaining_title_does_not_override_conflicting_current_titles(tmp_path):
    image = Image.open(BytesIO(picture())).convert("RGB")
    ImageDraw.Draw(image).rectangle((int(.14*image.width), int(.036*image.height),
                                    int(.17*image.width), int(.065*image.height)), fill=(130, 70, 210))
    output = BytesIO()
    image.save(output, format="PNG")
    class Conflicting(PixelBackend):
        def extract_text_with_boxes(self, sample):
            _, boxes = super().extract_text_with_boxes(sample)
            for box in boxes:
                if box["text"] == "花火":
                    box["text"] = "遗器详情"
            return "", boxes
    value, _ = run_reader(tmp_path, missing=True, payload=output.getvalue(), backend=Conflicting())
    assert value is None


def detail(**updates):
    return dict(crop_source="cached", title_covered=True, native_boxes=4, mapped_boxes=3,
                subcondition="preview_empty", **updates)


def test_q02_closed_detail_and_request_summary_survive_event_trimming():
    owner = SimpleNamespace()
    rejection_trace.emit(owner, "Q02", "compact", "reject", "no_analysis", request_id=ID, q02=detail())
    for i in range(300):
        rejection_trace.emit(owner, "V07", "verification", "reject",
                             "expired" if i % 2 else "no_result", request_id=ID)
    report = build_diagnostic_report(**{**inputs(), "rejection_trace": rejection_trace.snapshot(owner),
        "query_timings": {"state": "finished", "request_id": ID, "events": [
            {"stage": "ocr_call", "event": "returned", "duration_ms": 3.2, "after_ms": i,
             "operation": "extract_text_with_boxes"} for i in range(256)]}})
    snapshot = json.loads(report["copy_text"].split("\n", 5)[5])
    floor = next(f for f in snapshot["floors"] if f["floor"] == 4)
    summary = floor["metrics"]["rejection_trace"]["requests"][0]
    assert summary["last_q02"]["q02"] == detail()
    assert snapshot["size_control"]["state"] == "size_limited"
    assert not [e for e in floor["recent_events"] if e.get("branch_id") == "Q02"]
    assert report["snapshot"] == snapshot
    assert len(report["copy_text"].encode("utf-8")) <= 128 * 1024


@pytest.mark.parametrize("field,bad", [("crop_source", "PRIVATE"), ("subcondition", "PRIVATE")])
def test_q02_unknown_enum_is_mapped_and_private_fields_discarded(field, bad):
    owner = SimpleNamespace()
    value = detail()
    value.update({field: bad, "text": "PRIVATE", "name": "PRIVATE", "uid": "PRIVATE", "hash": "PRIVATE"})
    rejection_trace.emit(owner, "Q02", "compact", "reject", "no_analysis", request_id=ID, q02=value)
    result = rejection_trace.sanitize(rejection_trace.snapshot(owner))
    row = result["events"][0]
    assert row["q02"][field] == "unmapped_code"
    assert result["unmapped_count"] == 1
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize("field,bad", [("native_boxes", -1), ("native_boxes", True),
                                       ("mapped_boxes", float("nan")), ("title_covered", "yes")])
def test_q02_invalid_counts_stay_unknown(field, bad):
    value = detail()
    value[field] = bad
    owner = SimpleNamespace()
    rejection_trace.emit(owner, "Q02", "compact", "reject", "no_analysis", request_id=ID, q02=value)
    assert rejection_trace.snapshot(owner)["events"][0]["q02"][field] is None


def run_outer(tmp_path, monkeypatch, *, preview=None, prepare_empty=False, bad_recorder=False):
    owner, _, _ = owner_at_character(tmp_path)
    native = PixelBackend(title="" if preview is None else "角色详情")
    owner._vision.read_character_detail = lambda data, *, fields, cancelled: reader.read_character_detail(
        native, data, owner._catalog, fields=fields, cancelled=cancelled, layout_regions=hint(True))
    if preview is not None:
        monkeypatch.setattr(reader, "scene_preview", lambda *a, **k: copy.deepcopy(preview))
    if prepare_empty:
        async def empty(*args, **kwargs):
            return None
        monkeypatch.setattr(current_regions, "fetch_recent_frame", empty)
    else:
        owner._assistant_client.fetch_frame = lambda _: SimpleNamespace(image_bytes=picture(), frame_seq=9,
            timestamp=time.time(), game={"running": True}, capture={"state": "capturing", "isolated_window": True})
    if bad_recorder:
        monkeypatch.setattr(rejection_trace, "_record", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("PRIVATE")))
    with rejection_trace.context(owner, ID):
        result = asyncio.run(current_regions.read_current_character(owner,
            build_query_plan("", "read_screen", screen_fields=["characters"]), time.monotonic()+8))
    return result, rejection_trace.snapshot(owner)


@pytest.mark.parametrize("preview,expected", [
    (None, "preview_empty"),
    ({"page": {"page_type": "inventory"}, "scene": {"verified": True}, "recognition": {"scene_regions": [{}]}}, "wrong_page"),
    ({"page": {"page_type": "character_detail"}, "scene": {"verified": False}, "recognition": {"scene_regions": [{}]}}, "scene_unverified"),
    ({"page": {"page_type": "character_detail"}, "scene": {"verified": True}, "recognition": {"scene_regions": []}}, "no_controls"),
])
def test_q02_records_precise_page_failure_without_text(tmp_path, monkeypatch, preview, expected):
    result, state = run_outer(tmp_path, monkeypatch, preview=preview)
    assert result is None
    event = next(e for e in state["events"] if e["branch_id"] == "Q02" and e["reason"] == "no_analysis")
    assert event["q02"]["crop_source"] == "cached"
    assert event["q02"]["title_covered"] is True
    assert event["q02"]["native_boxes"] is not None
    assert event["q02"]["mapped_boxes"] is not None
    assert event["q02"]["subcondition"] == expected
    assert "角色详情" not in json.dumps(event, ensure_ascii=False)


def test_prepare_empty_is_not_reported_as_ocr_zero(tmp_path, monkeypatch):
    result, state = run_outer(tmp_path, monkeypatch, prepare_empty=True)
    assert result is None
    for event in (e for e in state["events"] if e["branch_id"] == "Q02"):
        assert event["q02"]["subcondition"] == "prepare_empty"
        assert event["q02"]["native_boxes"] is None
        assert event["q02"]["mapped_boxes"] is None


def test_failed_recorder_does_not_replace_rejected_business_result(tmp_path, monkeypatch):
    result, state = run_outer(tmp_path, monkeypatch, bad_recorder=True)
    assert result is None
    assert state["recording_errors"] > 0


@pytest.mark.parametrize("score,allowed", [(.849, False), (.85, True)])
def test_title_threshold_is_unchanged(tmp_path, score, allowed):
    class TitleScore(PixelBackend):
        def extract_text_with_boxes(self, sample):
            _, boxes = super().extract_text_with_boxes(sample)
            for box in boxes:
                if box["text"] == TOKENS[0][0]:
                    box["score"] = score
            return "", boxes
    value, _ = run_reader(tmp_path, missing=True, backend=TitleScore())
    assert (value is not None) is allowed


def test_trace_counts_include_unmapped_boxes_and_never_retain_text(tmp_path):
    class Outside(PixelBackend):
        def extract_text_with_boxes(self, sample):
            _, boxes = super().extract_text_with_boxes(sample)
            boxes.append(dict(text="PRIVATE", score=.99, left=0, top=0, right=1, bottom=1))
            return "", boxes
    with rejection_trace.compact_read_trace() as counts:
        value, backend = run_reader(tmp_path, missing=True, backend=Outside(title=""))
    assert value is None
    assert counts["native_boxes"] == len(backend.calls[0]["tokens"]) + 1
    assert counts["mapped_boxes"] == counts["native_boxes"] - 1
    assert counts["subcondition"] == "preview_empty"
    assert "PRIVATE" not in json.dumps(counts)


@pytest.mark.parametrize("broken", [False, True])
def test_trace_failures_preserve_success_and_business_exception(tmp_path, monkeypatch, broken):
    if broken:
        class BrokenContext:
            def set(self, value):
                raise RuntimeError("PRIVATE")
            def get(self):
                raise RuntimeError("PRIVATE")
        monkeypatch.setattr(rejection_trace, "_Q02_CONTEXT", BrokenContext())
    with rejection_trace.compact_read_trace():
        value, _ = run_reader(tmp_path, missing=True)
    assert value and len(value["characters"]) == 1
    class Failing(PixelBackend):
        def extract_text_with_boxes(self, image):
            raise ValueError("business failure")
    with pytest.raises(ValueError, match="business failure"), rejection_trace.compact_read_trace():
        run_reader(tmp_path, missing=True, backend=Failing())


def test_nested_trace_is_request_local_and_cancellation_propagates():
    async def run():
        with rejection_trace.compact_read_trace() as first:
            await asyncio.to_thread(rejection_trace.note_compact_read, native_boxes=7)
            with rejection_trace.compact_read_trace() as second:
                rejection_trace.note_compact_read(native_boxes=2)
            assert first["native_boxes"] == 7 and second["native_boxes"] == 2
            with pytest.raises(asyncio.CancelledError), rejection_trace.compact_read_trace():
                raise asyncio.CancelledError()
            assert first["native_boxes"] == 7
    asyncio.run(run())


def test_successful_reader_does_not_emit_rejection(tmp_path):
    owner = SimpleNamespace()
    with rejection_trace.context(owner, ID), rejection_trace.compact_read_trace() as counts:
        value, _ = run_reader(tmp_path, missing=True)
    assert value and counts["title_covered"] is True
    assert rejection_trace.snapshot(owner)["events"] == []


def test_same_q02_detail_coalesces_but_changed_subcondition_does_not():
    owner = SimpleNamespace()
    for _ in range(2):
        rejection_trace.emit(owner, "Q02", "compact", "reject", "no_analysis", request_id=ID, q02=detail())
    changed = detail()
    changed["subcondition"] = "wrong_page"
    rejection_trace.emit(owner, "Q02", "compact", "reject", "no_analysis", request_id=ID, q02=changed)
    rows = rejection_trace.snapshot(owner)["events"]
    assert len(rows) == 2 and rows[0]["count"] == 2
    assert rows[1]["q02"]["subcondition"] == "wrong_page"
