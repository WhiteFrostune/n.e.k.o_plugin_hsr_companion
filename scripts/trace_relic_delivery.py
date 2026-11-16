"""One original relic request, isolated source tracing; never install or speak.

Uses the stability runner's frozen source/helper checks and restoration. The
page is already open: this is not a replay of the historical transition timing.
No saved images or real host messages. Diagnostic wrappers call originals once.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import inspect
import json
import socket
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from . import verify_code_health_stability as runner
from .field_pipeline_probe import FieldPipelineProbe, coords, summary
from .verify_roundtrip_c_acceptance import loopback_only


def rejection_context(caller):
    """Only dependency geometry/counts, not caller locals, pixels or chat."""
    values = caller.f_locals
    selected = values.get("selected") or []
    pairs = values.get("pairs") or {}
    from ..runtime.delivery_scope import coordinates
    missing = [coordinates(p) for p in selected if isinstance(p, dict) and coordinates(p) not in pairs]
    return {"function": caller.f_code.co_name, "selected_count": len(selected),
            "guard_count": len(pairs), "missing_coordinates": coords(missing)}


async def campaign(owner, args, result, emit, save):
    from plugin.sdk.plugin import NekoPluginBase

    from ..runtime import delivery_scope, followup

    original = json.loads(Path(args.original_run).read_bytes())
    rows = [r for r in original["queries"] if r["phase"] == "relic"]
    if len(rows) != 1:
        raise ValueError("Expected exactly one original relic request")
    kwargs = copy.deepcopy(rows[0]["actual_parameters"])
    result["scope"] = "one_original_request_on_already_open_relic_not_original_transition_timing"
    result["actual_parameters"] = kwargs
    events, receipts, submissions = [], [], []
    owner.ctx = SimpleNamespace(bus=NekoPluginBase(SimpleNamespace(plugin_id="isolated-relic-delivery",
        metadata={}, bus=SimpleNamespace(memory=SimpleNamespace(get=lambda **kw: list(events))))).bus)
    owner._target_character = lambda: "hsr_isolated_test"
    owner.push_message = lambda **kw: submissions.append(copy.deepcopy(kw)) or {"submitted": True}
    original_record, original_rejected = followup.record_answer_delivery, delivery_scope.proof_rejected

    def record(target, **kw):
        value = original_record(target, **kw)
        if target is owner:
            receipts.append({"channel": kw["channel"], "payload": runner.packet_view(kw["payload"]),
                "projection": copy.deepcopy(kw.get("message_projection")), "submitted": kw.get("submitted")})
        return value

    with FieldPipelineProbe(owner, limit=1800) as probe:
        probe.phase = "relic"

        def rejected(target, reason):
            caller = inspect.currentframe().f_back
            try:
                if target is owner:
                    probe.emit("proof_rejection_dependencies", reason=reason, **rejection_context(caller))
            finally:
                del caller
            return original_rejected(target, reason)

        async def persist():
            while True:
                result["pipeline"] = copy.deepcopy(probe.events)
                result["followup_status"] = copy.deepcopy(getattr(owner, "_followup_status", {}))
                save()
                await asyncio.sleep(1)

        writer = asyncio.create_task(persist())
        try:
            with patch.object(followup, "record_answer_delivery", record), \
                    patch.object(delivery_scope, "proof_rejected", rejected):
                start = time.monotonic()
                result["asked_at"] = time.time()
                packet = await owner.hsr_companion_prepare(**kwargs)
                result["direct_seconds"] = round(time.monotonic() - start, 3)
                result["packet"] = runner.packet_view(packet)
                emit("direct", seconds=result["direct_seconds"], answer=packet.get("query_answer"))
                pending = getattr(owner, "_pending_answer", None)
                if pending is not None:
                    deadline = pending.expires_at
                    events.append({"type": "user_message", "lanlan": "hsr_isolated_test",
                                   "content": "好", "_ts": time.time()})
                    confirmation = await owner.hsr_companion_prepare(query="好", pending_reply={
                        "request_id": pending.request_id, "relation": "confirm", "player_message": "好"})
                    result["local_confirmation"] = {"accepted": confirmation["pending_reply"]["accepted"],
                        "deadline_unchanged": deadline == pending.expires_at, "is_real_cat_event": False}
                    task = owner._followup_task
                    while not task.done():
                        if owner._D_abort_marker.exists():
                            raise RuntimeError("Operator aborted; failure retained")
                        await asyncio.wait({task}, timeout=.5)
                    await task
                result["total_seconds"] = round(time.monotonic() - start, 3)
                result["followup_status"] = copy.deepcopy(getattr(owner, "_followup_status", {}))
                result["receipts"], result["notifications"] = receipts, submissions
                result["last_candidate"] = summary(getattr(owner, "_committed_observation", None))
                emit("request_finished", seconds=result["total_seconds"], status=result["followup_status"])
        finally:
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)
            result["pipeline"] = copy.deepcopy(probe.events)
            result["trace_dropped"], result["trace_errors"] = probe.dropped, probe.errors
            save()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--original-run", default=str(runner.ROOT / "dist/code-health-stability-20261101-2/live.json"))
    parser.add_argument("--port", type=int, default=8513)
    args = parser.parse_args()
    args.transitions_only = True
    with patch.object(socket.socket, "connect", loopback_only(socket.socket.connect)), \
            patch.object(runner, "campaign", campaign):
        asyncio.run(runner.run(args))
