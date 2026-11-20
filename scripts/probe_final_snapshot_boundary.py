"""Diagnostic replay of the real formal entry, never a live plugin mutation.

OCR/frames are explicit synthetic fixtures. A read-only Python trace captures
the real matches() return locals; it never changes that function's result.
Historical acceptance proves where data vanished, not which race occurred.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import sys
import tempfile
from pathlib import Path


async def run_case(directory, name):
    from plugin.plugins.hsr_companion.runtime.observation import public_visual_evidence
    from plugin.plugins.hsr_companion.runtime.observation_store import (
        ObservationSnapshot,
        ObservationStore,
        field_projection,
    )
    from plugin.plugins.hsr_companion.tests.test_chain_audit_20261017 import (
        audit_owner,
        commit_raw,
        navigation_raw,
        raw_page,
    )
    from plugin.plugins.hsr_companion.tests.test_delivery_fix_20261012 import CONTROL

    owner, values, sent = audit_owner(directory)
    mutation = name.removeprefix("page_only_")
    equipment = name == "real_equipment_change"
    raw = raw_page() if equipment else navigation_raw()
    if mutation == "quest_added":
        raw["screen_facts"]["facts"] = [f for f in raw["screen_facts"]["facts"] if f["scope"] != "quest"]
    await commit_raw(owner, raw)
    initial = copy.deepcopy(owner._committed_observation)
    checks = []
    phase = "before_audit"
    original_match = ObservationSnapshot.matches
    original_audit = owner._record_tool_use

    def observed_match(snapshot, current_owner):
        if current_owner is not owner:
            return original_match(snapshot, current_owner)
        captured = {}
        previous_trace = sys.gettrace()

        def trace(frame, event, arg):
            if frame.f_code is original_match.__code__ and event == "return":
                local = frame.f_locals
                committed = local.get("committed") or {}
                current = public_visual_evidence(committed, owner._observer_status)
                selected = snapshot.evidence()
                reuse = selected.get("reuse") or {}
                captured.update(
                    phase=phase, returned=bool(arg), epoch_matches=snapshot.epoch == getattr(owner, "_observation_epoch", 0),
                    snapshot_serial=snapshot.serial, owner_serial=getattr(owner, "_observation_serial", 0),
                    serial_admitted=local.get("serial_matches"), pending_admitted=local.get("pending_allowed"),
                    selected_page=selected.get("page_type"), committed_page=current.get("page_type"),
                    selected_fresh=selected.get("fresh"), committed_fresh=current.get("fresh"),
                    generation_matches=reuse.get("answer_generation", 0) == getattr(owner, "_answer_query_generation", 0),
                    capture_revision_matches=reuse.get("capture_failure_revision", 0) == getattr(owner, "_capture_failure_revision", 0),
                    invalidated=getattr(owner, "_observation_invalidated", False),
                    stop_requested=getattr(owner, "_observer_stop_requested", False),
                    pending_reason=getattr(owner, "_observation_pending_reason", ""),
                    scene_equal=selected.get("scene") == current.get("scene"),
                    scope_differences=[field for field in sorted(snapshot._fields)
                        if field_projection(selected, {field}) != field_projection(current, {field})],
                )
            return trace

        try:
            sys.settrace(trace)
            result = original_match(snapshot, current_owner)
        finally:
            sys.settrace(previous_trace)
        checks.append(captured)
        return result

    async def audited(*args, **kwargs):
        nonlocal phase
        await original_audit(*args, **kwargs)
        phase = "after_audit"
        newer = copy.deepcopy(raw)
        if mutation == "identical_reread":
            await commit_raw(owner, newer)
        elif mutation == "scene_metadata_only":
            newer["scene"]["evidence"] = ["same synthetic HUD; independent reread provenance"]
            await commit_raw(owner, newer)
        elif mutation == "visible_box_jitter":
            newer["screen_facts"]["visible_text"][0]["region"]["left"] += 1 / 640
            await commit_raw(owner, newer)
        elif mutation == "quest_added":
            await commit_raw(owner, navigation_raw())
        elif name == "requested_text_challenged":
            from plugin.plugins.hsr_companion.runtime.delivery_scope import coordinates
            text_region = (owner._committed_observation["screen_facts"]["visible_text"])[0]
            # The visible row is also the fixture control region: use a quest
            # row instead, so page-control proof remains untouched.
            proof = owner._committed_observation["screen_facts"]["facts"][-1]["evidence"][0]
            assert coordinates(proof) != coordinates(text_region)
            ObservationStore(owner).defer_regions({coordinates(proof)})
        elif name == "control_challenged":
            from plugin.plugins.hsr_companion.runtime.delivery_scope import coordinates
            ObservationStore(owner).defer_regions({coordinates(CONTROL)})
        elif name == "capture_failed":
            ObservationStore(owner).suspend("capture_unavailable_probe")
        elif name == "new_question":
            owner._answer_query_generation += 1
        elif name == "paused":
            owner._observer_status.update(enabled=False, state="paused")
            owner._observer_stop_requested = True
        elif name == "new_page":
            await commit_raw(owner, raw_page("team_setup"))
        elif name == "real_equipment_change":
            newer["screen_facts"]["facts"][1]["value"] = "32.4%"
            await commit_raw(owner, newer)

    owner._record_tool_use = audited
    ObservationSnapshot.matches = observed_match
    try:
        result = await owner.hsr_companion_prepare(query="我打开的是啥页面？" if not equipment else "完整报出遗器词条",
            intent="read_screen", screen_fields=["equipment"] if equipment else ["page"] if name.startswith("page_only_")
                else ["page", "quest", "visible_text"])
    finally:
        ObservationSnapshot.matches = original_match
    audit = values["tool_audit"][-1]
    receipt = result["delivery_receipt"]
    row = {"case": name, "source": "real formal entry with synthetic frames and explicit audit-boundary mutation",
        "planned_fields": result["query_plan"]["fields"],
        "audit_state": audit["answer_status_at_audit"]["state"],
        "audit_confirmed": audit["answer_status_at_audit"]["confirmed_fields"],
        "refresh_seconds": result["query_timing"]["refresh_wait_seconds"],
        "final_state": result["query_answer"]["state"], "final_confirmed": result["query_answer"]["confirmed_fields"],
        "selection_serial": receipt["selection_serial"], "payload_serial": receipt["payload_serial"],
        "page_delivered": result["screen_scene_answer"]["allowed"],
        "fixture_messages": len(sent), "real_host_messages": 0, "matches_trace": checks,
        "same_page_type": initial["page"]["page_type"] == (owner._committed_observation or {})["page"]["page_type"]}
    print(json.dumps({k: row[k] for k in ("case", "audit_state", "final_state", "final_confirmed")}), flush=True)
    return row


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    if not output.is_relative_to(root / "dist") or output.exists():
        raise ValueError("Use a new plugin dist report")
    installed = Path("C:/Users/cai/AppData/Local/N.E.K.O/.neko-plugin-installations/plugins/hsr_companion")
    files = ("__init__.py", "runtime/observation_store.py", "runtime/query_reuse.py", "runtime/query_coordinator.py",
             "runtime/review_policy.py", "runtime/live_refresh.py")
    def hashes(path):
        return {name: hashlib.sha256((path / name).read_bytes()).hexdigest() for name in files}
    source_before, active_before = hashes(root), hashes(installed)
    assert source_before == active_before, "diagnosis must use the installed implementation"
    names = ["baseline", "identical_reread", "scene_metadata_only", "visible_box_jitter", "quest_added",
             "requested_text_challenged", "control_challenged", "capture_failed", "new_question", "paused",
             "new_page", "real_equipment_change", "page_only_visible_box_jitter", "page_only_quest_added"]
    with tempfile.TemporaryDirectory(prefix="hsr_snapshot_probe_") as directory:
        async def run():
            return [await run_case(Path(directory) / name, name) for name in names]
        rows = asyncio.run(run())
    report = {"implementation_matches_installed": True, "historical_frame_replay": False,
        "source_modified": hashes(root) != source_before, "installed_modified": hashes(installed) != active_before,
        "images_stored": False, "cases": rows,
        "scope": "Causal condition replay, not proof of which mutation happened in the historical live request"}
    assert not report["source_modified"] and not report["installed_modified"]
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    assert not any(row["real_host_messages"] for row in rows)
    print(str(output), flush=True)


if __name__ == "__main__":
    main()
