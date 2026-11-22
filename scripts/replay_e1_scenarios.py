"""Exercise four E1 scenarios on a saved screenshot and the real offline plugin."""

import argparse
import asyncio
import hashlib
import json
import logging
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import psutil
from PIL import Image, ImageEnhance, ImageOps
from plugin.plugins.hsr_companion import HsrCompanionPlugin
from plugin.plugins.hsr_companion.adapters.local_vision import LocalVisionAdapter
from plugin.plugins.hsr_companion.runtime.live_refresh import drain_analysis_worker
from plugin.plugins.hsr_companion.scripts.analyze_ocr_bottlenecks import catalog_for_source
from plugin.plugins.hsr_companion.scripts.replay_request_timing import FileFrames, wait_until
from plugin.plugins.hsr_companion.scripts.replay_static_patrol import encode
from plugin.plugins.hsr_companion.tests.test_current_observation import plugin_fixture
from plugin.plugins.hsr_companion.tests.test_resident_observer import configure_loop

SCENARIOS = ("burst", "expired_idle", "name_noise", "cpu_pressure")
EXPECTED = "character.pearl"


class ScheduledFrames(FileFrames):
    """Synthetic proper cadence, with no future frame and no age renewal."""

    def __init__(self, variants):
        super().__init__(variants[0])
        self.origin_mono = time.monotonic()
        self.origin_wall = time.time()
        self.variants = variants
        self.digests = [hashlib.sha256(data).hexdigest() for data in variants]

    def cursor(self):
        return max(0, int((time.monotonic() - self.origin_mono) / 0.75))

    def health(self):
        return {**super().health(), "frame_seq": self.cursor() + 1}

    def fetch_frame(self, since=0):
        from plugin.plugins.hsr_companion.assistant import AssistantFrame

        index = self.cursor()
        self.sequence = index + 1
        if self.sequence <= since:
            return None
        key = index % len(self.variants)
        return AssistantFrame(
            self.sequence,
            self.origin_wall + index * 0.75,
            self.variants[key],
            "image/bmp",
            self.digests[key],
            self.game,
            self.capture,
        )


@contextmanager
def pressure(enabled):
    if not enabled:
        yield None
        return
    owner = psutil.Process()
    original = owner.cpu_affinity()
    cpus = original[: min(4, len(original))]
    workers = []
    try:
        owner.cpu_affinity(cpus)
        for _ in range(len(cpus)):
            script = (
                "import psutil,time\n"
                f"psutil.Process().cpu_affinity({cpus!r})\n"
                "end=time.monotonic()+180\nwhile time.monotonic()<end: pass\n"
            )
            workers.append(
                subprocess.Popen(
                    [sys.executable, "-c", script], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                )
            )
        yield {"logical_cpus": len(cpus), "busy_processes": len(workers)}
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.terminate()  # Owned Popen/PID only.
            worker.wait(timeout=5)
        owner.cpu_affinity(original)


def noise_variants(image, region):
    bounds = tuple(
        round(region[key] * size)
        for key, size in zip(
            ("left", "top", "right", "bottom"), (image.width, image.height, image.width, image.height), strict=True
        )
    )
    crop = image.crop(bounds)
    gray = ImageOps.grayscale(crop)
    mask = gray.point(lambda value: 255 if value < 104 else 0)
    variants = [encode(image)]
    for factor in (0.67, 1.56):
        altered = image.copy()
        altered.paste(ImageEnhance.Brightness(crop).enhance(factor), bounds, mask)
        variants.append(encode(altered))
    return tuple(variants)


async def read(owner):
    started = time.perf_counter()
    result = await owner.hsr_companion_prepare(
        query="你看到的是谁哇", intent="read_screen", screen_fields=["characters"]
    )
    elapsed = time.perf_counter() - started
    answer = result.get("screen_identity_answer") or {}
    visual = result.get("current_visual_evidence") or {}
    ids = [a.get("entity_id") for a in visual.get("characters") or []]
    state = (result.get("query_answer") or {}).get("state")
    return {
        "seconds": round(elapsed, 3),
        "state": state,
        "accepted_ids": ids,
        "correct_within_8": bool(elapsed <= 8 and state == "ready" and answer.get("allowed") and ids == [EXPECTED]),
    }


async def exercise(scenario, index, adapter, catalog, frames_data, output):
    owner, values, _ = plugin_fixture(output.parent / f"{output.stem}-{scenario}-{index}")
    owner._catalog, owner._vision = catalog, adapter
    owner._target_character = lambda: ""
    values["player_context"] = {}
    values["observer_settings"] = {"enabled": True, "proactive_enabled": False}
    owner._observer_status.update(enabled=True, proactive_enabled=False)
    client = ScheduledFrames(frames_data)
    configure_loop(owner, client)
    entered = threading.Event()
    full_passes = []
    original = adapter.analyze_live

    def full(*args, **kwargs):
        full_passes.append(time.monotonic())
        entered.set()
        return original(*args, **kwargs)

    adapter.analyze_live = full
    case = {"scenario": scenario, "repeat": index + 1, "queries": []}
    observer = owner._observer_task = asyncio.create_task(HsrCompanionPlugin._observer_loop(owner))
    try:
        if scenario == "expired_idle":
            await wait_until(
                lambda: (
                    bool(owner._runtime.stable_character_ids)
                    and getattr(owner, "_patrol_status", {}).get("decision") == "skip"
                ),
                150,
            )
            before = len(full_passes)
            wall_start = time.monotonic()
            await asyncio.sleep(61)
            case["idle_seconds"] = round(time.monotonic() - wall_start, 3)
            case["idle_fulls"] = len(full_passes) - before
            case["idle_status_before_question"] = (await owner._current_visual_evidence()).get("freshness")
        else:
            await wait_until(entered.is_set, 15)
        case["initial_identity_confirmed"] = bool(owner._runtime.stable_character_ids)
        if scenario == "burst":

            async def ask_after(delay):
                await asyncio.sleep(delay)
                row = await read(owner)
                return {**row, "scheduled_offset_seconds": delay}

            case["queries"] = await asyncio.gather(*(ask_after(delay) for delay in (0, 3, 6)))
        else:
            case["queries"] = [await read(owner)]
        case["all_correct_within_8"] = all(row["correct_within_8"] for row in case["queries"])
        case["patrol"] = dict(getattr(owner, "_patrol_status", {}))
        identity = getattr(owner._runtime, "identity_diagnostics", None)
        runtime = owner._runtime.snapshot()
        case["confirmation"] = (
            identity()
            if callable(identity)
            else {
                key: runtime.get(key) for key in ("pending_character_confirmations", "missing_character_confirmations")
            }
        )
        if callable(identity):
            from plugin.plugins.hsr_companion.runtime.recognition_metrics import observation_health

            case["observation_health"] = observation_health(owner, await owner._current_visual_evidence())
        case["e1_observation_metrics"] = getattr(owner, "_recognition_metrics", None) is not None
    except Exception as exc:
        case["setup_or_call_error"] = type(exc).__name__
        case["all_correct_within_8"] = False
    finally:
        case["full_passes_started"] = len(full_passes)
        owner._observer_stop_requested = True
        observer.cancel()
        await asyncio.gather(observer, return_exceptions=True)
        await drain_analysis_worker(owner)
        for name in ("_draining_vision_worker", "_content_revalidation_worker"):
            worker = getattr(owner, name, None)
            if worker is not None and not worker.done():
                await asyncio.gather(worker, return_exceptions=True)
        adapter.analyze_live = original
    return case


async def replay(args):
    output = Path(args.report)
    with output.open("x", encoding="utf-8") as stream:
        catalog = catalog_for_source()
        adapter = LocalVisionAdapter(catalog)
        try:
            with Image.open(args.frame) as opened:
                image = opened.convert("RGB")
            fixed = (encode(image),)
            selected = adapter.read_character_detail(fixed[0], fields={"characters"})
            name = next(actor for actor in selected["characters"] if actor["entity_id"] == EXPECTED)
            noisy = noise_variants(image, name["region"])
            for index in range(args.rounds):
                for scenario in SCENARIOS:
                    with pressure(scenario == "cpu_pressure") as load:
                        case = await exercise(
                            scenario, index, adapter, catalog, noisy if scenario == "name_noise" else fixed, output
                        )
                    case.update(arm=args.arm, pressure=load)
                    print(json.dumps(case, ensure_ascii=True), file=stream, flush=True)
                    print(
                        json.dumps(
                            {
                                k: case.get(k)
                                for k in (
                                    "arm",
                                    "scenario",
                                    "repeat",
                                    "queries",
                                    "all_correct_within_8",
                                    "setup_or_call_error",
                                )
                            }
                        ),
                        flush=True,
                    )
        finally:
            adapter.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frame", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--arm", choices=("A", "E1"), required=True)
    logging.disable(logging.CRITICAL)
    asyncio.run(replay(parser.parse_args()))


if __name__ == "__main__":
    main()
