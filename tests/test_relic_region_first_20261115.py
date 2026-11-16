"""3B.2a red replay: a late relic query must not wait for a four-tile scan.

3B.1 checks relic priority only after every native tile completes. This
replay joins a pure relic query after the producer has run for 13.75s,
then keeps an unrelated tile busy so the fixed 8s wait expires before the
all-tiles checkpoint. The assertions describe the 3B.2 behavior; they fail
until the region-first read lands. The failure message carries the
per-tile timing so the worst-case budget can be judged before designing it.
"""
import asyncio
import json
import threading
import time
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from PIL import Image, ImageDraw

import pytest

from plugin.plugins.hsr_companion.adapters.local_vision import LocalVisionAdapter
from plugin.plugins.hsr_companion.runtime import live_refresh, page_handoff
from plugin.plugins.hsr_companion.runtime.equipment import equipment_card
from plugin.plugins.hsr_companion.runtime.query_plan import build_query_plan
from plugin.plugins.hsr_companion.runtime.vision_trace import public_trace
from plugin.plugins.hsr_companion.tests.test_current_observation import plugin_fixture
from plugin.plugins.hsr_companion.tests.test_experience_fixes import box
from plugin.plugins.hsr_companion.tests.test_relic_fields import relic_boxes

FULL_WIDTH, FULL_HEIGHT = 2560, 1440
RELIC_X, RELIC_Y = 1500, 640
UNRELATED_TILE_SECONDS = 5.0
RELIC_BOTTOM_SECONDS = 5.0
MARKER_COLORS = {
    (0, 0, 255): "native_tile_page",
    (255, 0, 0): "native_tile_relic_top",
    (0, 255, 0): "native_tile_unrelated",
    (255, 255, 0): "native_tile_relic_bottom",
}
MARKERS = {
    "native_tile_page": (100, 100),
    "native_tile_relic_top": (RELIC_X, RELIC_Y),
    "native_tile_unrelated": (100, 900),
    "native_tile_relic_bottom": (RELIC_X, 900),
}
MARKER_BY_STAGE = {stage: color for color, stage in MARKER_COLORS.items()}


def scaled(boxes, width, height):
    sx, sy = width / FULL_WIDTH, height / FULL_HEIGHT
    return [{**item, "left": item["left"] * sx, "right": item["right"] * sx,
             "top": item["top"] * sy, "bottom": item["bottom"] * sy} for item in boxes]


class Backend:
    """Identify each crop by a drawn marker so tile order can change."""

    def __init__(self, *, with_character=True):
        self.calls = []
        self.started = []
        self.first_tile = True
        self.with_character = with_character
        self.entered = threading.Event()
        self.release = threading.Event()
        self.slow_release = threading.Event()
        self.relic_release = threading.Event()
        self.rows = [box("遗器详情", 60, 30), *relic_boxes("头部", x=RELIC_X, y=RELIC_Y)]

    def recognize_line(self, image):
        return []

    @staticmethod
    def find(image):
        if image.size == (768, 432):
            return "scene_probe", 0, 0
        present = {color for _, color in image.getcolors(maxcolors=image.width * image.height) or []}
        pixels = image.load()
        for color, stage in MARKER_COLORS.items():
            if color not in present:
                continue
            for y in range(image.height):
                for x in range(image.width):
                    if pixels[x, y] == color:
                        return stage, x, y
        return None, None, None

    @staticmethod
    def shift(boxes, offset):
        ox, oy = offset
        return [{**item, "left": item["left"] - ox, "right": item["right"] - ox,
                 "top": item["top"] - oy, "bottom": item["bottom"] - oy} for item in boxes]

    def extract_text_with_boxes(self, image):
        started = time.monotonic()
        stage, marker_x, marker_y = self.find(image)
        boxes = []
        if stage == "scene_probe":
            boxes = scaled(self.rows, *image.size)
        elif stage is not None:
            origin = (MARKERS[stage][0] - marker_x, MARKERS[stage][1] - marker_y)
            first = self.first_tile
            self.first_tile = False
            self.started.append({"stage": stage, "size": list(image.size), "at": started})
            if first:
                self.entered.set()
                self.release.wait(5)
            if stage == "native_tile_unrelated":
                self.slow_release.wait(UNRELATED_TILE_SECONDS)
            elif stage == "native_tile_relic_bottom":
                self.relic_release.wait(RELIC_BOTTOM_SECONDS)
            if stage == "native_tile_page":
                rows = [box("遗器详情", 60, 30)]
                if self.with_character:
                    rows.append(box("银狼", 200, 60))
                rows.append(box("探索", 100, 200))
            elif stage == "native_tile_unrelated":
                rows = [box("任务", 100, 700)]
            else:
                right = origin[0] + image.width
                bottom = origin[1] + image.height
                rows = [item for item in self.rows
                        if item["left"] >= origin[0] and item["right"] <= right
                        and item["top"] >= origin[1] and item["bottom"] <= bottom]
            boxes = self.shift(rows, origin)
        seconds = round(time.monotonic() - started, 3)
        self.calls.append({"stage": stage or "other", "size": list(image.size), "seconds": seconds})
        return "\n".join(item["text"] for item in boxes), boxes


@pytest.mark.parametrize("with_character,prior_character", [
    (True, False),
    (False, True),
    (False, False),
])
def test_late_relic_query_does_not_wait_for_four_tile_scan(
        tmp_path, monkeypatch, with_character, prior_character):
    owner, _, _ = plugin_fixture(tmp_path)
    if prior_character:
        owner._last_full_characters = {
            "page": "character_detail",
            "characters": [{"entity_id": "character.silverwolf", "name": "银狼",
                            "identity_scope": "detail_subject", "confidence": "high"}],
            "observed_at": (
                live_refresh.datetime.now(live_refresh.timezone.utc) - timedelta(seconds=5)
            ).isoformat(),
            "serial": 1,
        }
    image = Image.new("RGB", (FULL_WIDTH, FULL_HEIGHT), (40, 48, 54))
    draw = ImageDraw.Draw(image)
    for stage, (x, y) in MARKERS.items():
        draw.rectangle((x, y, x + 12, y + 12), fill=MARKER_BY_STAGE[stage])
    stream = BytesIO()
    image.save(stream, format="PNG")
    data = stream.getvalue()
    backend = Backend(with_character=with_character)
    owner._vision = LocalVisionAdapter(owner._catalog)
    monkeypatch.setattr(owner._vision, "_get_backend", lambda: backend)
    real_clock = time.monotonic
    clock_offset = 0.
    monkeypatch.setattr(live_refresh, "time", SimpleNamespace(
        monotonic=lambda: real_clock() + clock_offset, time=time.time))
    monkeypatch.setattr(live_refresh, "LIVE_POLL_SECONDS", .01)
    monkeypatch.setattr(page_handoff, "queue_page_check", lambda *args, **kwargs: None)

    def frame(_seq=0):
        return SimpleNamespace(image_bytes=data, frame_seq=22, timestamp=time.time(),
                               game={"running": True, "pid": 1},
                               capture={"state": "capturing", "isolated_window": True})

    owner._assistant_client = SimpleNamespace(fetch_frame=frame)

    async def validation_frame(_owner):
        return frame()

    monkeypatch.setattr(live_refresh, "fetch_recent_frame", validation_frame)
    owner._analysis_capture_at = (
        live_refresh.datetime.now(live_refresh.timezone.utc) - timedelta(seconds=14)
    ).isoformat()
    owner._analysis_input_frame_seq = 21
    query = build_query_plan("这件遗器的主词条和副词条分别是什么？", "read_screen",
                             screen_fields=["equipment"], response_detail="full")

    async def run():
        nonlocal clock_offset

        async def produce():
            value = await live_refresh.guarded_analysis(owner, data, helper=True)
            if value is not None:
                await owner._apply_local_analysis(value, image_fingerprint="offline",
                                                  source="assistant_local_ocr",
                                                  observed_at=owner._analysis_capture_at)

        producer = asyncio.create_task(produce())
        owner._observer_task = producer
        try:
            assert await asyncio.to_thread(backend.entered.wait, 2)
            # The producer has already been running; the question joins late.
            clock_offset = 13.75
            waiter = asyncio.create_task(live_refresh.refresh_for_query(
                owner, timeout=8, query_plan=query, **query.refresh_args()))

            async def release_at_join():
                while not any(stage["stage"] == "query_joined_worker"
                              for stage in (getattr(owner, "_active_refresh_trace", {}) or {}).get("stages", [])):
                    await asyncio.sleep(.005)
                backend.release.set()

            helper = asyncio.create_task(release_at_join())
            value = await waiter
            await helper
            audit = owner._last_refresh_diagnostics
            events = {event["stage"]: event for event in public_trace(owner)
                      if event["stage"].startswith("relic_")}
            completed = {item["stage"] for item in backend.calls}
            timings = {
                "completed": list(backend.calls),
                "started": list(backend.started),
                "pending": [{"stage": item["stage"], "elapsed": round(time.monotonic() - item["at"], 3)}
                            for item in backend.started if item["stage"] not in completed],
                "unrelated_tile_seconds": UNRELATED_TILE_SECONDS,
                "relic_bottom_seconds": RELIC_BOTTOM_SECONDS,
            }
            artifact = (Path(__file__).resolve().parents[1] / "dist/relic-region-first-20261115"
                        / f"tile-timing-{'character' if with_character else 'prior' if prior_character else 'no-character'}.json")
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text(json.dumps({"audit": audit, "events": events, "timings": timings},
                                           ensure_ascii=False, indent=2), encoding="utf-8")
            assert value is not None, {"audit": audit, "events": events, "timings": timings}
            assert audit["wait_seconds"] < 8
            card = equipment_card(value)
            assert card["complete"] is True
            assert len(card["sub_stats"]) == 4
            assert events["relic_parsed"]["relic_priority"] is True
            assert events["relic_committed"]["sub_stats"] == 4
            if with_character:
                assert any(item.get("name") == "银狼" for item in value.get("page_characters") or []), value
                assert not value.get("character_context"), value
            elif prior_character:
                assert not value.get("page_characters"), value
                raw = owner._committed_observation or {}
                debug = {
                    "raw_characters": raw.get("characters"),
                    "raw_context": raw.get("character_context"),
                    "raw_page": (raw.get("page") or {}).get("page_type"),
                    "raw_priority": ((raw.get("recognition") or {}).get("text_reader") or {}).get("relic_priority"),
                    "value": value,
                }
                assert (value.get("character_context") or {}).get("name") == "银狼", debug
                assert value.get("failure_reason") == "character_pending", value
            else:
                assert not value.get("page_characters"), value
                assert not value.get("character_context"), value
                assert value.get("equipment_complete") is True
                assert value.get("failure_reason") == "character_pending", value
            assert not any(item["stage"] == "native_tile_unrelated" for item in backend.started), timings
        finally:
            backend.release.set()
            backend.slow_release.set()
            backend.relic_release.set()
            try:
                await asyncio.wait_for(producer, timeout=5)
            except TimeoutError:
                producer.cancel()
                await asyncio.gather(producer, return_exceptions=True)
            except Exception:
                pass  # Cleanup must not replace the replay assertion.

    asyncio.run(run())
