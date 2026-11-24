"""One current character packet, admitted by the existing snapshot reviewer."""
from __future__ import annotations

import asyncio
import copy
import time
from datetime import datetime, timezone

from ..adapters.text_verification import identity_literal
from .contracts import native_name_confirmed
from .delivery_scope import coordinates
from .fact_lifecycle import candidate_evidence
from .live_refresh import _supports_kwarg, delivery_turn, fetch_recent_frame, frame_stamp, trace_refresh
from .observation_store import ObservationSnapshot
from .query_reuse import _SUPERSEDED, _remember_candidate, region_stamp, regions_changed, review_snapshot
from .rejection_trace import emit
from .review_policy import checked_receipt
from .timing_detail import phase, timed
from .vision_scheduler import run_query_worker
from .vision_trace import bind_trace, record_candidate


def _live(owner, generation, epoch, failure):
    return (not getattr(owner, "_observer_stop_requested", False)
        and getattr(owner, "_observer_status", {}).get("enabled")
        and generation == getattr(owner, "_answer_query_generation", 0)
        and epoch == getattr(owner, "_observation_epoch", 0)
        and failure == getattr(owner, "_capture_failure_revision", 0))


def _page_guard(owner, packet, snapshot):
    """Only an admitted page-only preview gets a page-only native guard."""
    if getattr(owner, "_accepted_frame_page_regions", None) or not getattr(owner, "_scene_preview", None):
        return
    admitted = {tuple(p) for p in packet["reuse"].get("page_control_coordinates", [])}
    pairs = [(p, patch) for p, patch in zip(*snapshot["guard"]) if coordinates(p) in admitted]
    if not pairs or {patch[0] for _, patch in pairs} != admitted:
        return
    controls = [p for p, _ in pairs]
    owner._accepted_frame_stamp = snapshot["stamp"]
    owner._accepted_frame_page_regions = controls
    owner._accepted_frame_scene_regions = controls
    owner._accepted_frame_page_type = packet["page_type"]
    owner._accepted_frame_delivery_guard = (controls, tuple(p for _, p in pairs))
    trace_refresh(owner, "region_page_guard_admitted", region_count=len(controls))


def _layout_regions(owner):
    """Only acquisition coordinates, never cached text, identities or values."""
    raw = getattr(owner, "_committed_observation", None) or {}
    if (raw.get("page") or {}).get("page_type") != "character_detail":
        return None
    recognition = raw.get("recognition") or {}
    def geometry(values):
        return [{"region": dict(a["region"])} for a in values if isinstance(a,dict) and isinstance(a.get("region"),dict)]
    return {"source_size": recognition.get("source_size"),
            "page_controls": geometry(recognition.get("scene_regions") or []),
            "name_regions": geometry(a for a in raw.get("characters") or [] if a.get("identity_scope") == "detail_subject")}


def supports_current_character(owner, plan):
    fields = getattr(plan, "fields", frozenset())
    return bool(fields <= {"page", "characters", "attributes"} and fields & {"characters", "attributes"}
                and callable(getattr(getattr(owner, "_vision", None), "read_character_detail", None)))


@timed("region_packet")
async def read_current_character(owner, plan, deadline):
    if not supports_current_character(owner, plan):
        emit(owner, "Q01", "compact", "not_applicable", "unsupported")
        return None
    reader = owner._vision.read_character_detail
    generation = getattr(owner, "_answer_query_generation", 0)
    epoch = getattr(owner, "_observation_epoch", 0)
    failure = getattr(owner, "_capture_failure_revision", 0)
    # A real consumer claim prevents the next producer from racing this short
    # read and its review. It does not cancel an unreturned native call.
    async with delivery_turn(owner):
        source = None

        async def prepare():
            nonlocal source
            with phase("region_capture", owner):
                source = await fetch_recent_frame(owner, wait_seconds=max(.001, min(2., deadline-time.monotonic())))
            if source is None:
                emit(owner, "Q02", "compact", "reject", "no_source")
                return None
            if not _live(owner, generation, epoch, failure):
                emit(owner, "Q02", "compact", "reject", "not_live")
                return None
            bind_trace(frame_seq=getattr(source, "frame_seq", None))
            acquisition = _layout_regions(owner)
            options = {"layout_regions": acquisition} if acquisition and _supports_kwarg(reader, "layout_regions") else {}
            return (source.image_bytes,), {"fields": plan.fields, **options}

        analysis = await run_query_worker(owner, reader, deadline=deadline, prepare=prepare)
        if not isinstance(analysis, dict):
            emit(owner, "Q02", "compact", "reject", "no_analysis")
            return None
        if analysis.get("page", {}).get("page_type") != "character_detail":
            emit(owner, "Q02", "compact", "reject", "wrong_page")
            return None
        if not analysis.get("scene", {}).get("verified"):
            emit(owner, "Q02", "compact", "reject", "scene_unverified")
            return None
        if not _live(owner, generation, epoch, failure):
            emit(owner, "Q02", "compact", "reject", "not_live")
            return None
        if time.monotonic() >= deadline:
            emit(owner, "Q02", "compact", "reject", "deadline")
            return None
        controls = (analysis.get("recognition") or {}).get("scene_regions") or []
        if not controls:
            emit(owner, "Q10", "compact", "reject", "no_controls")
            return None
        actors = [a for a in analysis.get("characters") or [] if native_name_confirmed(a)]
        raw = {**analysis, "source": "assistant_current_regions", "characters": actors,
               "observed_at": datetime.fromtimestamp(source.timestamp, timezone.utc).isoformat(),
               "runtime": {"stable_scene": analysis["scene"]["primary_state"],
                           "stable_character_ids": [a["entity_id"] for a in actors]}}
        # Check current controls before publishing even the PAGE. No native
        # facts are copied from the newer capture; its pixels only corroborate.
        latest = await fetch_recent_frame(owner, wait_seconds=max(.001, min(2., deadline-time.monotonic())))
        if latest is None:
            emit(owner, "Q10", "compact", "reject", "no_frame")
            return None
        if latest.game.get("pid") != source.game.get("pid"):
            emit(owner, "Q10", "compact", "reject", "window_changed")
            return None
        if not _live(owner, generation, epoch, failure):
            emit(owner, "Q10", "compact", "reject", "not_live")
            return None
        def controls_changed():
            # Exact current/source bytes prove equality more strongly than a
            # second decode of both control crops. Fresh capture, PID and all
            # lifecycle guards above still apply; final review stays shared.
            if source.image_bytes == latest.image_bytes:
                return False
            return regions_changed(region_stamp(source.image_bytes, controls), region_stamp(latest.image_bytes, controls))
        with phase("region_controls", owner):
            changed = await asyncio.to_thread(controls_changed)
        if changed:
            emit(owner, "Q11", "compact", "reject", "controls_changed")
            return None
        publish = getattr(owner, "_publish_scene_preview", None)
        if callable(publish):
            await publish({**analysis, "characters": [], "screen_facts": {"facts": [], "visible_text": []},
                "observed_at": raw["observed_at"],
                "recognition": {**analysis["recognition"], "phase": "scene_only"}})
        next_epoch = getattr(owner, "_observation_epoch", 0)
        if next_epoch not in {epoch, epoch+1}:
            emit(owner, "Q12", "compact", "reject", "epoch_changed")
            return None
        if not _live(owner, generation, next_epoch, failure):
            emit(owner, "Q12", "compact", "reject", "not_live")
            return None
        epoch = next_epoch  # The above current-page publication may revoke the old page.
        serial = getattr(owner, "_observation_serial", 0)
        raw["observation_serial"] = serial
        raw["screen_facts"] = {**(raw.get("screen_facts") or {}), "observation_serial": serial}
        evidence = candidate_evidence(raw)
        proofs = [*controls, *[p for f in evidence["screen_facts"]["facts"] for p in f.get("evidence", [])
                              if p.get("validation_scope") != "control_presence"],
                  *[(identity_literal(a) or a) for a in actors]]
        proofs = list({coordinates(p): p for p in proofs if p.get("region")}.values())
        def source_guards():
            return region_stamp(source.image_bytes, proofs), frame_stamp(source.image_bytes)
        with phase("region_guards", owner):
            patches, stamp = await asyncio.to_thread(source_guards)
        snapshot = {"evidence": evidence, "guard": (proofs, patches),
            "stamp": stamp, "epoch": epoch, "game_pid": source.game.get("pid"),
            "page_controls": controls, "identity_proofs": actors, "detached_region": True}
        record_candidate(owner, "candidate_parsed", raw)
        # A normal background commit can advance serial without changing any
        # fact. Re-select this same packet once, never merge/retimestamp it.
        result = None
        for _ in range(2):
            if time.monotonic() >= deadline:
                emit(owner, "Q13", "compact", "reject", "deadline")
                return None
            if not _live(owner, generation, epoch, failure):
                emit(owner, "Q13", "compact", "reject", "not_live")
                return None
            snapshot["evidence"]["screen_facts"]["observation_serial"] = getattr(owner, "_observation_serial", 0)
            with phase("region_snapshot_review", owner):
                result = await review_snapshot(owner, snapshot, plan=plan, full=True)
            if result is not _SUPERSEDED:
                break
            trace_refresh(owner, "region_review_reselected")
        with phase("region_receipt", owner):
            if result is None:
                emit(owner, "Q14", "compact", "reject", "no_result")
                return None
            if result is _SUPERSEDED:
                emit(owner, "Q14", "compact", "reject", "superseded")
                return None
            if not _live(owner, generation, epoch, failure):
                emit(owner, "Q14", "compact", "reject", "not_live")
                return None
            if not checked_receipt(owner, result, plan=plan):
                emit(owner, "Q14", "compact", "reject", "receipt_rejected")
                return None
            if not ObservationSnapshot.select(owner, result, fields=plan.fields).matches(owner):
                emit(owner, "Q14", "compact", "reject", "snapshot_rejected")
                return None
            emit(owner, "Q14", "compact", "allow", "admitted")
        record_candidate(owner, "candidate_verified", result)
        _page_guard(owner, result, snapshot)
        snapshots = getattr(owner, "_reusable_observations", {})
        if any(s.get("evidence", {}).get("page_type") != result["page_type"] for s in snapshots.values()):
            snapshots = {}
        _remember_candidate(snapshots, "full", copy.deepcopy(snapshot))
        owner._reusable_observations = snapshots
        trace_refresh(owner, "current_region_admitted", fields=sorted(plan.fields),
                      age_seconds=result.get("age_seconds"))
        with phase("region_exit", owner):
            return result
