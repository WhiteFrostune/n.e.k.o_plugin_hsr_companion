"""Compare D and A in a real resident/formal chain, offline only.

Saved target frame; optional synthetic relic source. Decorative perturbations
are prebuilt, never OCR substitutions. No installed helper/host/game access.
"""
import argparse
import asyncio
import functools
import json
import logging
import threading
import time
from pathlib import Path

from PIL import Image
from plugin.plugins.hsr_companion import HsrCompanionPlugin
from plugin.plugins.hsr_companion.adapters.local_vision import LocalVisionAdapter
from plugin.plugins.hsr_companion.runtime.live_refresh import drain_analysis_worker
from plugin.plugins.hsr_companion.runtime.timing_detail import snapshot
from plugin.plugins.hsr_companion.scripts.analyze_ocr_bottlenecks import catalog_for_source
from plugin.plugins.hsr_companion.scripts.replay_request_timing import wait_until
from plugin.plugins.hsr_companion.scripts.replay_static_patrol import ReplayFrames
from plugin.plugins.hsr_companion.tests.test_current_observation import plugin_fixture
from plugin.plugins.hsr_companion.tests.test_resident_observer import configure_loop


async def case(args, arm, scenario, index, adapter, catalog, loop_function, output):
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
    native_spans = []
    in_full = threading.local()
    backend = adapter._get_backend()._backend
    extract = backend.extract_text_with_boxes

    def measured(image):
        if not getattr(in_full, "active", False):
            return extract(image)
        span = {"pass": len(passes), "start": time.perf_counter(), "size": list(image.size)}
        native_spans.append(span)
        try:
            return extract(image)
        finally:
            span["end"] = time.perf_counter()

    backend.extract_text_with_boxes = measured
    original = adapter.analyze_live

    @functools.wraps(original)
    def counted(*values, **kwargs):
        passes.append(time.monotonic())
        in_full.active = True
        try:
            return original(*values, **kwargs)
        finally:
            in_full.active = False

    adapter.analyze_live = counted
    observer = owner._observer_task = asyncio.create_task(loop_function(owner))
    row = {"arm": arm, "scenario": scenario, "round": index,
           "source_page": "synthetic_relic" if scenario == "switch" else "saved_character_detail",
           "target_page": "saved_character_detail", "decorative_perturbation": args.animate}
    try:
        setup_started = time.perf_counter()
        def active_block(pass_no, ordinal):
            blocks = [span for span in native_spans if span["pass"] == pass_no]
            return len(blocks) >= ordinal and "end" not in blocks[ordinal-1]

        if scenario in {"cold", "cold_late_probe"}:
            ordinal = (2, 4)[index % 2] if scenario == "cold_late_probe" else 1
            await wait_until(lambda: active_block(1, ordinal), 180)
            row["identity_pending"] = not bool(owner._runtime.stable_character_ids)
        elif scenario == "mid_full":
            # Start in the first constituent of tile 1 or 2, after the scene probe.
            ordinal = ((5, 9) if arm == "A" else (2, 3))[index % 2]
            await wait_until(lambda: active_block(1, ordinal), 180)
        else:
            await wait_until(lambda: getattr(owner, "_last_full_commit_serial", 0) > 0, 180)
            if scenario == "initial":
                row["identity_pending"] = bool(owner._runtime.pending_character_ids)
                if row["identity_pending"]:
                    await wait_until(lambda: active_block(2, 1), 180)
            else:
                await wait_until(lambda: not owner._runtime.pending_scene
                    and not owner._runtime.pending_character_ids
                    and getattr(owner, "_vision_schedule_role", "idle") != "producer", 180)
                await asyncio.sleep(args.steady_seconds)
        row["setup_seconds"] = time.perf_counter()-setup_started
        row["producer_active_at_query"] = getattr(owner, "_vision_schedule_role", "idle") == "producer"
        if scenario == "switch":
            frames.scene = "target"
        begin = time.perf_counter()
        block = next((span for span in reversed(native_spans) if "end" not in span), None)
        if block:
            row["native_at_query"] = block
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
        backend.extract_text_with_boxes = extract
        adapter.analyze_live = original
        row["native_blocks"] = [dict(size=b["size"], seconds=b["end"]-b["start"])
                                for b in native_spans if "end" in b]
        if block := row.get("native_at_query"):
            row["native_at_query"] = {"size": block["size"], "seconds": block["end"]-block["start"],
                                      "residual_seconds": max(0., block["end"]-begin)}
        row["captures"] = frames.sequence
    return row


async def replay(args):
    output = Path(args.report).resolve()
    if output.exists():
        raise ValueError("report must be new")
    output.parent.mkdir(parents=True, exist_ok=True)
    catalog = catalog_for_source()
    adapter = LocalVisionAdapter(catalog)
    began = time.perf_counter()
    adapter._get_backend().is_available()
    report = {"arm": args.arm, "real_ocr": True, "formal_public_entry": True,
              "sdk_store": "memory_no_messages", "runs": [], "model_setup_seconds": time.perf_counter()-began}
    try:
        for index in range(args.rounds):
            for scenario in args.scenarios:
                row = await case(args, args.arm, scenario, index, adapter, catalog,
                                 HsrCompanionPlugin._observer_loop, output)
                report["runs"].append(row)
                output.write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding="utf-8")
                print(json.dumps({k: row.get(k) for k in ("arm", "scenario", "round", "formal_seconds",
                    "delivered_within_8", "producer_active_at_query", "native_at_query", "error_type")}), flush=True)
    finally:
        adapter.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frame", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--arm", choices=("D", "A"), required=True)
    parser.add_argument("--steady-seconds", type=float, default=10)
    parser.add_argument("--animate", action="store_true")
    scenarios = ("initial", "cold", "mid_full", "stable", "switch")
    parser.add_argument("--scenarios", nargs="+", choices=(*scenarios, "cold_late_probe"), default=list(scenarios))
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    asyncio.run(replay(args))


if __name__ == "__main__":
    main()
