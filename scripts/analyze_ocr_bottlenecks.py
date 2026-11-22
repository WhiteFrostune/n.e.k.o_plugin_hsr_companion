"""Offline resource/geometry experiments; never modify production policy.

Only saved frames are accepted. No host/helper sockets or game capture.
Numbers are local observations, not model/chat/live latency guarantees.
Run as a plugin module with the same SDK test harness as other audit scripts.
"""
import argparse
import asyncio
import functools
import io
import json
import logging
import os
import statistics
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import psutil
from PIL import Image
from plugin.plugins.hsr_companion.adapters.local_vision import LocalVisionAdapter
from plugin.plugins.hsr_companion.data import ExternalKnowledgeCatalog
from plugin.plugins.hsr_companion.runtime.current_regions import _layout_regions
from plugin.plugins.hsr_companion.runtime.live_refresh import drain_analysis_worker
from plugin.plugins.hsr_companion.scripts.replay_request_timing import FileFrames, run_case, wait_until
from plugin.plugins.hsr_companion.tests.test_current_observation import plugin_fixture
from plugin.plugins.hsr_companion.tests.test_resident_observer import configure_loop


class Meter:
    def __init__(self):
        self.process = psutil.Process()
        self.local = threading.local()
        self.calls = []

    def memory(self):
        info = self.process.memory_info()
        virtual = psutil.virtual_memory()
        return {"rss_mib": info.rss/2**20, "private_mib": getattr(info, "private", info.vms)/2**20,
                "threads": self.process.num_threads(), "page_faults": getattr(info, "num_page_faults", None),
                "system_available_mib": virtual.available/2**20, "system_memory_percent": virtual.percent}

    @contextmanager
    def measure(self, label):
        samples = []
        stop = threading.Event()
        before = self.process.cpu_times()
        began = time.perf_counter()
        depth = getattr(self.local, "depth", 0)
        self.local.depth = depth+1
        initial = self.memory() if depth == 0 else {}

        def sample():
            psutil.cpu_percent(None)
            while not stop.wait(.25):
                samples.append({**self.memory(), "system_cpu_percent": psutil.cpu_percent(None)})

        monitor = threading.Thread(target=sample, daemon=True) if depth == 0 else None
        if monitor is not None:
            monitor.start()
        state = {"label": label}
        previous = getattr(self.local, "label", None)
        self.local.label = label
        try:
            yield state
        finally:
            self.local.label = previous
            elapsed = time.perf_counter()-began
            after = self.process.cpu_times()
            stop.set()
            if monitor is not None:
                monitor.join(1)
            self.local.depth = depth
            cpu = (after.user+after.system)-(before.user+before.system)
            state.update(seconds=elapsed, cpu_seconds=cpu, core_equivalents=cpu/elapsed,
                         machine_cpu_percent=100*cpu/elapsed/psutil.cpu_count(), initial_memory=initial,
                         final_memory=self.memory() if depth == 0 else {},
                         peak_rss_mib=max([initial.get("rss_mib", 0), *[x["rss_mib"] for x in samples]]),
                         peak_private_mib=max([initial.get("private_mib", 0), *[x["private_mib"] for x in samples]]),
                         mean_system_cpu_percent=statistics.mean(x["system_cpu_percent"] for x in samples) if samples else None)


class Component:
    def __init__(self, target, meter, stage):
        self.target, self.meter, self.stage = target, meter, stage

    def __getattr__(self, name):
        return getattr(self.target, name)

    def __setattr__(self, name, value):
        if name in {"target", "meter", "stage"}:
            object.__setattr__(self, name, value)
        else:
            setattr(self.target, name, value)

    def __call__(self, *args, **kwargs):
        image = args[0]
        shapes = [list(x.shape) for x in image] if isinstance(image, list) else [list(image.shape)]
        with self.meter.measure(self.stage) as state:
            try:
                return self.target(*args, **kwargs)
            finally:
                self.meter.calls.append({"role": getattr(self.meter.local, "role", "query"),
                                         "stage": self.stage, "input_shapes": shapes, "metrics": state})


def attach(adapter, meter):
    wrapped = adapter._get_backend()
    backend = wrapped._backend
    detector = backend.engine.text_det
    detector.infer = Component(detector.infer, meter, "det_inference")
    detector.postprocess_op = Component(detector.postprocess_op, meter, "det_postprocess")
    original_preprocess = detector.get_preprocess
    detector.get_preprocess = lambda *args: Component(original_preprocess(*args), meter, "det_preprocess")
    backend.engine.text_rec.session = Component(backend.engine.text_rec.session, meter, "rec_inference")
    for name in ("text_det", "text_rec"):
        target = getattr(backend.engine, name)
        setattr(backend.engine, name, Component(target, meter, name))
    for name in ("extract_text_with_boxes", "recognize_line"):
        original = getattr(backend, name)

        def make(function, operation):
            def call(image, *args, **kwargs):
                with meter.measure(operation) as state:
                    try:
                        return function(image, *args, **kwargs)
                    finally:
                        meter.calls.append({"role": getattr(meter.local, "role", "query"), "stage": operation,
                                            "input_size": list(image.size), "metrics": state})
            return call
        setattr(backend, name, make(original, name))
    settings = {}
    for name in ("text_det", "text_rec"):
        target = getattr(backend.engine, name).target
        infer = getattr(target, "infer", None) or getattr(target, "session", None)
        session = getattr(infer, "session", None)
        if session is not None:
            opts = session.get_session_options()
            settings[name] = {"intra_threads": opts.intra_op_num_threads, "inter_threads": opts.inter_op_num_threads}
    return {"backend": type(backend).__name__, "model": backend.model, "sessions": settings}


def payload(path, codec):
    if codec == "original":
        return Path(path).read_bytes()
    with Image.open(path) as image:
        output = io.BytesIO()
        image.convert("RGB").save(output, format=codec)
        return output.getvalue()


def read(adapter, data, meter, label, layout=None):
    begin = len(meter.calls)
    with meter.measure(label) as state:
        value = adapter.read_character_detail(data, fields=frozenset({"characters"}), layout_regions=layout)
    state.update(confirmed=len((value or {}).get("characters") or []) == 1,
                 acquisition=((value or {}).get("recognition") or {}).get("acquisition"),
                 calls=meter.calls[begin:])
    return state


def full(adapter, data, meter, label):
    meter.local.role = "full"
    begin = len(meter.calls)
    with meter.measure(label) as state:
        value = adapter.analyze_live(data, cancelled=lambda: False, on_scene=lambda _: None,
                                     yield_requested=lambda: False)
    meter.local.role = "query"
    state.update(calls=meter.calls[begin:], page_type=(value.get("page") or {}).get("page_type"),
                 native=(value.get("recognition") or {}).get("text_reader"))
    # Native metadata includes text errors in some paths: retain only counts/cost.
    native = state.pop("native") or {}
    state["reader_summary"] = {k: native[k] for k in ("strategy", "tile_count", "completed_tiles", "budget_exceeded") if k in native}
    return state, value


def catalog_for_source():
    root = Path(__file__).resolve().parents[1]
    registry = json.loads((root/"resources/character_registry.json").read_text("utf-8"))
    return ExternalKnowledgeCatalog(registry=registry)


def save(path, report):
    path.write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding="utf-8")


def paired(args, report, output, catalog, adapter, meter):
    old = payload(args.old_frame, "original")
    new = payload(args.new_frame, "original")
    bmp = payload(args.new_frame, "BMP")
    report["paired"] = []
    for index in range(args.rounds):
        for label, data in (("old_png", old), ("new_png", new), ("new_bmp", bmp)):
            result = read(adapter, data, meter, label+str(index))
            report["paired"].append(result)
            save(output, report)
            print(json.dumps({"pair": label, "round": index, "seconds": result["seconds"],
                              "confirmed": result["confirmed"], "acquisition": result["acquisition"]}), flush=True)
    measured, value = full(adapter, bmp, meter, "one_full")
    report["native_full"] = measured
    layout = _layout_regions(SimpleNamespace(_committed_observation=value))
    report["cached_layout_geometry"] = layout
    report["cached_layout_reads"] = [read(adapter, bmp, meter, "cached_layout"+str(i), layout) for i in range(args.rounds)]
    save(output, report)
    passes = []
    original_live = adapter.analyze_live

    @functools.wraps(original_live)
    def measured_live(*values, **kwargs):
        meter.local.role = "full"
        with meter.measure("resident_full") as metrics:
            try:
                return original_live(*values, **kwargs)
            finally:
                passes.append(metrics)
                meter.local.role = "query"

    adapter.analyze_live = measured_live
    try:
        with meter.measure("resident_idle") as state:
            state["result"] = asyncio.run(run_case("idle", bmp, bmp, catalog, adapter, output.parent))
    finally:
        adapter.analyze_live = original_live
    state["resident_full_native_passes"] = passes
    state["ocr_calls"] = meter.calls[-30:]
    report["resident_idle"] = state
    save(output, report)


def resources(args, report, output, catalog, adapter, meter):
    data = payload(args.new_frame, "BMP")
    report["resources"] = {"one_backend_memory": meter.memory()}
    second = LocalVisionAdapter(catalog)
    with meter.measure("second_backend_setup") as state:
        report["resources"]["second_backend_config"] = attach(second, meter)
    report["resources"]["second_backend_setup"] = state
    report["resources"]["two_backend_memory"] = meter.memory()
    report["resources"]["second_backend_warmup"] = read(second, data, meter, "second_warmup")
    runs = []
    try:
        for index in range(args.rounds):
            entered = threading.Event()
            backend = adapter._get_backend()._backend
            original = backend.extract_text_with_boxes

            def signal(*args, **kwargs):
                entered.set()
                return original(*args, **kwargs)

            backend.extract_text_with_boxes = signal
            result = {}

            def produce():
                try:
                    result["full"], _ = full(adapter, data, meter, "independent_full")
                except Exception as exc:
                    result["full_error"] = type(exc).__name__

            worker = threading.Thread(target=produce)
            worker.start()
            try:
                if not entered.wait(60):
                    raise TimeoutError("owned background worker did not enter OCR")
                result["query"] = read(second, data, meter, "independent_query"+str(index))
            finally:
                worker.join()
                backend.extract_text_with_boxes = original
            runs.append(result)
            report["resources"]["independent_backend_runs"] = runs
            save(output, report)
            print(json.dumps({"independent_query": index, "seconds": result["query"]["seconds"],
                              "confirmed": result["query"]["confirmed"]}), flush=True)
    finally:
        second.close()
    report["resources"]["after_close_memory"] = meter.memory()
    save(output, report)


def child(args):
    # This child only reads a saved frame. It is always stopped via its own
    # Popen handle, never by process name or a PID discovered on the machine.
    adapter = LocalVisionAdapter(catalog_for_source())
    data = payload(args.new_frame, "BMP")
    adapter._get_backend().is_available()
    adapter.read_character_detail(data, fields=frozenset({"characters"}))
    print(json.dumps({"state": "ready", "pid": os.getpid()}), flush=True)
    backend = adapter._get_backend()._backend
    original = backend.extract_text_with_boxes
    signalled = False

    def signal(*values, **kwargs):
        nonlocal signalled
        if not signalled:
            signalled = True
            print(json.dumps({"state": "native_entered"}), flush=True)
        return original(*values, **kwargs)

    backend.extract_text_with_boxes = signal
    adapter.analyze_live(data, cancelled=lambda: False, on_scene=lambda _: None, yield_requested=lambda: False)
    print(json.dumps({"state": "full_completed"}), flush=True)
    sys.stdin.readline()  # Stay owned/alive for the parent's exit measurement.


def processes(args, report, output, adapter, meter):
    report["process"] = []
    data = payload(args.new_frame, "BMP")
    report["process_parent_warmup"] = read(adapter, data, meter, "process_parent_warmup")
    for index in range(2):
        # Inherit only interpreter search paths; the child sees the same
        # explicit worktree via a module bootstrap, no installed substitution.
        bootstrap = ("import sys,runpy; sys.path="+repr(sys.path)+"; import plugin.plugins; "
                     "plugin.plugins.__path__.insert(0,"+repr(str(Path(__file__).resolve().parents[2]))+"); "
                     "sys.argv="+repr(["analysis-worker", "--worker", "--new-frame", args.new_frame])+
                     "; runpy.run_module('plugin.plugins.hsr_companion.scripts.analyze_ocr_bottlenecks',run_name='__main__')")
        started = time.perf_counter()
        worker = subprocess.Popen([sys.executable, "-B", "-c", bootstrap], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        row = {}
        owned_child = None
        try:
            # Only fixed status JSON is emitted by the child, never OCR text.
            first = json.loads(worker.stdout.readline())
            row["startup_and_warmup_seconds"] = time.perf_counter()-started
            second = json.loads(worker.stdout.readline())
            row["states"] = [first.get("state"), second.get("state")]
            child_pid = first["pid"]
            launcher = psutil.Process(worker.pid)
            if child_pid != worker.pid and child_pid not in {p.pid for p in launcher.children(recursive=True)}:
                raise RuntimeError("child handshake did not match this experiment process tree")
            owned_child = psutil.Process(child_pid)
            row["measured_real_interpreter"] = True
            row["launcher_equals_interpreter"] = child_pid == worker.pid
            mem = owned_child.memory_info()
            row["child_rss_mib"] = mem.rss/2**20
            row["child_private_mib"] = getattr(mem, "private", mem.vms)/2**20
            row["child_threads"] = owned_child.num_threads()
            row["child_cpu_seconds_before_stop"] = sum(owned_child.cpu_times()[:2])
            before = time.perf_counter()
            owned_child.terminate()
            owned_child.wait(timeout=20)
            worker.wait(timeout=20)
            row["stop_seconds"] = time.perf_counter()-before
            row["query"] = read(adapter, data, meter, "after_process_stop"+str(index))
            row["stop_plus_native_query_seconds"] = row["stop_seconds"]+row["query"]["seconds"]
        finally:
            if owned_child is not None and owned_child.is_running():
                owned_child.terminate()
                owned_child.wait(timeout=20)
            if worker.poll() is None:
                worker.terminate()
                worker.wait(timeout=20)
            worker.stdin.close()
            worker.stdout.close()
        report["process"].append(row)
        save(output, report)
        print(json.dumps({"process_trial": index, "seconds": row["stop_plus_native_query_seconds"]}), flush=True)


async def patrol(args, report, output, catalog, adapter, meter):
    owner, values, _ = plugin_fixture(output.parent/"patrol-memory")
    owner._catalog, owner._vision = catalog, adapter
    owner._target_character = lambda: ""
    owner._observer_status.update(enabled=True, proactive_enabled=False)
    values["observer_settings"] = {"enabled": True, "proactive_enabled": False}
    values["player_context"] = {}
    frames = FileFrames(payload(args.new_frame, "BMP"))
    configure_loop(owner, frames)
    began = time.perf_counter()
    passes = []
    commits = []
    original_live = adapter.analyze_live
    original_apply = owner._apply_local_analysis

    @functools.wraps(original_live)
    def observed(*args, **kwargs):
        record = {"started_seconds": time.perf_counter()-began}
        passes.append(record)
        with meter.measure("patrol_full") as metrics:
            try:
                return original_live(*args, **kwargs)
            finally:
                record.update(ended_seconds=time.perf_counter()-began, metrics=metrics)

    async def apply(*args, **kwargs):
        value = await original_apply(*args, **kwargs)
        commits.append({"at_seconds": time.perf_counter()-began,
                        "serial": getattr(owner, "_observation_serial", 0),
                        "incomplete": (value.get("delivery") or {}).get("reason") == "analysis_yielded_incomplete"})
        return value

    adapter.analyze_live = observed
    owner._apply_local_analysis = apply
    task = owner._observer_task = asyncio.create_task(owner._observer_loop())
    try:
        with meter.measure("unchanged_patrol") as state:
            await wait_until(lambda: len(commits) >= 2, 180)
        report["patrol"] = state
    except TimeoutError:
        report["patrol"] = {"setup_timeout": True}
    finally:
        owner._observer_stop_requested = True
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await drain_analysis_worker(owner)
        worker = getattr(owner, "_draining_vision_worker", None)
        if worker is not None and not worker.done():
            await asyncio.gather(worker, return_exceptions=True)
        adapter.analyze_live = original_live
        report["patrol"].update(passes=passes, commits=commits, all_frame_pixels_identical=True,
                                capture_fetch_count=frames.sequence)
        save(output, report)


def estimate_schedule(native_full, query_samples, horizon=120):
    """Counterfactual arithmetic, not a claim of an implemented scheduler.

    Questions are assumed uniformly distributed over a stable patrol cycle.
    Native calls cannot be interrupted in A. No cancellation/clock constants
    are modified by this function. Layered spans must never be added twice.
    """
    duration = native_full["seconds"]
    blocks = [c["metrics"]["seconds"] for c in native_full["calls"]
              if c["stage"] in {"extract_text_with_boxes", "recognize_line"}]
    query_cost = statistics.median(query_samples)
    scenarios = {}
    for idle_gap in (4, 15, 60):
        cycle = duration+idle_gap
        remaining = sum(t*t/2 for t in blocks)/cycle
        scenarios[str(idle_gap)] = {"busy_fraction": duration/cycle,
                                   "expected_native_residual_seconds": remaining,
                                   "native_query_plus_expected_residual_seconds": query_cost+remaining,
                                   "full_starts_in_horizon": len(range(0, int(horizon*1000), max(1, int(cycle*1000))))}
    return {"assumptions": "uniform arrival, unchanged screen, measured native blocks, no proof-review queue",
            "query_median_native_seconds": query_cost, "native_call_seconds": blocks,
            "worst_native_residual_seconds": max(blocks, default=0), "steady_cycle_estimates": scenarios,
            "unchanged_skip_steady_full_jobs": 0,
            "unchanged_skip_excludes_initial_confirmation_and_required_content_changes": True,
            "does_not_include_capture_or_snapshot_review": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--old-frame")
    parser.add_argument("--new-frame", required=True)
    parser.add_argument("--report")
    parser.add_argument("--mode", choices=("paired", "resources", "process", "patrol", "all"), default="all")
    parser.add_argument("--rounds", type=int, choices=(1, 2, 3), default=3)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    if args.worker:
        child(args)
        return
    output = Path(args.report).resolve()
    if output.exists():
        raise ValueError("report must be new")
    output.parent.mkdir(parents=True, exist_ok=True)
    meter = Meter()
    catalog = catalog_for_source()
    adapter = LocalVisionAdapter(catalog)
    report = {"production_files_changed": False, "live_capture": False, "samples_are_saved_frames": True,
              "logical_cpus": psutil.cpu_count(), "physical_cpus": psutil.cpu_count(logical=False),
              "process_affinity_count": len(meter.process.cpu_affinity()), "memory_before_model": meter.memory(),
              "cpu_accounting": "process CPU seconds / elapsed; includes all concurrent owned threads; 1 core=100%",
              "component_spans_overlap": True}
    try:
        with meter.measure("model_setup") as state:
            report["backend"] = attach(adapter, meter)
        report["setup"] = state
        save(output, report)
        if args.mode in {"paired", "all"}:
            paired(args, report, output, catalog, adapter, meter)
        if args.mode in {"resources", "all"}:
            resources(args, report, output, catalog, adapter, meter)
        if args.mode in {"process", "all"}:
            processes(args, report, output, adapter, meter)
        if args.mode == "patrol":
            asyncio.run(patrol(args, report, output, catalog, adapter, meter))
        if report.get("native_full") and report.get("paired"):
            report["counterfactual_schedule"] = estimate_schedule(report["native_full"],
                [q["seconds"] for q in report["paired"] if q["label"].startswith("new_bmp")])
            save(output, report)
    except Exception as exc:
        import traceback
        report["fatal"] = {"type": type(exc).__name__, "origin": [
            {"file": Path(f.filename).name, "function": f.name, "line": f.lineno}
            for f in traceback.extract_tb(exc.__traceback__)[-5:]]}
        save(output, report)
        raise
    finally:
        adapter.close()


if __name__ == "__main__":
    main()
