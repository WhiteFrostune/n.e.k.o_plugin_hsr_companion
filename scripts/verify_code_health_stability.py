"""Bounded resident/source acceptance on one frozen package and helper binary.

Only real frames/recognition and the formal source entry are evaluated. Host
memory/events and notification submission are local fixtures, never cat/model
acceptance. No installation, game input, network service or saved screenshot.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import os
import socket
import subprocess
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.request import ProxyHandler, build_opener

from .audit_field_handoff_isolated import stop_owned, ui_action, ui_context
from .field_pipeline_probe import summary
from .verify_roundtrip_c_acceptance import loopback_only

ROOT = Path(__file__).resolve().parents[1]
HOST = "http://127.0.0.1:48916"
POINTER = Path("C:/Users/cai/AppData/Local/N.E.K.O/data-packs/hsr_companion/assistant/current.json")
INSTALLED = Path("C:/Users/cai/AppData/Local/N.E.K.O/.neko-plugin-installations/plugins/hsr_companion")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree(path):
    return {p.relative_to(path).as_posix(): sha(p) for p in path.rglob("*")
            if p.is_file() and p.suffix != ".pyc" and "__pycache__" not in p.parts}


def check_frozen(freeze, *, root=ROOT):
    package = (root / freeze["package"]).resolve(strict=True)
    assert package.is_relative_to(root / "dist")
    assert sha(package) == freeze["package_sha256"], "Frozen package changed"
    prefix = "payload/plugins/hsr_companion/"
    with zipfile.ZipFile(package) as z:
        expected = {n[len(prefix):]: hashlib.sha256(z.read(n)).hexdigest()
                    for n in z.namelist() if n.startswith(prefix) and not n.endswith("/")}
    assert expected and all(sha(root / n) == h for n, h in expected.items()), "Candidate source changed"
    executable = Path(freeze["assistant_executable"]).resolve(strict=True)
    assert executable.is_relative_to(POINTER.parent)
    assert sha(executable) == freeze["assistant_sha256"], "Helper binary changed"
    assert sha(POINTER) == freeze["assistant_pointer_sha256"], "Helper pointer changed"
    return expected, executable


def observer_enabled(context):
    if context.get("plugin", {}).get("runtime_enabled") is False:
        return False
    enabled = (context.get("state") or {}).get("observer", {}).get("enabled")
    if type(enabled) is not bool:
        raise RuntimeError("Observer state unavailable; do not assume paused")
    return enabled


def live_context():
    with build_opener(ProxyHandler({})).open(
            HOST + "/plugin/hsr_companion/hosted-ui/context?kind=panel&id=demo", timeout=10) as response:
        return json.load(response)


async def cleanup(owner, adapter, process, *, enabled, pause_attempted, result):
    """Independent cleanup steps: a worker error must not skip restoration."""
    errors = result.setdefault("cleanup_errors", [])
    if owner is not None:
        try:
            from ..runtime.followup import cancel_followup
            from ..runtime.live_refresh import drain_analysis_worker
            cancel_followup(owner, "acceptance_cleanup")
            owner._observer_stop_requested = True
            task = getattr(owner, "_observer_task", None)
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await drain_analysis_worker(owner)
        except Exception as exc:
            errors.append({"stage": "source_worker", "type": type(exc).__name__})
    if adapter is not None:
        try:
            adapter.close()
        except Exception as exc:
            errors.append({"stage": "source_adapter", "type": type(exc).__name__})
    try:
        await asyncio.to_thread(stop_owned, process)
    except Exception as exc:
        errors.append({"stage": "owned_helper", "type": type(exc).__name__})
    try:
        if pause_attempted and enabled:
            await asyncio.to_thread(ui_action, HOST, "start_auto_observation")
        current = await asyncio.to_thread(live_context)
        result["restored"] = observer_enabled(current) is enabled
    except Exception as exc:
        errors.append({"stage": "restore_observer", "type": type(exc).__name__})
        result["restored"] = False


def packet_view(packet):
    """Retain the actual answer, omitting archives, account IDs and long guides."""
    view = {k: copy.deepcopy(packet[k]) for k in (
        "query_plan", "query_answer", "request_completion", "delivery_receipt", "query_timing",
        "followup", "equipment_answer", "answer_progress", "page_type", "scene", "screen_facts", "navigation") if k in packet}
    if "current_visual_evidence" in packet:
        value = packet["current_visual_evidence"]
        view["current_visual_evidence"] = {k: copy.deepcopy(value[k]) for k in (
            "fresh", "page_type", "scene", "read_scope", "observed_at", "characters", "screen_facts") if k in value}
    guide = packet.get("guidance_answer")
    if guide is not None:
        view["guidance_answer"] = {k: copy.deepcopy(guide[k]) for k in (
            "assessment", "completion", "reply_plan") if k in guide}
    return view


def verdict(row):
    deliveries = [row["packet"], *[r["payload"] for r in row.get("followups", [])]]
    services = [p.get("request_completion") or {} for p in deliveries]
    return {"service_completed": any(s.get("service_ready") for s in services),
            "direct_ready": (row["packet"].get("query_answer") or {}).get("state") == "ready",
            "followup_submitted_complete": any(r["payload"].get("request_completion", {}).get("delivered_complete")
                                               for r in row.get("followups", [])),
            "truth_check": "pending_human_confirmation", "cat_acceptance": False}


async def acknowledged_transition(owner, run_root, phase, emit):
    marker = run_root / (phase + "_ack")
    emit("awaiting_acknowledged_transition", phase=phase, marker=str(marker),
         trigger="human_destination_ack_not_cold_first_frame")
    until = time.monotonic() + 300
    while not marker.exists():
        if owner._D_abort_marker.exists():
            raise RuntimeError("Operator aborted; failure retained")
        if time.monotonic() >= until:
            raise TimeoutError("Destination acknowledgment absent: " + phase)
        await asyncio.sleep(.25)


async def campaign(owner, args, result, emit, save):
    from ..runtime import followup
    from ..runtime.observation_store import ObservationStore

    events, submissions, receipts = [], [], []
    from plugin.sdk.plugin import NekoPluginBase
    owner.ctx = SimpleNamespace(bus=NekoPluginBase(SimpleNamespace(plugin_id="isolated-health-stability",
        metadata={}, bus=SimpleNamespace(memory=SimpleNamespace(get=lambda **kw: list(events))))).bus)
    owner._target_character = lambda: "hsr_isolated_test"
    owner.push_message = lambda **kw: submissions.append(copy.deepcopy(kw)) or {"submitted": True}
    active = {"phase": "baseline"}
    original_record, original_commit = followup.record_answer_delivery, ObservationStore.commit

    def record(target, **kw):
        value = original_record(target, **kw)
        if target is owner:
            receipts.append({"phase": active["phase"], "payload": packet_view(kw["payload"]),
                             "projection": copy.deepcopy(kw.get("message_projection")),
                             "submitted": kw.get("submitted")})
        return value

    def commit(store, analysis, **kw):
        value = original_commit(store, analysis, **kw)
        if store.owner is owner:
            result["commits"].append({"at": time.time(), "phase": active["phase"],
                "observation": summary(getattr(owner, "_committed_observation", None))})
        return value

    async def formal(phase, query, intent, fields=None, declaration=None):
        active["phase"] = phase
        kwargs = {"query": query, "intent": intent}
        if fields is not None:
            kwargs["screen_fields"] = fields
        if declaration is not None:
            kwargs["consultation"] = declaration
        start, receipt_start, push_start = time.monotonic(), len(receipts), len(submissions)
        row = {"phase": phase, "actual_parameters": kwargs, "asked_at": time.time(),
               "prequery": summary(getattr(owner, "_committed_observation", None))}
        result["queries"].append(row)
        packet = await owner.hsr_companion_prepare(**kwargs)
        row.update(direct_seconds=round(time.monotonic() - start, 3), packet=packet_view(packet))
        emit("direct", phase=phase, seconds=row["direct_seconds"], answer=packet.get("query_answer"))
        save()
        pending = getattr(owner, "_pending_answer", None)
        if pending is not None:
            deadline, generation = pending.expires_at, owner._answer_query_generation
            events.append({"type": "user_message", "lanlan": "hsr_isolated_test", "content": "好", "_ts": time.time()})
            confirmation = await owner.hsr_companion_prepare(query="好", pending_reply={
                "request_id": pending.request_id, "relation": "confirm", "player_message": "好"})
            row["local_confirmation"] = {"accepted": confirmation["pending_reply"]["accepted"],
                "deadline_unchanged": pending.expires_at == deadline,
                "generation_unchanged": owner._answer_query_generation == generation,
                "is_real_cat_event": False}
            task = getattr(owner, "_followup_task", None)
            while task is not None and not task.done():
                if owner._D_abort_marker.exists():
                    raise RuntimeError("Operator aborted; failure retained")
                await asyncio.wait({task}, timeout=.5)
            if task is not None:
                await task
        row.update(followup_status=copy.deepcopy(getattr(owner, "_followup_status", {})),
                   followups=copy.deepcopy(receipts[receipt_start:]),
                   notifications=copy.deepcopy(submissions[push_start:]),
                   total_seconds=round(time.monotonic() - start, 3))
        row["verdict"] = verdict(row)
        emit("query_finished", phase=phase, seconds=row["total_seconds"], verdict=row["verdict"])
        save()

    with patch.object(followup, "record_answer_delivery", record), patch.object(ObservationStore, "commit", commit):
        # Baseline setup is bounded and never repaired by a warm-up query.
        until = time.monotonic() + 60
        while time.monotonic() < until:
            page = (getattr(owner, "_committed_observation", None) or {}).get("page", {}).get("page_type")
            if page == "exploration_hud":
                break
            await asyncio.sleep(.25)
        else:
            raise RuntimeError("Exploration baseline not established in 60 seconds")
        result["baseline"] = summary(getattr(owner, "_committed_observation", None))
        if not args.transitions_only:
            start = time.monotonic()
            resident = result["freeze"]["limits"]["resident_seconds"]
            count = result["freeze"]["limits"]["resident_formal_queries"]
            emit("resident_started", seconds=resident, queries=count)
            for i in range(count):
                due = start + resident * i / (count - 1)
                while time.monotonic() < due:
                    if owner._D_abort_marker.exists():
                        raise RuntimeError("Operator aborted; failure retained")
                    await asyncio.sleep(min(1, due - time.monotonic()))
                await formal(f"resident_{i + 1}", "现在在哪，任务目标是什么？", "read_screen", ["location", "quest"])
            result["resident_elapsed_seconds"] = round(time.monotonic() - start, 3)
        for phase, query, intent, declaration in [
            ("attributes", "真珠这面板怎么样，接下来先养哪儿？", "assess_panel",
             {"scope": "current_panel", "subject_state": "specified", "character": "真珠"}),
            ("relic", "这件遗器给真珠用怎么样？", "assess_relic",
             {"scope": "current_relic", "subject_state": "specified", "character": "真珠"}),
            ("changed_relic", "换成这件呢？", "continue_consultation",
             {"scope": "current_relic", "subject_state": "inherit"}),
        ]:
            await acknowledged_transition(owner, Path(args.run_root), phase, emit)
            await formal(phase, query, intent, declaration=declaration)


async def run(args):
    from ..adapters.local_vision import LocalVisionAdapter
    from ..assistant import VisionAssistantClient
    from ..data import ExternalKnowledgeCatalog
    from ..data.avatar_pack import AvatarTemplatePack
    from ..data.vision_runtime import VisionMatchRuntime
    from ..tests.test_current_observation import plugin_fixture
    from ..tests.test_resident_observer import configure_loop

    run_root = Path(args.run_root).resolve(strict=True)
    assert run_root.is_relative_to(ROOT / "dist")
    assert 1024 <= args.port <= 65535 and args.port != 8219, "Never bind the installed helper port"
    report = run_root / "live.json"
    assert not report.exists(), "Do not overwrite an attempt"
    freeze = json.loads((run_root / "freeze.json").read_bytes())
    expected, executable = check_frozen(freeze)
    installed_before = tree(INSTALLED)
    result = {"freeze": freeze, "queries": [], "commits": [], "events": [], "samples": [],
              "installed": False, "host_messages": 0, "cat_acceptance": False, "restored": False}

    def save():
        temporary = report.with_suffix(".tmp")
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(report)

    def emit(kind, **kw):
        value = {"kind": kind, "at": time.time(), **kw}
        result["events"].append(value)
        print(json.dumps(value, ensure_ascii=True), flush=True)
        save()

    owner = adapter = process = sampler = None
    pause_attempted, enabled = False, False
    context_before = live_context()
    enabled = observer_enabled(context_before)
    try:
        if enabled:
            pause_attempted = True
            await asyncio.to_thread(ui_action, HOST, "stop_auto_observation")
            assert (await asyncio.to_thread(ui_context, HOST)).get("observer", {}).get("enabled") is False
        result["original_observer_enabled"] = enabled
        with socket.socket() as check:
            check.bind(("127.0.0.1", args.port))
        process = subprocess.Popen([str(executable), "--port", str(args.port), "--parent-pid", str(os.getpid())],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        client = VisionAssistantClient(f"http://127.0.0.1:{args.port}")
        for _ in range(40):
            health = await asyncio.to_thread(client.health)
            if health.get("connected") and health.get("game", {}).get("running"):
                break
            await asyncio.sleep(.25)
        else:
            raise RuntimeError("Existing helper binary could not supply the running game")
        emit("isolated_helper_ready", service_version=client.service_version, health=health)
        registry = json.loads((ROOT / "resources/character_registry.json").read_bytes())
        catalog = ExternalKnowledgeCatalog(registry=registry)
        avatars = AvatarTemplatePack(json.loads((ROOT / "resources/avatar_templates.json").read_bytes()),
            root=catalog.root, runtime=VisionMatchRuntime(root=catalog.root))
        adapter = LocalVisionAdapter(catalog, avatar_pack=avatars)
        started = time.monotonic()
        adapter._get_backend()  # Load backend only; no frame/query warm-up.
        emit("backend_loaded", seconds=round(time.monotonic() - started, 3), backend=type(adapter._backend).__name__)
        owner, values, _ = plugin_fixture(run_root / "unused-memory-store")
        owner._catalog, owner._vision = catalog, adapter
        values["observer_settings"] = {"enabled": True, "proactive_enabled": False, "interval_seconds": 2}
        # Read-only profile copy exercises source separation without changing
        # actual player records. Only selected role data is copied in memory.
        from .trace_current_panel_boundary import pearl_archive
        values.update(pearl_archive())
        configure_loop(owner, client)
        owner._data_pack_runtime = catalog.status()
        owner._assistant_process = SimpleNamespace(snapshot=lambda: {}, start_if_due=lambda: False)
        owner._target_character = lambda: ""
        owner._D_abort_marker = run_root / "abort"
        owner._observer_task = asyncio.create_task(owner._observer_loop())

        async def sample_load():
            import psutil
            own, helper = psutil.Process(), psutil.Process(process.pid)
            own.cpu_percent()
            helper.cpu_percent()
            while True:
                await asyncio.sleep(30)
                row = {"at": time.time(), "source_cpu_percent": own.cpu_percent(),
                    "helper_cpu_percent": helper.cpu_percent(), "source_rss": own.memory_info().rss,
                    "helper_rss": helper.memory_info().rss, "serial": getattr(owner, "_observation_serial", 0),
                    "observer_state": copy.deepcopy(owner._observer_status)}
                result["samples"].append(row)
                emit("resident_sample", serial=row["serial"], state=row["observer_state"].get("state"),
                     source_rss=row["source_rss"], source_cpu_percent=row["source_cpu_percent"])

        sampler = asyncio.create_task(sample_load())
        await campaign(owner, args, result, emit, save)
    except Exception as exc:
        result["failure"] = {"type": type(exc).__name__, "error": str(exc)[:200]}
        emit("failure", **result["failure"])
    finally:
        if sampler:
            sampler.cancel()
            await asyncio.gather(sampler, return_exceptions=True)
        await cleanup(owner, adapter, process, enabled=enabled, pause_attempted=pause_attempted, result=result)
        result["source_unchanged"] = all(sha(ROOT / n) == h for n, h in expected.items())
        result["installed_unchanged"] = tree(INSTALLED) == installed_before
        result["helper_unchanged"] = sha(executable) == freeze["assistant_sha256"] and sha(POINTER) == freeze["assistant_pointer_sha256"]
        emit("finished", restored=result["restored"], source_unchanged=result["source_unchanged"],
             installed_unchanged=result["installed_unchanged"], helper_unchanged=result["helper_unchanged"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--port", type=int, default=8513)
    parser.add_argument("--transitions-only", action="store_true",
                        help="Separate acknowledged-transition run; does not repeat or certify the resident period")
    with patch.object(socket.socket, "connect", loopback_only(socket.socket.connect)):
        asyncio.run(run(parser.parse_args()))
