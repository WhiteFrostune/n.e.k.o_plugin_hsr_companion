"""Offline resident/formal-call replay with real OCR; no host/helper/game access.

Run as a plugin module in the SDK test harness. Inputs and output are explicit
local paths. Only timing/status is saved, never OCR/chat text or image bytes.
File frames replace capture/transport, memory store and no speech target replace
host SDK services. Query/observer/scheduler/native OCR remain production code.
"""
import argparse
import asyncio
import hashlib
import io
import json
import logging
import threading
import time
from pathlib import Path

from PIL import Image
from plugin.plugins.hsr_companion.adapters.local_vision import LocalVisionAdapter
from plugin.plugins.hsr_companion.assistant import AssistantFrame
from plugin.plugins.hsr_companion.data import ExternalKnowledgeCatalog
from plugin.plugins.hsr_companion.runtime.live_refresh import drain_analysis_worker
from plugin.plugins.hsr_companion.runtime.timing_detail import snapshot
from plugin.plugins.hsr_companion.tests.test_current_observation import plugin_fixture
from plugin.plugins.hsr_companion.tests.test_resident_observer import configure_loop


class FileFrames:
    def __init__(self, payload):
        self.payload, self.sequence = payload, 0
        self.game = {"running": True, "pid": 1, "minimized": False, "foreground": True}
        self.capture = {"state": "capturing", "isolated_window": True, "frame_semantics": "every_capture"}

    def health(self):
        return {"connected": True, "frame_seq": self.sequence+1, "game": self.game, "capture": self.capture}

    def fetch_frame(self, since=0):
        self.sequence += 1
        return AssistantFrame(self.sequence, time.time(), self.payload, "image/bmp",
                              hashlib.sha256(self.payload).hexdigest(), self.game, self.capture)


def wire_image(path):
    with Image.open(path) as source:
        stream = io.BytesIO()
        source.convert("RGB").save(stream, format="BMP")
        return stream.getvalue()


async def wait_until(predicate, seconds):
    deadline = time.monotonic()+seconds
    while not predicate():
        if time.monotonic() >= deadline:
            raise TimeoutError("replay setup did not reach the requested checkpoint")
        await asyncio.sleep(.02)


async def run_case(label, payload, alternate, catalog, adapter, output):
    owner, values, _ = plugin_fixture(output/label)
    owner._catalog, owner._vision = catalog, adapter
    owner._target_character = lambda: ""  # No USER_CONTEXT_GET, socket or real speech.
    values["player_context"] = {}
    values["observer_settings"] = {"enabled": True, "proactive_enabled": False}
    owner._observer_status.update(enabled=True, proactive_enabled=False)
    frames = FileFrames(payload)
    configure_loop(owner, frames)
    event = threading.Event()
    backend = adapter._get_backend()._backend
    original = backend.extract_text_with_boxes
    entered = []

    def native(*args, **kwargs):
        entered.append(time.perf_counter())
        event.set()
        return original(*args, **kwargs)

    backend.extract_text_with_boxes = native
    observer = owner._observer_task = asyncio.create_task(owner._observer_loop())
    result = {"case": label}
    try:
        await wait_until(event.is_set, 30)
        if label in {"warm", "changed", "idle", "primed"}:
            await wait_until(lambda: bool(getattr(owner, "_committed_observation", None)), 90)
            result["warm_serial"] = getattr(owner, "_observation_serial", 0)
            if label == "primed":
                seeded = await owner.hsr_companion_prepare(query="你看到的是谁哇", intent="read_screen",
                                                           screen_fields=["characters"])
                result["seed_identity_allowed"] = (seeded.get("screen_identity_answer") or {}).get("allowed") is True
            if label in {"changed", "primed"}:
                frames.payload = alternate
            if label != "idle":
                event.clear()
                await wait_until(lambda: event.is_set() and getattr(owner, "_vision_schedule_role", "") == "producer", 60)
        if label == "mid_native":
            # Injection time only; leave every product deadline unchanged.
            await asyncio.sleep(5)
        if label == "late_call":
            await wait_until(lambda: len(entered) >= 3, 60)
        result["producer_active_at_request"] = getattr(owner, "_vision_schedule_role", "idle") == "producer"
        result["background_job"] = getattr(owner, "_vision_job_id", None)
        began = time.perf_counter()
        # Actual decorated public tool; actual plan, coordinator, observer and
        # worker admission. No wait-budget or recognizer-output substitution.
        value = await owner.hsr_companion_prepare(query="你看到的是谁哇", intent="read_screen",
                                                 screen_fields=["characters"])
        result["formal_seconds"] = round(time.perf_counter()-began, 6)
        answer = value.get("screen_identity_answer") or {}
        result["identity_allowed"] = answer.get("allowed") is True
        result["identity_count"] = len(answer.get("names") or [])
        result["refresh"] = getattr(owner, "_last_refresh_diagnostics", {})
        result["query_state"] = (value.get("query_answer") or {}).get("state")
    except Exception as exc:
        import traceback
        result["error_type"] = type(exc).__name__
        result["origin"] = [{"file": Path(f.filename).name, "function": f.name, "line": f.lineno}
                            for f in traceback.extract_tb(exc.__traceback__)[-6:]]
    finally:
        result["timing_at_return"] = snapshot(owner)
        owner._observer_stop_requested = True
        observer.cancel()
        try:
            await observer
        except asyncio.CancelledError:
            pass
        await drain_analysis_worker(owner)
        # A native call may finish after the request; retain its completion in
        # the same timing record instead of forgetting the thread at timeout.
        retained = getattr(owner, "_draining_vision_worker", None)
        if retained is not None and not retained.done():
            await asyncio.gather(retained, return_exceptions=True)
        backend.extract_text_with_boxes = original
        result["timing_after_drain"] = snapshot(owner)
        result["native_calls"] = len(entered)
    return result


async def replay(args):
    output = Path(args.report).resolve()
    if output.exists():
        raise ValueError("report must be a new file")
    output.parent.mkdir(parents=True, exist_ok=True)
    registry = json.loads((Path(__file__).resolve().parents[1]/"resources/character_registry.json").read_text("utf-8"))
    catalog = ExternalKnowledgeCatalog(registry=registry)
    adapter = LocalVisionAdapter(catalog)
    began = time.perf_counter()
    backend = adapter._get_backend()
    if not backend.is_available():
        raise RuntimeError("local OCR model unavailable")
    report = {"real_ocr": True, "real_resident_observer": True, "real_formal_entry": True,
              "capture": "saved_pixels_with_replay_timestamps_bmp", "host": "in_memory_no_speech",
              "installed_plugin_touched": False, "product_waits_changed": False,
              "model_setup_seconds": round(time.perf_counter()-began, 6), "runs": []}
    payload = wire_image(args.frame)
    alternate = wire_image(args.alternate_frame) if args.alternate_frame else payload
    try:
        for label in args.cases:
            result = await run_case(label, payload, alternate, catalog, adapter, output.parent)
            report["runs"].append(result)
            output.write_text(json.dumps(report, indent=2, ensure_ascii=True), encoding="utf-8")
            print(json.dumps({k: v for k, v in result.items() if k not in {
                "refresh", "timing_at_return", "timing_after_drain"}}), flush=True)
    finally:
        adapter.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frame", required=True)
    parser.add_argument("--alternate-frame")
    parser.add_argument("--report", required=True)
    parser.add_argument("--cases", nargs="+", choices=("cold", "warm", "changed", "idle", "mid_native", "late_call", "primed"), default=["cold", "warm", "changed"])
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)  # Existing product logs may contain OCR; do not export them.
    asyncio.run(replay(args))


if __name__ == "__main__":
    main()
