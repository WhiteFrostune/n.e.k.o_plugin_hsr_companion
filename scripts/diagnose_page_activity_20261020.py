"""Read-only diagnosis. No installation, host writes, game input or chat send.

Replay uses synthetic in-memory frames, not the historical game screenshot.
Optional IPC probes read only user-context metadata through the SDK facade.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[2]))


def replay_page_gate():
    from plugin.plugins.hsr_companion.runtime.delivery_scope import scoped_guard
    from plugin.plugins.hsr_companion.runtime.query_plan import build_query_plan
    from plugin.plugins.hsr_companion.runtime.query_reuse import region_stamp
    from plugin.plugins.hsr_companion.tests.test_query_reuse import picture

    region = {"left": .05, "top": .02, "right": .5, "bottom": .08}
    control = {"text": "页面控件", "control": True, "region": region}
    payload = picture()
    guard = ([control], region_stamp(payload, [control]))
    plan = build_query_plan("这个页面是干嘛的？页面上写了什么？", "explain_page")
    evidence = {"fresh": True, "page_type": "exploration_hud",
        "scene": {"verified": True, "primary_state": "exploration"},
        "screen_facts": {"facts": [], "visible_text": [{"text": "页面控件", "region": region}]}}
    cases = []
    for label, controls in (("matching_control", [control]), ("missing_control", [])):
        owner = SimpleNamespace(_accepted_frame_page_regions=[control],
            _active_refresh_trace={"started": time.monotonic(), "stages": []})
        selected = scoped_guard(owner, copy.deepcopy(evidence), plan, [guard],
            proof_source={"page_controls": controls})
        cases.append({"case": label, "page_text_same": True,
            "snapshot_controls": len(controls), "owner_controls": 1,
            "accepted": selected is not None,
            "trace": owner._active_refresh_trace["stages"]})
    assert cases[0]["accepted"] and not cases[1]["accepted"]
    return {"historical_reproduction": False,
            "scope": "Exact rejection gate only; upstream loss not established", "cases": cases}


def replay_social_page_parser():
    from plugin.plugins.hsr_companion.detectors.layout import scene_control_regions
    from plugin.plugins.hsr_companion.tests.test_experience_fixes import box
    from plugin.plugins.hsr_companion.tests.test_exploration_fields import classify, hud

    base = hud(place="观景车厢", title="黑潮时期的爱情", objective="询问哈托努斯关于宁芙的下落")
    base.pop(1)  # Social interior has no combat/team status HUD.
    base.append(box("Enter", 45, 720))
    result = []
    for label in ("complete", "chat_confidence_0.89", "location_confidence_0.89"):
        boxes = copy.deepcopy(base)
        if label == "chat_confidence_0.89":
            boxes[-1]["score"] = .89
        if label == "location_confidence_0.89":
            boxes[0]["score"] = .89
        page = classify(boxes)
        controls = scene_control_regions(page, boxes, width=1280, height=800)
        result.append({"case": label, "all_recognized_strings_unchanged": True,
            "page_type": page["page_type"], "control_count": len(controls)})
    assert result[0]["page_type"] == "exploration_hud"
    assert all(c["page_type"] == "unknown" and c["control_count"] == 0 for c in result[1:])
    return {"historical_reproduction": False,
        "scope": "Controlled OCR-box inputs: identifies coupling, not the original frame's actual scores",
        "cases": result}


async def replay_activity_blocks_ready_data():
    from plugin.plugins.hsr_companion.runtime import followup

    reads, sends = [], []

    async def unavailable_get(**kwargs):
        raise TimeoutError("controlled IPC failure")

    async def current():
        reads.append(True)
        return {"fresh": True, "page_type": "exploration_hud", "scene": {"verified": True},
                "screen_facts": {"visible_text": [{"text": "已确认页面文字"}], "facts": []}}

    owner = SimpleNamespace(_observer_status={"enabled": True, "state": "observing"},
        _target_character=lambda: "isolated-diagnostic", _current_visual_evidence=current,
        bus=SimpleNamespace(memory=SimpleNamespace(get=unavailable_get)),
        push_message=lambda **kwargs: sends.append(kwargs), _pending_answer=None)
    old_grace = followup.FOLLOWUP_GRACE_SECONDS
    followup.FOLLOWUP_GRACE_SECONDS = 0  # Shorten only isolated scheduling, not production budgets.
    now = time.monotonic()
    req = followup.AnswerRequest("isolated", "页面文字写了什么", "isolated-diagnostic", time.time(),
        now, now + .1, "unknown", False, False, True, False)
    owner._pending_answer = req
    try:
        await followup.deliver_followup(owner, req)
    finally:
        followup.FOLLOWUP_GRACE_SECONDS = old_grace
    assert not reads and not sends
    assert owner._followup_status["reason"] == "activity_unavailable_after_retries"
    return {"controlled_transport": True, "ready_data_reader_calls": len(reads),
            "local_submission_calls": len(sends), "status": owner._followup_status,
            "finding": "Activity failure blocks before any current-result read, regardless of data readiness"}


async def probe_ipc(endpoint):
    from plugin.core.bus.memory import MemoryClient
    from plugin.sdk.plugin import NekoPluginBase
    from plugin.utils.zeromq_ipc import ZmqIpcClient

    # Independent socket identity; never attaches to or drains the live plugin.
    transport = ZmqIpcClient("hsr_readonly_diagnostic", endpoint)
    ctx = SimpleNamespace(plugin_id="hsr_readonly_diagnostic",
        _plugin_comm_queue=object(), _zmq_ipc_client=transport)
    sdk = NekoPluginBase(SimpleNamespace(plugin_id="hsr_readonly_diagnostic", metadata={},
        bus=SimpleNamespace(memory=MemoryClient(ctx))))
    results = []
    try:
        for budget in (.25, 1., 2.):
            started = time.monotonic()
            try:
                records = await sdk.bus.memory.get(bucket_id="林悠怡", limit=8, timeout=budget)
                records = list(records)
                result = {"returned": True, "record_count": len(records)}
            except Exception as exc:
                # No chat contents, raw socket payloads or credentials.
                result = {"returned": False, "error_type": type(exc).__name__,
                          "error": str(exc)[:180]}
            results.append({"transport_budget_seconds": budget,
                "elapsed_seconds": round(time.monotonic() - started, 4), **result})
    finally:
        transport.close()
    return {"source": "independent current-time SDK/IPC read, not the original request",
            "endpoint": endpoint, "results": results}


def frozen_hashes():
    installed = Path("C:/Users/cai/AppData/Local/N.E.K.O/.neko-plugin-installations/plugins/hsr_companion")
    names = ["runtime/delivery_scope.py", "runtime/query_reuse.py", "runtime/live_refresh.py",
             "runtime/chat_activity.py", "runtime/followup.py", "adapters/local_vision.py",
             "detectors/layout.py", "runtime/query_plan.py"]
    result = []
    for name in names:
        source = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        active = hashlib.sha256((installed / name).read_bytes()).hexdigest()
        result.append({"file": name, "sha256": source, "installed_matches_source": source == active})
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", help="Optional existing local read-only IPC endpoint")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = {"checked_at": datetime.now(timezone.utc).isoformat(),
              "production_modified": False, "game_input": 0, "host_messages_sent": 0,
              "hashes_before": frozen_hashes(), "page_gate_replay": replay_page_gate(),
              "social_parser_replay": replay_social_page_parser(),
              "followup_activity_replay": asyncio.run(replay_activity_blocks_ready_data())}
    if args.endpoint:
        if not args.endpoint.startswith("tcp://127.0.0.1:"):
            raise ValueError("Only the already running local IPC endpoint is supported")
        result["ipc_probe"] = asyncio.run(probe_ipc(args.endpoint))
    result["hashes_after"] = frozen_hashes()
    assert result["hashes_before"] == result["hashes_after"]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
