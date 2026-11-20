"""Select bounded candidates, then use the shared live admission policy.

Every scope needs native dependency guards. Pixels challenge text, not the
existence of a candidate; unavailable proof falls back to the single producer.
"""

from __future__ import annotations

import copy
import math
import time
from collections import Counter
from datetime import datetime
from io import BytesIO

from .contracts import FRESH_LIVE_AGE_SECONDS, LIVE_PAGE_TYPES, STALE_MAX_AGE_SECONDS
from .navigation import navigation_answer

# Recognition age does not decide live-candidate admission. Fresh capture and
# scoped literal/control proof do; persisted history never enters this cache.
LIVE_FRAME_MAX_AGE_SECONDS = 2.0
_SUPERSEDED = object()


def stale_state(page_type, age, *, allow_stale=False):
    """One age policy for cached readings.

    Static pages may reuse a guarded older reading up to the hard cap. Live
    pages only use an older reading when the caller is the timeout fallback.
    """
    if age < 0:
        return "invalid"
    if age > STALE_MAX_AGE_SECONDS:
        return "expired"
    if age > FRESH_LIVE_AGE_SECONDS:
        if page_type in LIVE_PAGE_TYPES and not allow_stale:
            return "not_allowed"
        return "stale"
    return "fresh"


def prefer_richer_roster(candidates, plan, *, allow_stale):
    """Stale fallback prefers a richer same-page roster, never merges rows."""
    if not (allow_stale and plan is not None and "characters" in plan.fields):
        return list(candidates)
    def roster_size(candidate):
        return len({item.get("entity_id") or item.get("name")
                    for item in (candidate["evidence"].get("characters") or [])})
    return sorted([candidate for candidate in candidates if candidate], key=roster_size, reverse=True)


def content_coverage(evidence):
    """Coverage only, not correctness or permission to use an old value."""
    screen = evidence.get("screen_facts") or {}
    # Compare shape/row coverage, not yesterday's literal labels or character
    # IDs. A complete different item/role must replace the display archive.
    facts = Counter((f.get("scope"), f.get("kind")) for f in screen.get("facts") or [])
    actors = Counter(a.get("identity_scope") for a in evidence.get("characters") or [])
    coverage = {("fact", scope, kind, i) for (scope, kind), n in facts.items() for i in range(n)}
    coverage.update(("actor", scope, i) for scope, n in actors.items() for i in range(n))
    coverage.update(("text", i) for i in range(len(screen.get("visible_text") or [])))
    return frozenset(coverage)


def complete_fact_guard(analysis: dict) -> bool:
    """Every exposed value needs literal evidence, not just a page control."""
    for fact in (analysis.get("screen_facts") or {}).get("facts") or []:
        proofs = [p for p in fact.get("evidence") or [] if p.get("validation_scope") != "control_presence"]
        if not proofs or not all(p.get("region") for p in proofs):
            return False
    return True


def region_stamp(payload: bytes, regions: list) -> tuple:
    from PIL import Image

    patches = []
    with Image.open(BytesIO(payload)) as image:
        for item in regions[:48]:
            region = item.get("region") or {}
            coords = [float(region.get(key, -1)) for key in ("left", "top", "right", "bottom")]
            if not all(math.isfinite(v) and 0 <= v <= 1 for v in coords):
                continue
            left, top, right, bottom = coords
            if right <= left or bottom <= top:
                continue
            bounds = (int(left * image.width), int(top * image.height),
                      math.ceil(right * image.width), math.ceil(bottom * image.height))
            patch = image.crop(bounds).convert("L")
            patch.thumbnail((512, 64))
            patches.append((tuple(coords), patch.size, patch.tobytes()))
    return tuple(patches)


def text_pixel_differences(before: bytes, after: bytes) -> list[int]:
    """Ignore a bounded dominant background-light shift, not changed glyphs.

    Text-box backgrounds are translucent over the 3D world. Preserve both
    unchanged pixels and pixels following the common light shift. A removed
    or added glyph still differs from both. Complex motion remains a miss.
    """
    signed = [b - a for a, b in zip(before, after)]
    if not signed:
        return []
    counts = Counter(signed)
    shift, _ = counts.most_common(1)[0]
    if 8 <= abs(shift) <= 64 and sum(n for delta, n in counts.items() if abs(delta - shift) <= 3) >= len(signed) * .65:
        return [min(abs(delta), abs(delta - shift)) for delta in signed]
    return [abs(delta) for delta in signed]


def regions_changed(before: tuple, after: tuple) -> bool:
    if not before or len(before) != len(after):
        return True
    for (region_a, size_a, pixels_a), (region_b, size_b, pixels_b) in zip(before, after):
        if region_a != region_b or size_a != size_b:
            return True
        # Exact nonempty native bytes are stronger than the heuristic below.
        # Geometry was checked first; empty/failed crops are still not proof.
        if pixels_a and pixels_a == pixels_b:
            continue
        differences = text_pixel_differences(pixels_a, pixels_b)
        if not differences or sum(differences) / len(differences) / 255 > .015:
            return True
        if sum(d > 24 for d in differences) / len(differences) > .02:
            return True
        # A changed glyph can occupy far less than 2% of a wide OCR box.
        # Check small cells too; background flicker may cause a safe cache miss.
        width, height = size_a
        for top in range(0, height, 8):
            for left in range(0, width, 8):
                cell = [differences[y * width + x]
                        for y in range(top, min(height, top + 8))
                        for x in range(left, min(width, left + 8))]
                # A handful of low-amplitude antialias/background pixels is
                # not a changed glyph. Strong tiny changes still revoke it;
                # weaker changes need a larger local cluster.
                if sum(d > 64 for d in cell) >= 3 or sum(d > 24 for d in cell) >= 8:
                    return True
    return False


def _remember_candidate(snapshots, key, candidate):
    """One current and at most one richer packet, each with its OWN proofs.

    Retention compares coverage, not validity. Selection must still compare
    post-review content; a formerly rich packet cannot hide a valid partial.
    """
    previous = snapshots.get(key)
    retained_key = key + "_previous"
    retained = snapshots.get(retained_key)
    coverage = content_coverage(candidate["evidence"])
    if previous and content_coverage(previous["evidence"]) - coverage:
        if not retained or len(content_coverage(previous["evidence"])) > len(content_coverage(retained["evidence"])):
            snapshots[retained_key] = previous
    elif retained and content_coverage(retained["evidence"]) <= coverage:
        snapshots.pop(retained_key, None)
    snapshots[key] = candidate


def remember_reusable(owner, analysis: dict) -> None:
    """Keep scoped answers, not an old roster or persisted restart cache."""
    stamp = getattr(owner, "_next_frame_stamp", None)
    guard = getattr(owner, "_next_frame_query_guard", None)
    if stamp is None or not guard:
        return
    from .fact_lifecycle import candidate_evidence
    # Cache insertion keeps an internally guarded CANDIDATE. Recognition age
    # is not answer permission; review_snapshot still requires a fresh frame.
    # Otherwise slow-but-validated first reads never enter the recovery path.
    evidence = candidate_evidence(analysis)
    if not evidence["fresh"] or not evidence["scene"]["verified"]:
        return
    snapshots = getattr(owner, "_reusable_observations", {})
    if any(s.get("evidence", {}).get("page_type") != evidence["page_type"] for s in snapshots.values()):
        snapshots = {}  # Never retain another page's navigation/identity cache.
    full_guard = getattr(owner, "_next_frame_full_query_guard", None)
    if evidence["read_scope"] == "full" and full_guard:
        full = copy.deepcopy(evidence)
        if not getattr(owner, "_next_frame_identity_guarded", False):
            full.update(characters=[], team=[], controlled_character=None, can_answer_character_identity=False)
        _remember_candidate(snapshots, "full", {"evidence": full, "stamp": stamp, "guard": full_guard,
                             "control_trace": copy.deepcopy((analysis.get("recognition") or {}).get("control_trace") or {}),
                             "identity_proofs": copy.deepcopy(analysis.get("characters") or [])
                                 if getattr(owner, "_next_frame_identity_guarded", False) else [],
                             "page_controls": copy.deepcopy(getattr(owner, "_next_frame_page_regions", ())),
                             "epoch": getattr(owner, "_observation_epoch", 0),
                             "game_pid": getattr(owner, "_analysis_game_pid", 0)})
    # A scene/navigation snapshot cannot answer who, equipment or skill questions.
    evidence.update(characters=[], team=[], controlled_character=None,
                    can_answer_character_identity=False, identity_list_complete=False)
    # Keep literal page proofs before scoped snapshots remove unrelated rows.
    # Otherwise navigation loses the heading text that full reads can verify.
    controls = copy.deepcopy(getattr(owner, "_next_frame_page_regions", ()))
    from .delivery_scope import coordinates
    rows = (evidence.get("screen_facts") or {}).get("visible_text") or []
    for control in controls:
        texts = {r.get("text") for r in rows if coordinates(r) == coordinates(control) and r.get("text")}
        if len(texts) == 1:
            control.update(text=next(iter(texts)), control=True)
    common = {"evidence": evidence, "stamp": stamp, "guard": guard,
              "control_trace": copy.deepcopy((analysis.get("recognition") or {}).get("control_trace") or {}),
              "page_controls": controls, "identity_proofs": [],
              "epoch": getattr(owner, "_observation_epoch", 0),
              "game_pid": getattr(owner, "_analysis_game_pid", 0)}
    answer = navigation_answer(evidence, requested=True)
    # A proved page does not depend on readable corner text, and a proved
    # quest does not require a proved location. These are guarded candidates,
    # not a statement that the whole navigation request has been completed.
    projected = scoped_snapshot(owner, evidence, guard, {"current_location"})
    if projected:
        _remember_candidate(snapshots, "scene", {**common, **projected})
    if any(answer[field]["known"] for field in ("current_location", "objective", "quest_title", "destination")):
        projected = scoped_snapshot(owner, evidence, guard,
            {"current_location", "tracked_quest", "quest_objective", "quest_destination"})
        if projected:
            _remember_candidate(snapshots, "navigation", {**common, **projected})
    owner._reusable_observations = snapshots


def scoped_snapshot(owner, evidence, guard, kinds):
    """Guard/expose only the requested facts PLUS independent page controls.

    A floating NPC or equipment count cannot invalidate a location answer.
    Removing its guard also removes its public value: never authorize fields
    whose pixels were not checked. Full reads retain their complete guard.
    """
    def coordinates(region):
        return tuple(region.get(k, -1) for k in ("left", "top", "right", "bottom"))
    all_facts = evidence["screen_facts"]["facts"]
    facts = [f for f in all_facts if f.get("kind") in kinds]
    def literal(fs):
        return {coordinates(p["region"]) for f in fs for p in f.get("evidence") or []
                if p.get("region") and p.get("validation_scope") != "control_presence"}
    wanted = literal(facts)
    page_regions = getattr(owner, "_next_frame_page_regions", None)
    if not page_regions:
        # Legacy fixtures/embedders: unexplained guard regions remain controls,
        # rather than silently dropping a possibly page-identifying region.
        wanted |= {coordinates(r["region"]) for r in guard[0]} - literal(all_facts)
    else:
        wanted |= {coordinates(r["region"]) for r in page_regions if r.get("region")}
    pairs = [(r, p) for r, p in zip(*guard) if coordinates(r["region"]) in wanted]
    if not pairs or {p[0] for _, p in pairs} != wanted:
        return None  # Missing proof coverage is not a reusable snapshot.
    scoped = copy.deepcopy(evidence)
    scoped["screen_facts"].update(facts=copy.deepcopy(facts), visible_text=[], unclassified_text=[])
    scoped["screen_facts"]["field_checks"] = [c for c in scoped["screen_facts"].get("field_checks") or []
                                             if c.get("kind") in kinds]
    return {"evidence": scoped, "guard": ([r for r, _ in pairs], tuple(p for _, p in pairs))}


async def try_reuse(owner, *, navigation: bool, full: bool = False, require_characters: bool = False, plan=None, pixel_only=False, allow_stale=False):
    from .live_refresh import delivery_turn, mark_delivery_verified
    if full and plan is not None:
        from .query_coordinator import has_answerable_content
        snapshots = getattr(owner, "_reusable_observations", {})
        if not any(s and has_answerable_content(plan, s["evidence"])
                   for s in (snapshots.get("full"), snapshots.get("full_previous"))):
            from .live_refresh import trace_refresh
            trace_refresh(owner, "reuse_miss", reason="no_answerable_fields")
            return None  # Join the producer without waiting for a pointless check.
    async with delivery_turn(owner):
        result = await _try_reuse(owner, navigation=navigation, full=full, require_characters=require_characters, plan=plan,
                                  pixel_only=pixel_only, allow_stale=allow_stale)
        if result is not None:
            mark_delivery_verified(owner)
        return result


async def _try_reuse(owner, *, navigation: bool, full: bool, require_characters: bool, plan=None, pixel_only=False, allow_stale=False):
    # One bounded retry may use the newly committed snapshot. An obsolete
    # checker must never revoke it or force another complete recognition.
    generation = getattr(owner, "_answer_query_generation", 0)
    key = "full" if full else "navigation" if navigation else "scene"
    for _ in range(2):
        if plan is not None:
            # The resident producer may have committed while an older cache
            # review was suspended. Consume THIS query's producer receipt
            # before starting another transfer/native review of the new data.
            from .observation_store import ObservationStore
            from .review_policy import producer_receipt
            produced = producer_receipt(owner, await ObservationStore(owner).current(), plan=plan)
            if produced is not None:
                return produced
        selected = await _try_reuse_once(owner, navigation=navigation, full=full, require_characters=require_characters, plan=plan,
                                       pixel_only=pixel_only, allow_stale=allow_stale)
        if selected is not _SUPERSEDED:
            from .query_coordinator import answer_coverage, satisfied_fields
            current = getattr(owner, "_reusable_observations", {}).get(key)
            original = ((selected or {}).get("reuse") or {}).get("original_serial")
            current_serial = ((current or {}).get("evidence", {}).get("screen_facts") or {}).get("observation_serial")
            if (plan is not None and current
                    and getattr(owner, "_reusable_observations", {}).get(key + "_previous")
                    and (selected is None or not plan.fields <= satisfied_fields(plan, selected))
                    and (selected is None or original != current_serial)
                    and answer_coverage(plan, current["evidence"]) > answer_coverage(plan, selected)):
                # A richer retained item can fail its OWN live proofs while
                # leaving a nonempty page-only packet. Recompare coverage
                # AFTER review, then review the current packet's OWN proofs.
                # This is the existing bounded fallback, not a merge/retry.
                fallback = await _try_reuse_once(owner, navigation=navigation, full=full,
                    require_characters=require_characters, plan=plan, prefer_current=True, pixel_only=pixel_only,
                    allow_stale=allow_stale)
                if fallback is _SUPERSEDED:
                    continue
                if fallback is not None and answer_coverage(plan, fallback) >= answer_coverage(plan, selected):
                    from .live_refresh import trace_refresh
                    trace_refresh(owner, "reuse_postreview_selected", serial=current_serial,
                                  before_rows=answer_coverage(plan, selected)[2],
                                  after_rows=answer_coverage(plan, fallback)[2])
                    return fallback
            return selected
        if (generation != getattr(owner, "_answer_query_generation", 0)
                or not owner._observer_status.get("enabled") or getattr(owner, "_observer_stop_requested", False)):
            break
    return None


async def _try_reuse_once(owner, *, navigation: bool, full: bool, require_characters: bool, plan=None, prefer_current=False, pixel_only=False, allow_stale=False):
    from .query_coordinator import has_answerable_content

    snapshots = getattr(owner, "_reusable_observations", {})
    key = "full" if full else "navigation" if navigation else "scene"
    snapshot = snapshots.get(key)
    if snapshot is None and plan is not None and plan.fields == {"page"}:
        # A full candidate may contain good controls but no location. Do not
        # make independent page proof depend on a navigation cache existing.
        snapshot = snapshots.get("full")
    if plan is not None:
        from .query_coordinator import satisfied_fields
        # Keep at most one detached richer read. It MUST still pass its own
        # age, epoch, process, page and literal guards below; never merge rows.
        candidates = [snapshot] if prefer_current else [snapshot, snapshots.get(key + "_previous")]
        # A stale fallback should not answer with an early one-name commit
        # when a richer same-page roster is also available. Pixels still
        # decide admission; this only changes candidate order.
        candidates = prefer_richer_roster(candidates, plan, allow_stale=allow_stale)
        complete = next((s for s in candidates if s and plan.fields <= satisfied_fields(plan, s["evidence"])), None)
        if complete is not None:
            snapshot = complete
        else:
            snapshot = next((s for s in candidates if s and has_answerable_content(plan, s["evidence"])), None)
    if snapshot and require_characters and not snapshot["evidence"].get("characters") and (
            plan is None or not has_answerable_content(plan, snapshot["evidence"])):
        snapshot = None
    # A verified menu can answer the page part of a navigation question,
    # without pretending an exploration quest exists on that menu.
    if navigation and not full and snapshot is None:
        candidate = snapshots.get("full")
        if plan is not None and candidate and has_answerable_content(plan, candidate["evidence"]):
            snapshot = candidate
        scene = snapshots.get("scene")
        if snapshot is None and scene and (scene["evidence"]["page_type"] != "exploration_hud"
                      or plan is not None and has_answerable_content(plan, scene["evidence"])):
            snapshot = scene
    return await review_snapshot(owner, snapshot, navigation=navigation, full=full, plan=plan, pixel_only=pixel_only,
                                 allow_stale=allow_stale)


async def review_snapshot(owner, snapshot, *, navigation=False, full=True, plan=None, allow_partial=True, pixel_only=False, allow_stale=False):
    """The live admission policy shared by cache, direct and follow-up reads.

    Selection is separate from review. Pixels challenge dependencies; only a
    confirmed page/lifecycle transition revokes the underlying candidate.
    """
    import asyncio

    from .fact_lifecycle import observation_tick
    from .live_refresh import (
        compare_live_frames,
        fetch_recent_frame,
        frame_stamp,
        invalidate_frame_change,
        trace_refresh,
    )

    if not snapshot or getattr(owner, "_observation_invalidated", False):
        trace_refresh(owner, "reuse_miss", reason="candidate_missing" if not snapshot else "observation_invalidated")
        return None
    pending_reason = getattr(owner, "_observation_pending_reason", "")
    recovering = pending_reason.startswith(("capture_unavailable", "capture_error"))
    if (not owner._observer_status.get("enabled") or getattr(owner, "_observer_stop_requested", False)
            or owner._observer_status.get("state") not in {"observing", "updating"} and not (
            recovering and owner._observer_status.get("state") in {"error", "updating"})):
        trace_refresh(owner, "reuse_miss", reason="observer_not_observing")
        return None
    evidence = snapshot["evidence"]
    guard = snapshot["guard"]
    if plan is not None:
        from .delivery_scope import select_scoped_guard
        selected = select_scoped_guard(owner, evidence, plan, [guard], proof_source=snapshot, allow_partial=allow_partial)
        if selected is None:
            trace_refresh(owner, "reuse_miss", reason="missing_proof")
            return None
        evidence, guard = selected
    epoch = getattr(owner, "_observation_epoch", 0)
    serial = getattr(owner, "_observation_serial", 0)
    owns_current = (snapshot["evidence"].get("screen_facts") or {}).get("observation_serial", serial) == serial
    generation = getattr(owner, "_answer_query_generation", 0)

    def still_current():
        return (epoch, serial, generation) == (getattr(owner, "_observation_epoch", 0),
            getattr(owner, "_observation_serial", 0), getattr(owner, "_answer_query_generation", 0))
    try:
        age = observation_tick(time.time()) - datetime.fromisoformat(evidence["observed_at"]).timestamp()
    except (ValueError, TypeError, KeyError):
        return None
    age_policy = stale_state(evidence.get("page_type"), age, allow_stale=allow_stale)
    if age_policy == "invalid" or snapshot["epoch"] != epoch:
        trace_refresh(owner, "reuse_miss", reason="candidate_epoch_mismatch" if snapshot["epoch"] != epoch else "invalid_timestamp")
        return None
    if age_policy == "expired":
        trace_refresh(owner, "reuse_miss", reason="stale_expired", age_seconds=round(age, 1))
        return None
    if age_policy == "not_allowed":
        trace_refresh(owner, "reuse_miss", reason="stale_not_allowed", age_seconds=round(age, 1))
        return None
    client = getattr(owner, "_assistant_client", None)
    if client is None or not snapshot["game_pid"]:
        return None
    # A single old transfer is not proof the game is unavailable. Retry the
    # capture sequence within the SAME caller deadline, not the whole OCR.
    latest = await fetch_recent_frame(owner)
    if not still_current():
        trace_refresh(owner, "reuse_superseded", serial=serial,
                      current_serial=getattr(owner, "_observation_serial", 0))
        return _SUPERSEDED
    if (latest is None or not 0 <= time.time() - latest.timestamp <= LIVE_FRAME_MAX_AGE_SECONDS
            or latest.game.get("pid") != snapshot["game_pid"] or latest.game.get("minimized")
            or not latest.game.get("running") or latest.capture.get("state") != "capturing"
            or latest.capture.get("isolated_window") is not True):
        trace_refresh(owner, "reuse_miss", reason="capture_unavailable")
        await owner._invalidate_current_observation("capture_unavailable_for_reuse")
        return None
    regions, patches = guard
    try:
        current_stamp = await asyncio.to_thread(frame_stamp, latest.image_bytes)
        current_patches = await asyncio.to_thread(region_stamp, latest.image_bytes, regions)
    except (OSError, ValueError):
        if not still_current():
            return _SUPERSEDED
        trace_refresh(owner, "reuse_miss", reason="invalid_image")
        await owner._invalidate_current_observation("capture_unavailable_for_reuse")
        return None
    if not still_current():
        trace_refresh(owner, "reuse_superseded", serial=serial,
                      current_serial=getattr(owner, "_observation_serial", 0))
        return _SUPERSEDED
    # Global transitions plus native-resolution text/control checks. This is a
    # conservative heuristic, not proof that arbitrary unseen pixels are equal.
    # Exploration's 3D camera is not a page transition. Its text/control
    # regions still determine whether this particular snapshot can be reused.
    # A complete native guard already includes independent page controls.
    # Coarse animated pixels are not a second, overriding page detector.
    global_changed = compare_live_frames(owner, snapshot["stamp"], current_stamp, ui_only=bool(regions))
    text_changed = regions_changed(patches, current_patches)
    native_reviewed = False
    if pixel_only and (global_changed or text_changed):
        trace_refresh(owner, "native_review_deferred", reason="page_first", region_count=len(regions))
        return None  # Not a proof, not a revocation, and never an OCR queue job.
    if text_changed and not global_changed and len(patches) == len(current_patches):
        from .live_refresh import revalidate_guard_text

        if (getattr(owner, "_vision_schedule_role", "idle") == "producer"
                and not callable(getattr(getattr(owner, "_vision", None), "enqueue_review", None))):
            # This cache needs native OCR, but the resident producer already
            # owns recognition and its final validation. Join that producer
            # through refresh_for_query rather than spending the entire query
            # deadline queued with a frame that will be stale on admission.
            # Pixel-only cache hits above remain usable during recognition.
            trace_refresh(owner, "delivery_deferred", reason="busy")
            return None
        coordinates = {before[0] for before, after in zip(patches, current_patches)
                       if regions_changed((before,), (after,))}
        same_text = await revalidate_guard_text(owner, latest, coordinates, evidence=evidence, controls=regions,
                                               reference_guard=guard)
        native_reviewed = True
        if not still_current():
            trace_refresh(owner, "reuse_superseded", serial=serial,
                          current_serial=getattr(owner, "_observation_serial", 0))
            return _SUPERSEDED
        if same_text.capture is not None:
            # The producer admitted the queued review on a NEW capture. Bind
            # data/patches to that capture, never its stale pre-queue pixels.
            latest = same_text.capture
            current_patches = await asyncio.to_thread(region_stamp, latest.image_bytes, regions)
            current_stamp = await asyncio.to_thread(frame_stamp, latest.image_bytes)
            if not still_current() or len(current_patches) != len(patches):
                return _SUPERSEDED if not still_current() else None
            coordinates = {a[0] for a, b in zip(patches, current_patches) if regions_changed((a,), (b,))}
            from .live_refresh import TextVerification
            proved = same_text.confirmed_coordinates
            same_text = TextVerification("confirmed" if coordinates <= proved else "unconfirmed", proved)
        if not 0 <= time.time() - latest.timestamp <= LIVE_FRAME_MAX_AGE_SECONDS:
            # OCR can finish after its input's two-second capture window. Do
            # not repeat OCR or relax that window: carry only literal proofs
            # whose native patches still match a genuinely fresh frame.
            from .live_refresh import TextVerification
            newer = await fetch_recent_frame(owner)
            if not still_current():
                return _SUPERSEDED
            if newer is None or newer.game.get("pid") != snapshot["game_pid"]:
                return None
            newer_patches = await asyncio.to_thread(region_stamp, newer.image_bytes, regions)
            if not still_current():
                return _SUPERSEDED
            if (len(newer_patches) != len(current_patches)
                    or not 0 <= time.time() - newer.timestamp <= LIVE_FRAME_MAX_AGE_SECONDS):
                return None
            unchanged = {a[0] for a, b in zip(current_patches, newer_patches) if not regions_changed((a,), (b,))}
            admitted = (coordinates if same_text else same_text.confirmed_coordinates) & unchanged
            coordinates = {a[0] for a, b in zip(patches, newer_patches) if regions_changed((a,), (b,))}
            same_text = TextVerification("confirmed" if coordinates <= admitted else "unconfirmed", frozenset(admitted))
            latest, current_patches = newer, newer_patches
            current_stamp = await asyncio.to_thread(frame_stamp, latest.image_bytes)
            if not still_current():
                return _SUPERSEDED
            trace_refresh(owner, "verification_frame_advanced", region_count=len(admitted))
        if same_text:
            from .delivery_scope import update_checked_patches
            from .observation_store import ObservationStore
            if owns_current:
                ObservationStore(owner).clear_deferred(coordinates)
            update_checked_patches(owner, current_patches, selected_snapshot=snapshot)
            if plan is None:
                snapshot["guard"] = (regions, current_patches)
                snapshot["stamp"] = current_stamp
                owner._accepted_frame_stamp = current_stamp
            delivery = getattr(owner, "_accepted_frame_delivery_guard", None)
            mapped = {patch[0]: patch for patch in current_patches}
            if plan is None and delivery and all(patch[0] in mapped for patch in delivery[1]):
                owner._accepted_frame_delivery_guard = (delivery[0], tuple(mapped[p[0]] for p in delivery[1]))
            text_changed = False
            trace_refresh(owner, "reuse_text_revalidated", serial=serial)
        else:
            if not owns_current and (plan is None or not allow_partial
                    or same_text.state not in {"unconfirmed", "missing_proof"}):
                trace_refresh(owner, "reuse_miss", reason="retained_proof_rejected")
                return None  # Old proofs cannot quarantine or revoke a newer commit.
            if same_text.confirmed_coordinates:
                from .delivery_scope import update_checked_patches
                from .observation_store import ObservationStore
                proved = same_text.confirmed_coordinates
                update_checked_patches(owner, tuple(p for p in current_patches if p[0] in proved), selected_snapshot=snapshot)
                mapped = {p[0]: p for p in current_patches if p[0] in proved}
                guard = (regions, tuple(mapped.get(p[0], p) for p in patches))
                patches = guard[1]
                if owns_current:
                    ObservationStore(owner).clear_deferred(proved)
                coordinates -= proved
            # Failure to prove a literal (including weak OCR) is NOT a new
            # value. Background and query reviewers quarantine the same way;
            # neither destroys fields or the reusable candidate collection.
            from .observation_store import ObservationStore
            if owns_current:
                ObservationStore(owner).defer_regions(coordinates)
            trace_refresh(owner, "delivery_deferred", reason=same_text.state)
            if plan is None or not allow_partial or same_text.state not in {"unconfirmed", "missing_proof"}:
                return None
            from .delivery_scope import coordinates as region_key
            from .delivery_scope import select_scoped_guard, withhold_regions
            controls = snapshot.get("page_controls", ())
            if coordinates & {region_key(p) for p in controls}:
                # Use the SAME page-proof rule as the producer. A weak
                # redundant tab is not a changed page when the remaining
                # independently proved controls still classify that page.
                # This subset belongs only to this delivery, never the store.
                from ..detectors.layout import select_proved_equipment_controls
                # frame_stamp already decoded THIS latest capture off-thread.
                # Reuse its native dimensions, rather than reopen the same
                # image synchronously on the query/follow-up event loop.
                width, height = current_stamp[0]
                admitted_controls = select_proved_equipment_controls(
                    {"page_type": evidence.get("page_type")}, controls,
                    lambda p: region_key(p) not in coordinates, width=width, height=height)
                if not admitted_controls:
                    return None
                snapshot = {**snapshot, "page_controls": admitted_controls}
                trace_refresh(owner, "delivery_page_controls_selected", input_count=len(controls),
                              proved_count=len(admitted_controls))
            pruned = withhold_regions(evidence, coordinates, identity_proofs=snapshot.get("identity_proofs", ()))
            partial = select_scoped_guard(owner, pruned, plan, [guard], proof_source=snapshot, allow_partial=True)
            if partial is None or any(p[0] in coordinates for p in partial[1][1]):
                return None
            evidence, guard = partial
            regions, patches = guard
            current_patches = await asyncio.to_thread(region_stamp, latest.image_bytes, regions)
            if not still_current() or regions_changed(patches, current_patches):
                return _SUPERSEDED if not still_current() else None
            text_changed = False
    if global_changed or text_changed:
        trace_refresh(owner, "reuse_miss", reason="pixels_changed", global_changed=global_changed, regions_changed=text_changed)
        if owns_current:
            await invalidate_frame_change(owner, snapshot["stamp"], current_stamp,
                                          getattr(owner, "_accepted_frame_page_regions", ()), "visual_snapshot_needs_refresh")
        return None
    from .observation_store import ObservationStore
    if owns_current:
        ObservationStore(owner).clear_deferred({patch[0] for patch in patches})
    from .delivery_scope import coordinates as region_key
    control_coordinates = {region_key(p) for p in snapshot.get("page_controls", ())}
    deferred = getattr(owner, "_deferred_verification", None)
    # The resident watcher may quarantine control PIXELS while this consumer
    # awaits exact-text verification. The checked packet is valid for its own
    # scope; it must not bless all other fields or clear a capture/stop failure.
    control_quarantine_only = (
        getattr(owner, "_observation_pending_reason", "") == "verification_pending_control"
        and bool(control_coordinates) and control_coordinates <= {p[0] for p in patches}
        and not (deferred and deferred[:2] == (epoch, serial) and deferred[2] & control_coordinates)
    )
    if (epoch != getattr(owner, "_observation_epoch", 0) or serial != getattr(owner, "_observation_serial", 0)
            or not owner._observer_status.get("enabled") or getattr(owner, "_observer_stop_requested", False)
            or owner._observer_status.get("state") in {"paused", "stopped"}
            or pending_reason != getattr(owner, "_observation_pending_reason", "") and not control_quarantine_only):
        trace_refresh(owner, "reuse_miss", reason="invalidated_during_check")
        return None
    if recovering:
        # Only this scoped handoff is authorized. Keep pending_reason masking
        # the global full result; its other actors/text were NOT revalidated.
        owner._observer_status.update(state="observing", message="正在核对当前界面信息")
    result = copy.deepcopy(evidence)
    result["read_scope"] = "full" if full else "navigation" if navigation else "scene"
    pending_character = bool(result.get("equipment_complete") and not result.get("characters")
                             and not result.get("page_characters"))
    result.update(status="confirmed", failure_reason="character_pending" if pending_character else "",
                  text_coverage_complete=False,
                  message=("已用新采集画面核对遗器词条；当前帧未确认角色归属。" if pending_character else
                           "已用新采集画面核对本结果依赖的文字和控件区域；未重复整轮识别，未确认的字段仍未知。"))
    result["screen_facts"]["unclassified_text"] = []
    try:
        final_age = observation_tick(time.time()) - datetime.fromisoformat(result["observed_at"]).timestamp()
    except (ValueError, TypeError, KeyError):
        final_age = age
    stale = stale_state(result.get("page_type"), final_age, allow_stale=allow_stale) == "stale"
    result["freshness"] = "stale" if stale else "fresh"
    result["age_seconds"] = round(max(0.0, final_age), 1)
    if stale:
        seconds = max(1, int(round(final_age)))
        result["age_label"] = f"这是 {seconds} 秒前的画面，现在可能变了"
        result["message"] = result["age_label"] + "：" + result.get("message", "")
    original_serial = result["screen_facts"].get("observation_serial", 0)
    # This is a delivery revision, not a new OCR/capture timestamp. Pixels for
    # these scoped values passed validation against this stable store revision.
    result["screen_facts"]["observation_serial"] = serial
    from .equipment_view import equipment_card
    result["screen_facts"]["equipment_card"] = equipment_card(result)
    result["reuse"] = {"validated_at": datetime.fromtimestamp(latest.timestamp).astimezone().isoformat(),
                       "original_age_seconds": round(age, 3), "ocr_repeated": False, "epoch": epoch,
                       "original_serial": original_serial, "validated_revision": serial,
                       "answer_generation": generation, "reviewed_fields": sorted(plan.fields) if plan else None,
                       "native_text_reviewed": native_reviewed, "full_recognition_repeated": False,
                       "stale": stale, "age_seconds": result["age_seconds"]}
    result["reuse"].update(reviewed_coordinates=[list(p[0]) for p in patches],
                           page_control_coordinates=[list(c) for c in sorted(control_coordinates)],
                           challenge_revision=getattr(owner, "_review_challenge_serial", 0),
                           capture_failure_revision=getattr(owner, "_capture_failure_revision", 0))
    from .delivery_boundary import fact_packet_digest
    result["reuse"]["fact_packet_digest"] = fact_packet_digest(result)
    if owns_current:
        from .fact_lifecycle import record_verified_regions
        record_verified_regions(owner, {p[0] for p in patches}, latest.timestamp, serial=serial)
    result["last_verified_at"] = result["reuse"]["validated_at"]
    result["freshness_basis"] = "scoped_live_verification"
    slots = getattr(owner, "_team_slot_states", None)
    if slots:
        from .observation_store import attach_team_roster
        attach_team_roster(result, slots)
    trace_refresh(owner, "reuse_validated", original_age_seconds=round(age, 3),
                  stale=stale, age_seconds=result["age_seconds"])
    return result
