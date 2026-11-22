"""Compare bb1838d and D in a real resident/formal chain, offline only.

Saved target frame; optional synthetic relic source. Decorative perturbations
are prebuilt, never OCR substitutions. No installed helper/host/game access.
"""
import argparse
import ast
import asyncio
import copy
import functools
import io
import json
import logging
import subprocess
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from plugin.plugins.hsr_companion import HsrCompanionPlugin
from plugin.plugins.hsr_companion.adapters.local_vision import LocalVisionAdapter
from plugin.plugins.hsr_companion.runtime.live_refresh import drain_analysis_worker
from plugin.plugins.hsr_companion.runtime.timing_detail import snapshot
from plugin.plugins.hsr_companion.scripts.analyze_ocr_bottlenecks import Meter, catalog_for_source
from plugin.plugins.hsr_companion.scripts.replay_request_timing import FileFrames, wait_until
from plugin.plugins.hsr_companion.tests.test_current_observation import plugin_fixture
from plugin.plugins.hsr_companion.tests.test_resident_observer import configure_loop


def encode(image):
    stream = io.BytesIO()
    image.convert("RGB").save(stream, format="BMP")
    return stream.getvalue()


def synthetic_relic(size):
    image = Image.new("RGB", size, (24, 26, 38))
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", round(size[1]*.034))
    def line(text, x, y):
        draw.text((round(x*size[0]), round(y*size[1])), text, font=font, fill="white")
    line("遗器详情", .04, .04)
    for text, y in (("卜者的演算玉兆", .10), ("+15", .17), ("生命值 705", .25),
                    ("攻击力 57", .34), ("生命值 8.2%", .40), ("攻击力 3.8%", .46),
                    ("效果命中 7.7%", .52), ("套装效果", .67), ("应天涉远的卜者", .73),
                    ("卸下", .88), ("强化", .94)):
        line(text, .66, y)
    return image


class ReplayFrames(FileFrames):
    def __init__(self, target, *, animate, switch_case):
        self.templates = {}
        self.scene = "before" if switch_case else "target"
        for key, source in (("target", target), ("before", synthetic_relic(target.size))):
            pictures = [source.copy(), source.copy()]
            if animate:
                # A synthetic decorative animation away from controls/content.
                for i, image in enumerate(pictures):
                    ImageDraw.Draw(image).rectangle((int(image.width*.38), int(image.height*.45),
                        int(image.width*.39), int(image.height*.46)), fill=(30+150*i, 30, 40))
            self.templates[key] = [encode(i) for i in pictures]
        super().__init__(self.templates[self.scene][0])

    def fetch_frame(self, since=0):
        self.payload = self.templates[self.scene][self.sequence % 2]
        return super().fetch_frame(since)


def baseline_loop():
    import plugin.plugins.hsr_companion as root
    source = subprocess.check_output(["git", "show", "bb1838d:__init__.py"], text=True, encoding="utf-8")
    cls = next(n for n in ast.parse(source).body if isinstance(n, ast.ClassDef) and n.name == "HsrCompanionPlugin")
    node = copy.deepcopy(next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_observer_loop"))
    namespace = dict(vars(root))
    exec(compile(ast.Module(body=[node], type_ignores=[]), "bb1838d/__init__.py", "exec"), namespace)
    return namespace["_observer_loop"]


async def case(args, arm, scenario, index, adapter, catalog, meter, loop_function, output):
    owner, values, _ = plugin_fixture(output.parent/(arm+scenario+str(index)))
    owner._catalog, owner._vision = catalog, adapter
    owner._target_character = lambda: ""
    values["observer_settings"] = {"enabled": True, "proactive_enabled": False}
    values["player_context"] = {}
    owner._observer_status.update(enabled=True, proactive_enabled=False)
    with Image.open(args.frame) as opened:
        frames = ReplayFrames(opened.convert("RGB"), animate=args.animate, switch_case=scenario == "switch")
    configure_loop(owner, frames)
    passes = []
    original = adapter.analyze_live

    @functools.wraps(original)
    def counted(*values, **kwargs):
        passes.append(time.monotonic())
        return original(*values, **kwargs)

    adapter.analyze_live = counted
    observer = owner._observer_task = asyncio.create_task(loop_function(owner))
    row = {"arm": arm, "scenario": scenario, "round": index,
           "source_page": "synthetic_relic" if scenario == "switch" else "saved_character_detail",
           "target_page": "saved_character_detail", "decorative_perturbation": args.animate}
    try:
        setup_started = time.perf_counter()
        await wait_until(lambda: getattr(owner, "_last_full_commit_serial", 0) > 0, 180)
        row["first_commit_seconds"] = time.perf_counter()-setup_started
        row["pending_after_first_commit"] = {
            "scene": bool(owner._runtime.pending_scene),
            "identity": bool(owner._runtime.pending_character_ids),
        }
        if not args.first_commit_only:
            # Initial identity confirmation is real work, not stable idle load.
            # Exclude it explicitly rather than removing its product safeguard.
            await wait_until(lambda: not owner._runtime.pending_scene
                and not owner._runtime.pending_character_ids
                and getattr(owner, "_vision_schedule_role", "idle") != "producer", 180)
        row["setup_seconds"] = time.perf_counter()-setup_started
        row["initial_confirmation_settled"] = not (
            owner._runtime.pending_scene or owner._runtime.pending_character_ids)
        row["initial_page"] = (owner._committed_observation.get("page") or {}).get("page_type")
        row["full_count_before_steady"] = len(passes)
        with meter.measure("steady") as resources:
            await asyncio.sleep(args.steady_seconds)
        row["steady_resources"] = resources
        row["steady_full_count"] = len(passes)-row["full_count_before_steady"]
        row["patrol_before_query"] = dict(getattr(owner, "_patrol_status", {}))
        row["producer_active_at_query"] = getattr(owner, "_vision_schedule_role", "idle") == "producer"
        if scenario == "switch":
            frames.scene = "target"
        begin = time.perf_counter()
        result = await owner.hsr_companion_prepare(query="你看到的是谁哇", intent="read_screen", screen_fields=["characters"])
        row["formal_seconds"] = time.perf_counter()-begin
        answer = result.get("screen_identity_answer") or {}
        row["identity_allowed"] = answer.get("allowed") is True
        row["identity_count"] = len(answer.get("names") or [])
        visual = result.get("current_visual_evidence") or {}
        row["expected_identity"] = any(x.get("entity_id") == "character.pearl" for x in visual.get("characters") or [])
        row["query_state"] = (result.get("query_answer") or {}).get("state")
        row["delivered_within_8"] = (row["formal_seconds"] <= 8 and row["identity_allowed"]
                                     and row["expected_identity"] and row["query_state"] == "ready")
        row["refresh"] = getattr(owner, "_last_refresh_diagnostics", {})
    except Exception as exc:
        import traceback
        row["error_type"] = type(exc).__name__
        row["origin"] = [{"file": Path(f.filename).name, "function": f.name, "line": f.lineno}
                         for f in traceback.extract_tb(exc.__traceback__)[-5:]]
        row["delivered_within_8"] = False
    finally:
        row["timing_at_return"] = snapshot(owner)
        row["full_count_at_return"] = len(passes)
        owner._observer_stop_requested = True
        observer.cancel()
        try:
            await observer
        except asyncio.CancelledError:
            pass
        await drain_analysis_worker(owner)
        for name in ("_draining_vision_worker", "_content_revalidation_worker"):
            worker = getattr(owner, name, None)
            if worker is not None and not worker.done():
                await asyncio.gather(worker, return_exceptions=True)
        adapter.analyze_live = original
        row["captures"] = frames.sequence
    return row


async def replay(args):
    output = Path(args.report).resolve()
    if output.exists():
        raise ValueError("report must be new")
    output.parent.mkdir(parents=True, exist_ok=True)
    catalog = catalog_for_source()
    adapter = LocalVisionAdapter(catalog)
    meter = Meter()
    began = time.perf_counter()
    adapter._get_backend().is_available()
    report = {"baseline": "bb1838d", "baseline_scope": "original observer function; remaining production path unchanged",
              "real_ocr": True, "formal_public_entry": True, "sdk_store": "memory_no_messages",
              "question_deadline_changed": False, "name_threshold_changed": False, "runs": [],
              "model_setup_seconds": time.perf_counter()-began}
    current = HsrCompanionPlugin._observer_loop
    before = baseline_loop()
    try:
        for index in range(args.rounds):
            # Reverse the pairing in round two; all failures remain in report.
            arms = [("bb1838d", before), ("D", current)] if index % 2 == 0 else [("D", current), ("bb1838d", before)]
            for scenario in args.scenarios:
                for arm, function in arms:
                    row = await case(args, arm, scenario, index, adapter, catalog, meter, function, output)
                    report["runs"].append(row)
                    output.write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding="utf-8")
                    print(json.dumps({k: row.get(k) for k in ("arm", "scenario", "round", "formal_seconds",
                        "delivered_within_8", "identity_allowed", "expected_identity", "query_state",
                        "steady_full_count", "producer_active_at_query", "error_type")}), flush=True)
    finally:
        adapter.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frame", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--rounds", type=int, choices=(1, 2), default=2)
    parser.add_argument("--steady-seconds", type=float, default=10)
    parser.add_argument("--animate", action="store_true")
    parser.add_argument("--first-commit-only", action="store_true",
                        help="Reproduce the initial, pre-confirmation batch; not stable idle")
    parser.add_argument("--scenarios", nargs="+", choices=("stable", "switch"), default=["stable", "switch"])
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    asyncio.run(replay(args))


if __name__ == "__main__":
    main()
