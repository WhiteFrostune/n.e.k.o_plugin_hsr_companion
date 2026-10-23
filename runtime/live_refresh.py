"""In-memory change guard and bounded fresh queries; one OCR producer only."""

from __future__ import annotations

import asyncio
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from typing import Any

from ..adapters.text_reader import ObservationSuperseded
from ..adapters.text_verification import identity_literal, proof_key
from ..detectors.exploration import generic_navigation
from .query_reuse import LIVE_FRAME_MAX_AGE_SECONDS, region_stamp, regions_changed, text_pixel_differences, try_reuse

LIVE_POLL_SECONDS = 0.25


@dataclass(frozen=True)
class TextVerification:
    state: str
    confirmed_coordinates: frozenset = frozenset()
    capture: Any = None  # Internal atomic frame used by a cooperative review.

    def __bool__(self):
        return self.state == "confirmed"


@asynccontextmanager
async def delivery_turn(owner):
    """Give bounded handoff verification priority over the NEXT OCR pass."""
    owner._delivery_waiters = getattr(owner, "_delivery_waiters", 0) + 1
    event = getattr(owner, "_delivery_idle_event", None)
    if event is None:
        owner._delivery_idle_event = event = asyncio.Event()
    event.clear()
    try:
        # Pixel-only reuse does not need OCR admission and must remain usable
        # while the resident recognizer is busy. Only native rereads queue.
        yield
    finally:
        owner._delivery_waiters -= 1
        if not owner._delivery_waiters:
            event.set()


def mark_delivery_verified(owner):
    """A checked plugin packet, never a host speech/submission receipt."""
    owner._delivery_seen_revision = (getattr(owner, "_observation_epoch", 0), getattr(owner, "_observation_serial", 0))


async def wait_delivery_turn(owner):
    revision = (getattr(owner, "_observation_epoch", 0), getattr(owner, "_observation_serial", 0))
    consumer = getattr(owner, "_visual_query_waiters", 0) or getattr(owner, "_pending_answer", None)
    if (consumer and revision[1] and getattr(owner, "_last_full_commit_serial", 0) == revision[1]
            and getattr(owner, "_delivery_seen_revision", None) != revision
            and getattr(owner, "_delivery_offered_revision", None) != revision):
        owner._delivery_offered_revision = revision
        # A committed-result event has already woken real consumers. Yield a
        # scheduling turn so they can enter delivery_turn; then honor ACTUAL
        # claims below. A waiter count or a sleeping followup is not a claim
        # and must not reserve .75s ahead of producing the next page.
        await asyncio.sleep(0)
    if getattr(owner, "_delivery_waiters", 0):
        try:
            await asyncio.wait_for(owner._delivery_idle_event.wait(), timeout=2.5)
        except TimeoutError:
            pass  # A waiter cannot starve the resident producer indefinitely.


async def fetch_recent_frame(owner, *, wait_seconds=2.0):
    """Wait for a genuinely fresh helper frame; never rewrite its timestamp.

    The helper's cached frame can age while OCR runs. Retry capture reads,
    not OCR; keep the existing two-second freshness requirement.
    """
    deadline = time.monotonic() + wait_seconds
    since = 0
    while True:
        try:
            latest = await asyncio.to_thread(owner._assistant_client.fetch_frame, since)
        except RuntimeError:
            latest = None
        if latest is not None:
            if (not latest.game.get("running") or latest.game.get("minimized")
                    or latest.capture.get("state") != "capturing" or latest.capture.get("isolated_window") is not True
                    or getattr(owner, "_analysis_game_pid", 0) and latest.game.get("pid") != owner._analysis_game_pid):
                return None
            age = time.time() - latest.timestamp
            if 0 <= age <= LIVE_FRAME_MAX_AGE_SECONDS:
                return latest
            since = getattr(latest, "frame_seq", 0)
            trace_refresh(owner, "capture_retry", reason="stale_frame", age_seconds=round(age, 3))
        if time.monotonic() >= deadline or getattr(owner, "_observer_stop_requested", False):
            return None
        await asyncio.sleep(min(.15, max(0, deadline - time.monotonic())))


def trace_refresh(owner: Any, stage: str, **details: Any) -> None:
    """Bounded per-query timings only; never log game text or image bytes."""
    trace = getattr(owner, "_active_refresh_trace", None)
    if trace is not None and len(trace["stages"]) < 48:
        trace["stages"].append({"stage": stage, "after_seconds": round(time.monotonic() - trace["started"], 3), **details})


def fact_regions(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    screen = analysis.get("screen_facts") or {}
    regions = [{"region": proof["region"]}
               for fact in screen.get("facts") or []
               for proof in fact.get("evidence") or [] if proof.get("region") and proof.get("validation_scope") != "control_presence"]
    if (analysis.get("page") or {}).get("page_type", "unknown") == "unknown":
        regions.extend({"region": item["region"]} for item in screen.get("visible_text") or [] if item.get("region"))
    return regions


def frame_stamp(payload: bytes):
    from PIL import Image

    try:
        with Image.open(BytesIO(payload)) as image:
            return image.size, image.convert("L").resize((320, 200), Image.Resampling.BILINEAR)
    except (OSError, ValueError):
        return None


def stable_screen_content(analysis: dict, original: bytes, latest: bytes, *, verifier=None, cancelled=None, trace=None) -> dict:
    """Withdraw changed fields/rows, not the independently proven page.

    This does NOT reread/guess replacement text. Dynamic counts/backgrounds
    may cause conservative omissions. A fact needs all its proof regions.
    """
    screen = analysis.get("screen_facts") or {}
    rows = screen.get("visible_text") or []
    facts = screen.get("facts") or []
    def fact_proof(proof):
        return {**proof, "control": proof.get("validation_scope") == "control_presence"
                or generic_navigation(str(proof.get("text") or ""))}

    proofs = [fact_proof(p) for f in facts for p in f.get("evidence") or []]
    actors = analysis.get("characters") or []
    actor_literals = [p for a in actors if (p := identity_literal(a)) is not None]
    page_verified = bool((analysis.get("scene") or {}).get("verified")
                         and (analysis.get("page") or {}).get("page_type") != "unknown")
    # Context controls belong to page validation, not the value's literal
    # equality check. Otherwise one unreadable keyboard hint deletes every
    # location/quest value despite an independently confirmed page.
    proofs_to_verify = [p for p in proofs if not page_verified or not p.get("control")]
    if not (analysis.get("recognition") or {}).get("navigation_only_query"):
        proofs_to_verify.extend(rows)
    proofs.extend(rows)
    from .delivery_scope import coordinates
    controls = []
    for control in (analysis.get("recognition") or {}).get("scene_regions") or []:
        texts = {r.get("text") for r in rows if coordinates(r) == coordinates(control) and r.get("text")}
        controls.append({**control, "control": True, **({"text": next(iter(texts))} if len(texts) == 1 else {})})
    regions = [{"region": p["region"]} for p in [*controls, *actors, *proofs] if p.get("region")]
    # Avoid duplicate proof boxes consuming the bounded patch budget.
    regions = list({tuple(item["region"].get(key, -1) for key in ("left", "top", "right", "bottom")): item
                    for item in regions}.values())
    before = {patch[0]: patch for patch in region_stamp(original, regions)}
    after = {patch[0]: patch for patch in region_stamp(latest, regions)}
    pixel_checks = {}
    def pixels_stable(item):
        if not item.get("region"):
            return False  # No live pixel evidence for this fragment.
        coords = tuple(float(item["region"].get(key, -1)) for key in ("left", "top", "right", "bottom"))
        # Facts, visible rows and identity summaries can share one rectangle.
        # This cache exists only for this exact pair of immutable frames.
        if coords not in pixel_checks:
            pixel_checks[coords] = (coords in before and coords in after
                                    and not regions_changed((before[coords],), (after[coords],)))
        return pixel_checks[coords]
    # The same producer validates identity and content on the same new frame.
    # Prioritize identity literals rather than letting unrelated visible text
    # exhaust the bounded native reread budget. Portraits have no text fallback.
    changed_proofs = list({proof_key(p): p for p in [*controls, *actor_literals, *proofs_to_verify]
                          if p.get("text") and not pixels_stable(p)}.values())
    verified = set()
    if changed_proofs and callable(verifier):
        try:
            verified = verifier(latest, changed_proofs, cancelled=cancelled)
        except ObservationSuperseded:
            raise
        except (RuntimeError, ValueError, OSError, ImportError):
            pass  # A failed reread is unknown, never authorization to keep old text.

    if trace is not None:
        trace.append({"stage": "proof_selection", "page_verified": page_verified,
                      "changed": [{"proof": p, "index": i,
                                   "inside_reread_budget": i < 16,
                                   "text_revalidated": proof_key(p) in verified}
                                  for i, p in enumerate(changed_proofs)]})

    def stable(item):
        return pixels_stable(item) or proof_key(item) in verified

    from PIL import Image

    from ..detectors.layout import select_proved_equipment_controls
    with Image.open(BytesIO(latest)) as current_image:
        page_controls = select_proved_equipment_controls(analysis.get("page") or {}, controls, stable,
                                                        width=current_image.width, height=current_image.height)

    retained_actors = [a for a in actors if pixels_stable(a)
                       or (p := identity_literal(a)) is not None and proof_key(p) in verified]
    identity_summary = {"observed": len(actors), "confirmed": len(retained_actors),
        "pixels_unchanged": sum(pixels_stable(a) for a in actors),
        "text_revalidated": sum(not pixels_stable(a) for a in retained_actors),
        "withheld": len(actors) - len(retained_actors)}
    retained = [row for row in rows if stable(row)]
    retained_facts, checks = [], []
    for fact in facts:
        proof = [fact_proof(p) for p in fact.get("evidence") or []]
        literals = [p for p in proof if not p.get("control")]
        accepted = bool(literals) and all(stable(p) for p in literals) and (
            page_verified or all(stable(p) for p in proof))
        if accepted:
            retained_facts.append(fact)
        if trace is not None:
            trace.append({"stage": "fact_admission", "kind": fact.get("kind"),
                          "label": fact.get("label"), "value": fact.get("value"), "accepted": accepted,
                          "proofs": [{"proof": p, "pixels_stable": pixels_stable(p),
                                      "text_revalidated": proof_key(p) in verified,
                                      "required": not p.get("control") or not page_verified}
                                     for p in proof]})
        checks.append({"kind": fact.get("kind"), "scope": fact.get("scope"),
                       "state": "confirmed" if accepted else "pending",
                       "reason": "text_revalidated" if accepted and any(proof_key(p) in verified for p in proof)
                       else "pixels_unchanged" if accepted else "missing_proof" if not proof else "text_not_revalidated"})
    return {**analysis, "characters": retained_actors,
            "screen_facts": {**screen, "facts": retained_facts, "visible_text": retained, "unclassified_text": [],
                             "field_checks": checks},
            "recognition": {**(analysis.get("recognition") or {}), "unstable_text_rows_removed": len(rows) - len(retained),
                            "control_trace": {**((analysis.get("recognition") or {}).get("control_trace") or {}),
                                              "input_count": len(controls), "proved_count": len(page_controls),
                                              "revalidated": bool(controls) and bool(page_controls)},
                            "unstable_facts_removed": len(facts) - len(retained_facts),
                            "text_revalidation_attempted": min(16, len(changed_proofs)),
                            "text_revalidation_confirmed": len(verified), "identity_validation": identity_summary,
                            "page_controls_revalidated": bool(controls) and bool(page_controls),
                            "scene_regions": page_controls if page_controls else (analysis.get("recognition") or {}).get("scene_regions", [])}}


def frame_changed(previous, current, characters=(), *, ui_only=False) -> bool:
    """Detect large transitions or changes in previously read name regions.

    No pixel hash equality: breathing/particle animations are not page changes.
    This is a conservative change heuristic, not a new scene/character model.
    """
    if previous is None or current is None:
        return False
    if previous[0] != current[0]:
        return True

    # The desktop's bundled Pillow intentionally omits ImageStat/ImageChops.
    # Use the already available Image pixels; no host dependency changes.
    def difference(bounds=None, *, text=False):
        a, b = previous[1], current[1]
        if bounds is not None:
            a, b = a.crop(bounds), b.crop(bounds)
        a, b = a.tobytes(), b.tobytes()
        values = text_pixel_differences(a, b) if text else [abs(x - y) for x, y in zip(a, b)]
        return sum(values) / max(1, len(values)) / 255

    sample_width, sample_height = previous[1].size
    if not ui_only:
        overall = difference()
        heading = difference((0, 0, round(sample_width * .65), round(sample_height * .15)))
        if overall > 0.16 or (overall > 0.045 and heading > 0.12):
            return True
    for item in characters:
        region = item.get("region") or {}
        if item.get("region_precision") == "merged_ocr_line" or not region:
            continue
        bounds = tuple(
            max(0, min(limit, round(float(region.get(key) or 0) * limit)))
            for key, limit in (("left", sample_width), ("top", sample_height), ("right", sample_width), ("bottom", sample_height))
        )
        if bounds[2] > bounds[0] and bounds[3] > bounds[1]:
            if difference(bounds, text=item.get("identity_scope") != "hud_portrait") > 0.12:
                return True
    return False


async def invalidate_frame_change(owner: Any, previous, current, controls, reason: str) -> None:
    """Revoke changed content, retain ONLY a revalidated page label.

    No location, quest, roster or selected object survives this partial update.
    If a page-identifying control changes, revoke the page as well.
    """
    # A pixel observation is a challenge, not a semantic page transition.
    # Keep the candidate and quarantine its page dependencies until reviewed.
    # Recognized new-page previews / process lifecycle still revoke explicitly.
    if controls and getattr(owner, "_committed_observation", None):
        from .delivery_scope import coordinates
        from .observation_store import ObservationStore
        ObservationStore(owner).defer_regions({coordinates(p) for p in controls})
        trace_refresh(owner, "page_controls_deferred", reason=reason, region_count=len(controls))
        return
    publisher = getattr(owner, "_publish_scene_preview", None)
    reader = getattr(owner, "_current_visual_evidence", None)
    epoch = getattr(owner, "_observation_epoch", 0)
    serial = getattr(owner, "_observation_serial", 0)
    known = await reader() if controls and callable(publisher) and callable(reader) else {}
    if (epoch, serial) != (getattr(owner, "_observation_epoch", 0), getattr(owner, "_observation_serial", 0)):
        return
    retain = bool(previous is not None and current is not None
                  and known.get("fresh") and known.get("scene", {}).get("verified")
                  and not frame_changed(previous, current, controls, ui_only=True))
    from .revocation_trace import cause
    with cause(origin="frame_change", basis="page_control_pixels" if controls else "coarse_pixels_only",
               controls=len(controls or ()), candidate=bool(getattr(owner, "_committed_observation", None))):
        await owner._invalidate_current_observation(reason)
    if (not retain or getattr(owner, "_observer_stop_requested", False)
            or (getattr(owner, "_observation_epoch", 0), getattr(owner, "_observation_serial", 0)) != (epoch + 1, serial)):
        return
    scene = known["scene"]
    owner._analysis_frame_stamp = current
    await publisher({"ok": True, "observed_at": known.get("observed_at"),
                     "page": {"page_type": known["page_type"], "scene": scene["primary_state"],
                              "label": scene["label"], "supported": True, "confidence": "high"},
                     "scene": {**scene, "confidence": "high", "evidence": ["当前帧页面标志仍一致；旧内容已撤销"]},
                     "characters": [], "screen_facts": {"facts": [], "visible_text": []},
                     "recognition": {"phase": "scene_only", "scene_regions": list(controls)}})


async def guarded_analysis(owner: Any, payload: bytes, *, helper: bool) -> dict[str, Any] | None:
    """Monitor the live helper while OCR runs; discard superseded work."""
    await wait_delivery_turn(owner)
    from .vision_scheduler import vision_turn
    async with vision_turn(owner, "producer"):
        return await _guarded_analysis(owner, payload, helper=helper)


async def _guarded_analysis(owner: Any, payload: bytes, *, helper: bool) -> dict[str, Any] | None:
    stamp = await asyncio.to_thread(frame_stamp, payload)
    owner._analysis_frame_stamp = stamp
    previous = getattr(owner, "_accepted_frame_stamp", None)
    controls = getattr(owner, "_accepted_frame_page_regions", getattr(owner, "_accepted_frame_scene_regions", ()))
    regions = [*controls, *getattr(owner, "_accepted_frame_characters", ())]
    exploration_ui = (bool(controls) and getattr(owner, "_accepted_frame_page_type", "unknown") != "unknown"
                      or bool(regions) and getattr(owner, "_accepted_frame_page_type", "") == "exploration_hud")
    coarse_regions = controls if controls and exploration_ui else regions
    from .delivery_scope import coordinates
    native_guard = getattr(owner, "_accepted_frame_delivery_guard", None)
    native_page_guard = bool(controls and native_guard and {coordinates(p) for p in controls} <= {p[0] for p in native_guard[1]})
    if not native_page_guard and frame_changed(previous, stamp, coarse_regions, ui_only=exploration_ui):
        await invalidate_frame_change(owner, previous, stamp, controls, "content_or_page_changed")
    epoch = getattr(owner, "_observation_epoch", 0)
    request_generation = getattr(owner, "_query_refresh_generation", 0)
    stop = threading.Event()
    previews = asyncio.Queue()
    pass_preview = {}
    page_check = None
    loop = asyncio.get_running_loop()

    def on_scene(value):
        if not stop.is_set():
            pass_preview.update(value)
            loop.call_soon_threadsafe(previews.put_nowait, value)

    live = getattr(owner._vision, "analyze_live", None)
    scene_reader = getattr(owner._vision, "analyze_scene", None)
    scene_only = bool(callable(scene_reader) and getattr(owner, "_scene_query_waiters", 0) > 0
                      and getattr(owner, "_scene_query_waiters", 0) == getattr(owner, "_visual_query_waiters", 0))
    pending_answer = getattr(owner, "_pending_answer", None)
    if pending_answer is not None and not getattr(owner, "_visual_query_waiters", 0):
        scene_only = bool(callable(scene_reader) and (pending_answer.scene_only or pending_answer.navigation))
    manual = getattr(owner, "_manual_refresh_task", None)
    if manual is not None and not manual.done():
        scene_only = False  # The same producer supplies the requested full read.
    navigation_reader = getattr(owner._vision, "analyze_navigation", None)
    navigation_only = bool(scene_only and callable(navigation_reader) and getattr(owner, "_navigation_query_waiters", 0) > 0)
    navigation_only |= bool(scene_only and callable(navigation_reader) and pending_answer is not None and pending_answer.navigation)
    scope = "navigation" if navigation_only else "scene" if scene_only else "full"
    health_polls, frame_transfers = 0, 0
    monitored_sequence = getattr(owner, "_assistant_frame_seq", 0)
    # Invalid test/failed image bytes are still handled by the OCR error path.
    worker_started = time.monotonic()
    trace_refresh(owner, "analysis_started", scope=scope)
    task = asyncio.create_task(asyncio.to_thread(navigation_reader if navigation_only else scene_reader, payload, cancelled=stop.is_set) if scene_only else asyncio.to_thread(
        live, payload, cancelled=stop.is_set, on_scene=on_scene
    ) if callable(live) else asyncio.to_thread(owner._vision.analyze, payload))
    changed = False
    try:
        while not task.done():
            done, _ = await asyncio.wait({task}, timeout=LIVE_POLL_SECONDS)
            if helper and stamp is not None and getattr(owner, "_query_refresh_generation", 0) > request_generation:
                # Join the producer already owning OCR. Its final live-frame
                # validation binds the result to the latest question. A real
                # page change still stops/discards the old work below.
                request_generation = getattr(owner, "_query_refresh_generation", 0)
                trace_refresh(owner, "query_joined_worker", scope=scope,
                              worker_elapsed_seconds=round(time.monotonic() - worker_started, 3))
            latest = None
            healthy_wait = False
            sequence = None
            if helper and stamp is not None:
                try:
                    health_reader = getattr(owner._assistant_client, "health", None)
                    should_transfer = True
                    if callable(health_reader):
                        status = await asyncio.to_thread(health_reader)
                        health_polls += 1
                        game, capture = status.get("game") or {}, status.get("capture") or {}
                        healthy_wait = bool(status.get("connected") and game.get("running") and not game.get("minimized")
                            and capture.get("state") == "capturing" and capture.get("isolated_window") is True
                            and (not getattr(owner, "_analysis_game_pid", 0) or game.get("pid") == owner._analysis_game_pid))
                        sequence = status.get("frame_seq")
                        # With admitted native controls, the mid-inference
                        # coarse-pixel branch below cannot revoke the page.
                        # These PNGs would never participate in a decision:
                        # keep health polling, then fetch a genuinely fresh
                        # frame for the mandatory final literal/control check.
                        # Unknown/legacy pages retain their intermediate guard.
                        should_transfer = (healthy_wait and not native_page_guard
                            and (not isinstance(sequence, int) or sequence != monitored_sequence))
                    if should_transfer:
                        latest = await asyncio.to_thread(owner._assistant_client.fetch_frame, 0)
                        frame_transfers += 1
                        if latest is not None:
                            monitored_sequence = getattr(latest, "frame_seq", monitored_sequence)
                except RuntimeError:
                    latest = None
            if helper and stamp is not None and latest is None and not healthy_wait:
                changed = True
                stop.set()
                trace_refresh(owner, "analysis_rejected", reason="capture_unavailable_during_analysis", scope=scope)
                await owner._invalidate_current_observation("capture_unavailable_during_analysis")
                break
            if latest is not None:
                latest_stamp = await asyncio.to_thread(frame_stamp, latest.image_bytes)
                if not scene_only and not controls and frame_changed(stamp, latest_stamp, coarse_regions, ui_only=exploration_ui):
                    changed = True
                    stop.set()
                    trace_refresh(owner, "analysis_rejected", reason="page_changed_during_analysis", scope=scope)
                    await invalidate_frame_change(owner, stamp, latest_stamp, controls,
                                                  "content_or_page_changed_during_analysis" if controls else "page_changed_during_analysis")
                    # Do not start another OCR worker while this one still owns
                    # the shared backend. It exits between individual OCR calls.
                    break
            while not previews.empty():
                preview = previews.get_nowait()
                publisher = getattr(owner, "_publish_scene_preview", None)
                if callable(publisher):
                    await publisher(preview)
                    # Early scenes work on both helper and compatibility paths.
                    epoch = getattr(owner, "_observation_epoch", 0)
                    owner._preview_frame_stamp = stamp
            if helper and not scene_only and page_check is None and pass_preview and not task.done():
                from .page_handoff import queue_page_check
                page_check = queue_page_check(owner, payload, pass_preview, stop, frame_seq=sequence)
                if page_check is not None:
                    trace_refresh(owner, "page_check_queued", scope=scope)
            if page_check is not None and page_check.done():
                try:
                    transition = page_check.result()
                except Exception:
                    transition = None  # Failed classification never proves a switch.
                    trace_refresh(owner, "page_check_result", reason="probe_failed", scope=scope)
                page_check = None
                if transition is not None:
                    from .page_handoff import result_current
                    current = result_current(owner, transition)
                    reason = "request_obsolete" if transition["reason"] == "confirmed_transition" and not current else transition["reason"]
                    trace_refresh(owner, "page_check_result", reason=reason, scope=scope,
                                  attempt=transition["attempt"], probe_seconds=transition["probe_seconds"],
                                  native_calls=transition.get("native_calls"), native_seconds=transition.get("native_seconds"),
                                  baseline_updated=transition.get("baseline_updated", False))
                    if reason != "confirmed_transition":
                        continue
                    stop.set()
                    changed = True
                    owner._analysis_frame_stamp = transition["stamp"]
                    publisher = getattr(owner, "_publish_scene_preview", None)
                    if callable(publisher):
                        await publisher(transition["preview"])
                        owner._preview_frame_stamp = transition["stamp"]
                    trace_refresh(owner, "page_handoff", from_page=transition["from_page"],
                                  to_page=transition["to_page"], scope=scope)
                    break
            if done:
                break
        try:
            analysis = await asyncio.shield(task)
        except ObservationSuperseded:
            trace_refresh(owner, "old_worker_drained", scope=scope)
            return None
        trace_refresh(owner, "analysis_finished", scope=scope,
                      processing_seconds=(analysis.get("recognition") or {}).get("processing_seconds") if isinstance(analysis, dict) else None,
                      health_polls=health_polls, frame_transfers=frame_transfers)
        if changed or epoch != getattr(owner, "_observation_epoch", 0):
            trace_refresh(owner, "analysis_discarded", scope=scope, epoch_changed=epoch != getattr(owner, "_observation_epoch", 0))
            return None
        live_validation = None
        delivery_guard = None
        if helper and stamp is not None and not changed:
            validation_generation = getattr(owner, "_query_refresh_generation", 0)
            latest = await fetch_recent_frame(owner)
            if (latest is None or not 0 <= time.time() - latest.timestamp <= LIVE_FRAME_MAX_AGE_SECONDS
                    or not latest.game.get("running") or latest.game.get("minimized")
                    or (getattr(owner, "_analysis_game_pid", 0) and latest.game.get("pid") != owner._analysis_game_pid)
                    or latest.capture.get("state") != "capturing" or latest.capture.get("isolated_window") is not True):
                changed = True
                trace_refresh(owner, "analysis_rejected", reason="capture_unavailable_after_analysis", scope=scope)
                await owner._invalidate_current_observation("capture_unavailable_during_analysis")
            else:
                if not getattr(owner, "_analysis_game_pid", 0):
                    # Compatibility image callers do not go through the
                    # observer's initial capture hook. Bind their first valid
                    # helper process too; later checks remain process-strict.
                    owner._analysis_game_pid = latest.game.get("pid", 0)
                # This is the SAME producer. Keep the task drainable on cancellation
                # so a timed-out reread cannot race a second OCR worker.
                task = asyncio.create_task(asyncio.to_thread(
                    stable_screen_content, analysis, payload, latest.image_bytes,
                    verifier=getattr(owner._vision, "verify_text_regions", None), cancelled=stop.is_set))
                try:
                    analysis = await asyncio.shield(task)
                except ObservationSuperseded:
                    return None
                from .page_control_trace import record_controls
                record_controls(owner, "validated", analysis=analysis)
                if epoch != getattr(owner, "_observation_epoch", 0):
                    return None
                # Retained fields now have proof on this validation frame. Do not
                # rebuild their guard from the older, background-mismatched image.
                guard_payload = latest.image_bytes
                # Native glyph checks protect literal content. Keep established
                # page/actor change guards separate: a cache-miss threshold for
                # tiny text pixels must not become a new whole-page detector.
                delivery_regions = fact_regions(analysis)
                if (getattr(owner, "_content_query_waiters", 0) > 0
                        or getattr(getattr(owner, "_pending_answer", None), "content", False)):
                    delivery_regions.extend({"region": item["region"]}
                        for item in (analysis.get("screen_facts") or {}).get("visible_text") or [] if item.get("region"))
                delivery_regions = [item for item in delivery_regions if item.get("region")]
                delivery_guard = (delivery_regions, await asyncio.to_thread(region_stamp, guard_payload, delivery_regions)) if delivery_regions else None
                current_controls = (analysis.get("recognition") or {}).get("scene_regions") or []
                changed = frame_changed(stamp, await asyncio.to_thread(frame_stamp, latest.image_bytes),
                                        current_controls if current_controls else analysis.get("characters") or [],
                                        ui_only=scene_only or bool(current_controls) or exploration_ui)
                if current_controls:
                    # The producer has just checked the literal controls on
                    # this frame. Do not override it with coarse pixels again.
                    changed = not (analysis.get("recognition") or {}).get("page_controls_revalidated", False)
                coarse_changed = changed
                native_changed = False
                native_changed_indices = []
                if delivery_guard:
                    latest_patches = await asyncio.to_thread(region_stamp, latest.image_bytes, delivery_regions)
                    native_changed = regions_changed(delivery_guard[1], latest_patches)
                    native_changed_indices = [index for index, (before, after) in enumerate(zip(delivery_guard[1], latest_patches))
                                              if regions_changed((before,), (after,))]
                    changed |= native_changed
                if changed:
                    trace_refresh(owner, "analysis_rejected", reason="page_changed_after_analysis", scope=scope,
                                  native_regions_changed=native_changed, coarse_changed=coarse_changed,
                                  page_region_count=len(current_controls), guarded_region_count=len(delivery_regions),
                                  native_changed_indices=native_changed_indices,
                                  changed_region_metadata=[{"index": index, "region": delivery_regions[index].get("region"),
                                                            "identity_scope": delivery_regions[index].get("identity_scope")}
                                                           for index in native_changed_indices[:8]],
                                  character_region_count=len(analysis.get("characters") or []),
                                  fact_region_count=len(fact_regions(analysis)))
                    await invalidate_frame_change(owner, stamp, await asyncio.to_thread(frame_stamp, latest.image_bytes),
                                                  current_controls, "unconfirmed_page_after_analysis")
                elif validation_generation == getattr(owner, "_query_refresh_generation", 0):
                    live_validation = {"query_generation": validation_generation, "epoch": epoch,
                                       "checked_at": datetime.now(timezone.utc).isoformat(),
                                       "checked_frame_at": latest.timestamp,
                                       "answer_generation": getattr(owner, "_answer_query_generation", 0)}
        if changed or epoch != getattr(owner, "_observation_epoch", 0):
            trace_refresh(owner, "analysis_discarded", scope=scope, epoch_changed=epoch != getattr(owner, "_observation_epoch", 0))
            return None
        trace_refresh(owner, "capture_validated", scope=scope)
        # All retained literals and guards now describe guard_payload. Using
        # the original capture here can immediately invalidate a just-verified
        # result on the next observer tick despite identical current text.
        guard_payload = latest.image_bytes if helper and latest is not None else payload
        owner._next_frame_stamp = await asyncio.to_thread(frame_stamp, guard_payload)
        owner._next_frame_scene_only = scene_only
        owner._next_frame_scene_regions = [*((analysis.get("recognition") or {}).get("scene_regions") or []), *fact_regions(analysis)]
        owner._next_frame_page_regions = (analysis.get("recognition") or {}).get("scene_regions") or []
        from .page_control_trace import record_controls
        record_controls(owner, "staged", analysis=analysis, controls=owner._next_frame_page_regions)
        owner._next_frame_delivery_guard = delivery_guard
        query_regions = owner._next_frame_scene_regions
        owner._next_frame_query_guard = (query_regions, await asyncio.to_thread(region_stamp, guard_payload, query_regions)) if query_regions else None
        scene_regions = [*((analysis.get("recognition") or {}).get("scene_regions") or []),
                         *fact_regions({"screen_facts": {"facts": [f for f in (analysis.get("screen_facts") or {}).get("facts") or []
                                                                   if f.get("kind") == "current_location"]}})]
        owner._next_frame_query_scene_guard = (scene_regions, await asyncio.to_thread(region_stamp, guard_payload, scene_regions)) if scene_regions else None
        # Reuse of a full result is allowed only when ALL exposed literal rows,
        # facts and supported identity boxes fit in a complete native guard.
        # Never rely on region_stamp's 48-region truncation for completeness.
        actors = analysis.get("characters") or []
        owner._next_frame_identity_guarded = bool(actors and all(item.get("region") for item in actors))
        if owner._next_frame_identity_guarded and not helper:
            # Helper results already admitted actors against guard_payload in
            # stable_screen_content. Requiring unchanged original pixels here
            # would undo that exact-text proof on animated backgrounds.
            before_actors = await asyncio.to_thread(region_stamp, payload, actors)
            after_actors = await asyncio.to_thread(region_stamp, guard_payload, actors)
            owner._next_frame_identity_guarded = (len(before_actors) == len(actors)
                and not regions_changed(before_actors, after_actors))
        delivery_items = [*owner._next_frame_page_regions, *(delivery_guard[0] if delivery_guard else []),
                          *[(identity_literal(a) or a) for a in actors]]
        delivery_items = list({tuple(i["region"].get(k, -1) for k in ("left", "top", "right", "bottom")): i
                               for i in delivery_items if i.get("region")}.values())
        if delivery_items and len(delivery_items) <= 48:
            owner._next_frame_delivery_guard = (delivery_items, await asyncio.to_thread(region_stamp, guard_payload, delivery_items))
        full_regions = [*query_regions, *[item for item in actors if item.get("region")],
                        *[item for item in (analysis.get("screen_facts") or {}).get("visible_text") or [] if item.get("region")]]
        full_regions = list({tuple(item["region"].get(k, -1) for k in ("left", "top", "right", "bottom")): item
                             for item in full_regions if item.get("region")}.values())
        all_rows_guarded = all(item.get("region") for item in (analysis.get("screen_facts") or {}).get("visible_text") or [])
        from .query_reuse import complete_fact_guard
        all_facts_guarded = complete_fact_guard(analysis)
        full_patches = await asyncio.to_thread(region_stamp, guard_payload, full_regions) if full_regions and len(full_regions) <= 48 else ()
        owner._next_frame_full_query_guard = ((full_regions, full_patches)
            if not scene_only and all_rows_guarded and all_facts_guarded and len(full_patches) == len(full_regions) and full_patches else None)
        trace_refresh(owner, "query_guards_built", scope=scope)
        identity_validation = (analysis.get("recognition") or {}).get("identity_validation")
        if identity_validation:
            trace_refresh(owner, "identity_validation", **identity_validation)
        trace_refresh(owner, "analysis_validated", scope=scope)
        if helper and latest is not None:
            # Candidate admission and per-question answer permission have
            # different lifetimes. Keep the proved literal/guard packet even
            # when native review took >2s or another question arrived. The
            # consumer's fresh-frame receipt is still independently checked.
            analysis["_candidate_validation"] = {
                "epoch": epoch, "game_pid": latest.game.get("pid"),
                "checked_frame_at": latest.timestamp,
            }
        if live_validation is not None:
            analysis["_live_validation"] = live_validation
        return analysis
    finally:
        stop.set()
        if page_check is not None:
            page_check.cancel()
        if not task.done():
            # Cancellation does not terminate to_thread. Retain the worker so
            # lifecycle shutdown can drain it rather than reuse its backend.
            owner._draining_vision_worker = task


async def drain_analysis_worker(owner: Any) -> None:
    worker = getattr(owner, "_draining_vision_worker", None)
    try:
        if worker is not None:
            await asyncio.shield(worker)
    except ObservationSuperseded:
        pass  # Expected cooperative shutdown, not a failed plugin stop.
    finally:
        owner._draining_vision_worker = None


async def watch_latest_frame(owner: Any, payload: bytes, *, helper: bool) -> bool:
    """Check changes between OCR passes too, without starting another producer."""
    revision = (getattr(owner, "_observation_epoch", 0), getattr(owner, "_observation_serial", 0))
    stamp = await asyncio.to_thread(frame_stamp, payload)
    if revision != (getattr(owner, "_observation_epoch", 0), getattr(owner, "_observation_serial", 0)):
        return False  # The payload predates a newer commit/withdrawal.
    controls = getattr(owner, "_accepted_frame_page_regions", getattr(owner, "_accepted_frame_scene_regions", ()))
    guard = getattr(owner, "_accepted_frame_delivery_guard", None)
    from .delivery_scope import coordinates
    native_page_guard = bool(controls and guard and {coordinates(p) for p in controls} <= {p[0] for p in guard[1]})
    regions = [*controls, *getattr(owner, "_accepted_frame_characters", ())]
    exploration_ui = (bool(controls) and getattr(owner, "_accepted_frame_page_type", "unknown") != "unknown"
                      or bool(regions) and getattr(owner, "_accepted_frame_page_type", "") == "exploration_hud")
    if not native_page_guard and frame_changed(
        getattr(owner, "_accepted_frame_stamp", None), stamp,
        controls if controls and exploration_ui else regions, ui_only=exploration_ui,
    ):
        await invalidate_frame_change(owner, getattr(owner, "_accepted_frame_stamp", None), stamp, controls,
                                      "content_or_page_changed_between_analysis")
        return True
    if controls and guard:
        patches = await asyncio.to_thread(region_stamp, payload, guard[0])
        if revision != (getattr(owner, "_observation_epoch", 0), getattr(owner, "_observation_serial", 0)):
            return False
        changed = {p[0] for i, p in enumerate(guard[1])
                   if i >= len(patches) or regions_changed((p,), (patches[i],))}
        checked_at = getattr(owner, "_watch_frame_timestamp", None)
        if helper and isinstance(checked_at, (int, float)) and 0 <= time.time() - checked_at <= LIVE_FRAME_MAX_AGE_SECONDS:
            from .fact_lifecycle import record_verified_regions
            from .observation_store import ObservationStore
            unchanged = {p[0] for p in guard[1]} - changed
            record_verified_regions(owner, unchanged, checked_at, serial=revision[1])
            ObservationStore(owner).clear_deferred(unchanged)
            if not changed and getattr(owner, "_observation_pending_reason", "").startswith((
                    "capture_unavailable", "capture_error", "verification_pending")):
                # A truly new frame checked ALL admitted dependencies. This
                # is recovery evidence, not a timeout changing validity.
                owner._observation_pending_reason = ""
                owner._observer_status["state"] = "observing"
        if changed:
            from .observation_store import ObservationStore
            if (helper and isinstance(checked_at, (int, float)) and 0 <= time.time() - checked_at <= LIVE_FRAME_MAX_AGE_SECONDS
                    and getattr(owner, "_vision_schedule_role", "idle") != "producer"):
                # Between producer passes, try literal review before marking
                # background animation as lost page/values. Same reviewer and
                # same serial OCR backend, never a second full recognizer.
                from .review_policy import review_current
                checked = await review_current(owner, allow_partial=False)
                if checked is not None:
                    publisher = getattr(owner, "_push_current_visual_context", None)
                    if callable(publisher):
                        publisher(await owner._current_visual_evidence())
                    return False
            # Name/value pixel changes prompt a new read, not a page switch.
            # Quarantine their dependents until the producer/consumer proves
            # them again; never continue publishing an unverified old identity.
            ObservationStore(owner).defer_regions(changed)
            trace_refresh(owner, "regions_deferred", region_count=len(changed))
            publisher = getattr(owner, "_push_current_visual_context", None)
            if callable(publisher):
                publisher(await owner._current_visual_evidence())
            return True
    return False


async def validate_before_delivery(owner: Any, *, plan=None, selection=None) -> bool:
    async with delivery_turn(owner):
        result = await _validate_before_delivery(owner, plan=plan, selection=selection)
        if result:
            mark_delivery_verified(owner)
        return result


async def _validate_before_delivery(owner: Any, *, plan=None, selection=None) -> bool:
    """Compatibility bool adapter; live committed data uses one review policy."""
    client = getattr(owner, "_assistant_client", None)
    preview = getattr(owner, "_scene_preview", None)
    stamp = getattr(owner, "_preview_frame_stamp", None) if preview else getattr(owner, "_accepted_frame_stamp", None)
    if client is not None and getattr(owner, "_observation_invalidated", False):
        return False
    if client is not None and not preview and getattr(owner, "_committed_observation", None):
        from .review_policy import review_current
        checked = await review_current(owner, plan=plan, allow_partial=selection is not None)
        if checked is not None and selection is not None:
            selection.append(checked)
        return checked is not None
    if client is None or stamp is None:
        return True  # Manual images have no live helper contract.
    if not owner._observer_status.get("enabled") or getattr(owner, "_observer_stop_requested", False):
        return False
    revision = (getattr(owner, "_observation_epoch", 0), getattr(owner, "_observation_serial", 0))
    latest = await fetch_recent_frame(owner)
    if revision != (getattr(owner, "_observation_epoch", 0), getattr(owner, "_observation_serial", 0)):
        return False
    if latest is None:
        await owner._invalidate_current_observation("capture_unavailable_before_delivery")
        return False
    if (not 0 <= time.time() - latest.timestamp <= LIVE_FRAME_MAX_AGE_SECONDS
            or getattr(owner, "_analysis_game_pid", 0) and latest.game.get("pid") != owner._analysis_game_pid
            or not latest.game.get("running") or latest.game.get("minimized")
            or latest.capture.get("state") != "capturing" or latest.capture.get("isolated_window") is not True):
        await owner._invalidate_current_observation("capture_unavailable_before_delivery")
        return False
    controls = ((preview or {}).get("recognition") or {}).get("scene_regions") or getattr(owner, "_accepted_frame_page_regions", ())
    current = await asyncio.to_thread(frame_stamp, latest.image_bytes)
    if revision != (getattr(owner, "_observation_epoch", 0), getattr(owner, "_observation_serial", 0)):
        return False
    if frame_changed(stamp, current, controls, ui_only=bool(controls)):
        await invalidate_frame_change(owner, stamp, current, controls, "content_or_page_changed_before_delivery")
        return False
    # Early previews lack a complete native candidate guard. They may expose
    # the independently recognized page only, never certify committed content.
    return True
async def revalidate_guard_text(owner, latest, coordinates, *, evidence=None, controls=(), reference_guard=None):
    """One bounded verification slot, sharing the adapter's serial OCR gate."""
    if getattr(owner, "_vision_schedule_role", "idle") == "producer":
        # The adapter's producer executes this review at its next native-call
        # boundary. Do not acquire its whole-pass async gate or start a second
        # inference thread; unsupported/finishing producers still return busy.
        return await _revalidate_guard_text(owner, latest, coordinates, evidence=evidence,
                                            controls=controls, reference_guard=reference_guard, cooperative=True)
    from .vision_scheduler import vision_turn
    async with vision_turn(owner, "delivery"):
        return await _revalidate_guard_text(owner, latest, coordinates, evidence=evidence, controls=controls,
                                            reference_guard=reference_guard)


async def _revalidate_guard_text(owner, latest, coordinates, *, evidence=None, controls=(), reference_guard=None,
                                 cooperative=False):
    reader = getattr(getattr(owner, "_vision", None), "verify_text_regions", None)
    current = evidence if evidence is not None else getattr(owner, "_committed_observation", None)
    def result(state, admitted=frozenset(), capture=None):
        owner._last_delivery_verification = {"state": state}
        trace_refresh(owner, "text_verification", reason=state, region_count=len(coordinates))
        return TextVerification(state, frozenset(admitted), capture)
    if not callable(reader) or not current:
        return result("unavailable")
    screen = current.get("screen_facts") or {}
    proofs = [p for f in screen.get("facts") or [] for p in f.get("evidence") or []
              if p.get("validation_scope") != "control_presence"]
    proofs.extend(screen.get("visible_text") or [])
    proofs.extend(p for p in controls if p.get("control") is True and p.get("text"))
    proofs.extend(p for p in controls if p.get("identity_literal") is True and p.get("text"))
    available = list({proof_key(p): p for p in proofs}.values())
    selected = [p for p in available if proof_key(p)[0] in coordinates]
    if not selected or {proof_key(p)[0] for p in selected} != coordinates:
        return result("missing_proof")
    queued_coordinates = frozenset(coordinates)
    epoch, serial = getattr(owner, "_observation_epoch", 0), getattr(owner, "_observation_serial", 0)
    # A refresh attempt may advance inside the SAME question. That does not
    # cancel its in-flight review. A new question/page/capture failure does.
    generation = getattr(owner, "_answer_query_generation", 0)
    capture_revision = getattr(owner, "_capture_failure_revision", 0)
    identity = (epoch, serial, generation, capture_revision, frozenset(proof_key(p) for p in selected))
    job = getattr(owner, "_content_revalidation_job", None)
    # The immutable fact/question/capture boundary owns this native job, not
    # the set of background-challenged pixels chosen by the latest caller.
    # That set can grow/shrink while the SAME job waits for a checkpoint.
    if job is not None and job["identity"][:4] != identity[:4]:
        job["stop"].set()
        if job["queued"] is not None:
            job["queued"].cancel()
        if not job["task"].done():
            return result("busy")  # Drain the native call; never overlap it.
        owner._content_revalidation_job = job = None
    if job is not None:
        task = job["task"]
        if job["identity"][4] != identity[4]:
            trace_refresh(owner, "verification_scope_changed", queued_regions=len(job["queued_coordinates"]),
                          requested_regions=len(coordinates))
        trace_refresh(owner, "verification_resumed", completed=task.done(), region_count=len(coordinates))
        return await _receive_guard_review(owner, job, result, expected_proofs=available)
    worker = getattr(owner, "_content_revalidation_worker", None)
    if worker is not None and not worker.done():
        return result("busy")
    stop = threading.Event()
    queued = None
    def read_at_checkpoint():
        nonlocal selected, coordinates
        # A queued screenshot can become old before admission. Acquire a new
        # atomic helper frame in the producer thread, preserving its timestamp.
        def check_current():
            return (stop.is_set() or (epoch, serial, generation, capture_revision) != (
                getattr(owner, "_observation_epoch", 0), getattr(owner, "_observation_serial", 0),
                getattr(owner, "_answer_query_generation", 0), getattr(owner, "_capture_failure_revision", 0))
                or getattr(owner, "_observer_stop_requested", False)
                or not owner._observer_status.get("enabled"))
        if check_current():
            raise ObservationSuperseded()
        capture = owner._assistant_client.fetch_frame(0)
        if (capture is None or not 0 <= time.time() - capture.timestamp <= LIVE_FRAME_MAX_AGE_SECONDS
                or capture.game.get("pid") != latest.game.get("pid") or not capture.game.get("running")
                or capture.game.get("minimized") or capture.capture.get("state") != "capturing"
                or capture.capture.get("isolated_window") is not True):
            return None, None
        if reference_guard is not None:
            # A new checkpoint frame needs a NEW dependency selection too.
            # Queue-time coordinates may no longer include all changed fields.
            # Still use this snapshot's original patches/text, never the next
            # producer's values or permission to approve unselected literals.
            from .query_reuse import region_stamp, regions_changed
            regions, original_patches = reference_guard
            actual_patches = region_stamp(capture.image_bytes, regions)
            if len(original_patches) != len(actual_patches):
                raise ValueError("checkpoint_guard_coverage_missing")
            coordinates = {a[0] for a, b in zip(original_patches, actual_patches)
                           if regions_changed((a,), (b,))}
            selected = [p for p in available if proof_key(p)[0] in coordinates]
            if {proof_key(p)[0] for p in selected} != coordinates:
                raise ValueError("checkpoint_literal_coverage_missing")
        if not selected:
            return set(), capture  # All original scoped patches match THIS frame.
        return reader(capture.image_bytes, selected, cancelled=check_current), capture
    if cooperative:
        submit = getattr(owner._vision, "enqueue_review", None)
        queued = submit(read_at_checkpoint) if callable(submit) else None
        if queued is None:
            return result("busy")
        task = asyncio.wrap_future(queued)
        trace_refresh(owner, "verification_queued", region_count=len(coordinates))
    else:
        def read_direct():
            def cancelled():
                return (stop.is_set() or (epoch, serial, generation, capture_revision) != (
                    getattr(owner, "_observation_epoch", 0), getattr(owner, "_observation_serial", 0),
                    getattr(owner, "_answer_query_generation", 0), getattr(owner, "_capture_failure_revision", 0))
                    or getattr(owner, "_observer_stop_requested", False)
                    or not owner._observer_status.get("enabled"))
            return reader(latest.image_bytes, selected, cancelled=cancelled), latest
        task = asyncio.create_task(asyncio.to_thread(read_direct))
    def finished(value):
        if not value.cancelled():
            value.exception()  # Consume a cooperative late exit after caller timeout.
    task.add_done_callback(finished)
    owner._content_revalidation_worker = task
    # Retain only one bounded native job. Its original capture/proofs, rather
    # than a later caller's image, bind a late result. Nothing is persisted.
    job = {"identity": identity, "task": task, "stop": stop, "queued": queued,
           "proofs": lambda: selected, "coordinates": lambda: coordinates,
           "queued_coordinates": queued_coordinates, "cooperative": cooperative,
           "reference_guard": reference_guard}
    owner._content_revalidation_job = job
    return await _receive_guard_review(owner, job, result, expected_proofs=available)


async def _receive_guard_review(owner, job, result, *, expected_proofs):
    """A two-second wait slice is not permission to abandon valid native work.

    Query/followup deadlines are unchanged. The caller still rechecks fresh
    pixels and object guards before using this frame-bound literal receipt.
    """
    task = job["task"]
    epoch, serial, generation, capture_revision, _ = job["identity"]
    retain = False
    try:
        confirmed, capture = await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
        if capture is None:
            return result("unavailable")
        selected, coordinates = job["proofs"](), job["coordinates"]()
        if job["cooperative"] and job["reference_guard"] is not None:
            trace_refresh(owner, "checkpoint_proofs_selected", queued_regions=len(job["queued_coordinates"]),
                          execution_regions=len(coordinates), selected_proofs=len(selected))
        if ((epoch, serial, generation, capture_revision) != (getattr(owner, "_observation_epoch", 0),
                        getattr(owner, "_observation_serial", 0), getattr(owner, "_answer_query_generation", 0),
                        getattr(owner, "_capture_failure_revision", 0))
                or getattr(owner, "_observer_stop_requested", False)):
            return result("superseded")
        # Literal equality describes THIS frame. The shared admission caller
        # separately checks delivery freshness and can carry it to a newer
        # pixel-identical scoped frame; elapsed inference time is not mismatch.
        # A resumed caller can request a different subset. Consume the ORIGINAL
        # frame/literal receipt but authorize only exact expectations still in
        # that caller's evidence. Coordinates alone cannot prove a new number,
        # another proof role, or an added field the old job never checked.
        expected = {}
        for proof in expected_proofs:
            key = proof_key(proof)
            if key[0] in coordinates:
                expected.setdefault(key[0], set()).add(key)
        admitted = {coords for coords, keys in expected.items() if keys <= confirmed}
        return result("confirmed" if coordinates <= admitted else "unconfirmed", admitted, capture)
    except TimeoutError:
        retain = True
        trace_refresh(owner, "verification_continued", region_count=len(job["queued_coordinates"]))
        return result("busy")  # Pending, not a literal mismatch and not cancellation.
    except ObservationSuperseded:
        return result("busy")
    except asyncio.CancelledError:
        if asyncio.current_task().cancelling():
            raise
        return result("busy")  # Producer exited before this queued job started.
    except (RuntimeError, ValueError, OSError):
        return result("error")
    finally:
        if task.done() and not retain:
            job["stop"].set()
            if getattr(owner, "_content_revalidation_job", None) is job:
                owner._content_revalidation_job = None
            if getattr(owner, "_content_revalidation_worker", None) is task:
                owner._content_revalidation_worker = None


def cancel_guard_review(owner):
    """Retire frame-bound proof work without freeing a running native gate."""
    job = getattr(owner, "_content_revalidation_job", None)
    if job is not None:
        job["stop"].set()
        if job["queued"] is not None:
            job["queued"].cancel()
        owner._content_revalidation_job = None


def query_pending(owner: Any, analysis: dict[str, Any]) -> bool:
    requested = getattr(owner, "_query_refresh_after", 0.0)
    if not requested:
        return False
    if int(analysis.get("observation_serial") or 0) <= getattr(owner, "_query_refresh_serial", -1):
        return True
    # Only a newly committed result checked against live pixels for THIS query
    # may cross the capture-time gate. Old cache/preview results cannot do so.
    if getattr(owner, "_query_validated_read", None) == (
        getattr(owner, "_query_refresh_generation", 0), getattr(owner, "_observation_epoch", 0),
        int(analysis.get("observation_serial") or 0),
    ):
        return False
    try:
        return (
            datetime.fromisoformat(str(analysis.get("observed_at") or "").replace("Z", "+00:00")).timestamp()
            < requested
        )
    except ValueError:
        return True


async def validated_delivery(owner: Any, evidence: dict, *, plan=None) -> dict | None:
    """Select one checked result; do not reread mutable state at handoff."""
    from .review_policy import producer_receipt
    produced = producer_receipt(owner, evidence, plan=plan)
    if produced is not None:
        mark_delivery_verified(owner)
        return produced
    if not evidence.get("fresh"):
        raw = getattr(owner, "_committed_observation", None) or {}
        # A live current projection may have aged out or be awaiting capture.
        # Re-select its raw candidate through the SAME fresh-frame review;
        # never revive a receipt, an old object, preview or disk history.
        if (plan is not None and not evidence.get("reuse")
                and getattr(owner, "_assistant_client", None) is not None
                and raw and not getattr(owner, "_scene_preview", None)
                and evidence.get("observed_at") == raw.get("observed_at")
                and (evidence.get("screen_facts") or {}).get("observation_serial") == raw.get("observation_serial")):
            from .review_policy import review_current
            return await review_current(owner, plan=plan)
        return None
    if getattr(owner, "_assistant_client", None) is None and query_pending(owner, {
        "observation_serial": (evidence.get("screen_facts") or {}).get("observation_serial"),
        "observed_at": evidence.get("observed_at"),
    }):
        return None  # No live helper exists to authorize a pre-question sample.
    from .review_policy import checked_receipt, observable
    if getattr(owner, "_assistant_client", None) is not None and not observable(owner):
        return None
    if evidence.get("reuse"):
        # Do not recheck against a different/current OCR result. A revoked or
        # expired receipt must select anew, not quietly certify the old packet.
        if not checked_receipt(owner, evidence, plan=plan):
            return None
        from .delivery_scope import project_evidence
        return project_evidence(evidence, plan)
    epoch = getattr(owner, "_observation_epoch", 0)
    serial = (evidence.get("screen_facts") or {}).get("observation_serial")
    if serial and serial != getattr(owner, "_observation_serial", 0):
        return None
    selection = []
    if not await validate_before_delivery(owner, **({"plan": plan, "selection": selection} if plan is not None else {})):
        return None
    if epoch != getattr(owner, "_observation_epoch", 0) or getattr(owner, "_observation_invalidated", False):
        return None
    if serial and serial != getattr(owner, "_observation_serial", 0):
        return None
    from .delivery_scope import project_evidence
    selected = selection[0] if selection else project_evidence(evidence, plan)
    selected["handoff"] = {"epoch": epoch}
    return selected


async def refresh_for_query(owner: Any, *, timeout: float = 20.0, require_characters: bool = True,
                            require_content: bool = False, require_navigation: bool = False, query_plan=None) -> dict | None:
    """Validate a scoped snapshot first; otherwise wait for fresh single-worker OCR."""
    owner._last_refresh_diagnostics = {"outcome": "observer_unavailable", "stages": []}
    observer = getattr(owner, "_observer_task", None)
    if observer is None or observer.done() or not owner._observer_status.get("enabled"):
        return
    owner._last_visual_query_at = time.monotonic()
    trace = {"started": time.monotonic(), "stages": []}
    deadline = trace["started"] + timeout
    owner._active_refresh_trace = trace
    trace_refresh(owner, "query_started", require_characters=require_characters,
                  require_content=require_content, require_navigation=require_navigation)
    completed = False
    completion_state = "ready"
    page_first = bool(query_plan is not None and "page" in query_plan.fields and len(query_plan.fields) > 1)
    execution_plan = query_plan.with_fields({"page"}) if page_first else query_plan
    reuse_args = {"navigation": require_navigation}
    if query_plan is not None:
        reuse_args["plan"] = query_plan
    if require_characters or require_content:
        reuse_args.update(full=True, require_characters=require_characters and not require_content)
    original_reuse_args = dict(reuse_args)
    if page_first:
        # Admission must happen before the large text batch, not after it
        # times out. Use the SAME reviewer/native worker, scoped to controls.
        reuse_args = {"navigation": False, "full": False, "plan": execution_plan}
        require_characters = require_content = require_navigation = False
        trace_refresh(owner, "page_first_scheduled", remaining_fields=sorted(query_plan.fields - {"page"}))

    async def reuse_selected():
        result = await try_reuse(owner, **reuse_args)
        if page_first and result is not None:
            # Preserve already-ready complete results without queuing native
            # OCR for unrelated text. A miss cannot erase the checked page.
            richer = None
            if deadline - time.monotonic() > .4:
                try:
                    richer = await asyncio.wait_for(try_reuse(owner, **original_reuse_args, pixel_only=True), timeout=.35)
                except TimeoutError:
                    pass  # Optional enrichment cannot erase the admitted page.
            if richer is not None:
                return richer
        return result
    def ready(value):
        nonlocal completion_state
        if value is None:
            return False
        if query_plan is None:
            return True  # Compatibility callers retain their existing partial semantics.
        from .query_coordinator import has_answerable_content, satisfied_fields
        enough = query_plan.fields <= satisfied_fields(query_plan, value)
        completion_state = "ready" if enough else "partial"
        if not enough:
            trace_refresh(owner, "answer_incomplete", missing_fields=sorted(query_plan.fields - satisfied_fields(query_plan, value)))
        return enough or has_answerable_content(query_plan, value)
    try:
        reused = await asyncio.wait_for(reuse_selected(),
            timeout=max(.001, deadline - time.monotonic()))
    except TimeoutError:
        reused = None
    except asyncio.CancelledError:
        if getattr(owner, "_active_refresh_trace", None) is trace:
            owner._active_refresh_trace = None
        owner._last_refresh_diagnostics = {"outcome": "cancelled", "stages": list(trace["stages"])}
        raise
    if ready(reused):
        outcome = "reused" if completion_state == "ready" else "partial"
        trace_refresh(owner, "query_finished", outcome=outcome)
        owner._last_refresh_diagnostics = {
            "outcome": outcome, "wait_seconds": round(time.monotonic() - trace["started"], 3),
            "stages": list(trace["stages"]),
        }
        if getattr(owner, "_active_refresh_trace", None) is trace:
            owner._active_refresh_trace = None
        return reused
    owner._visual_query_waiters = getattr(owner, "_visual_query_waiters", 0) + 1
    if not require_characters:
        owner._scene_query_waiters = getattr(owner, "_scene_query_waiters", 0) + 1
    if require_navigation:
        owner._navigation_query_waiters = getattr(owner, "_navigation_query_waiters", 0) + 1
    if require_content:
        owner._content_query_waiters = getattr(owner, "_content_query_waiters", 0) + 1
    # Observation ISO timestamps have microsecond precision. On Windows,
    # time.time() can have a fractional microsecond beyond datetime.now(),
    # making a same-tick NEW result look older and wait until timeout.
    # Use the same clock/precision as utc_now_iso, not a freshness tolerance.
    owner._query_refresh_after = datetime.now(timezone.utc).timestamp()
    owner._query_refresh_generation = getattr(owner, "_query_refresh_generation", 0) + 1
    owner._query_refresh_deadline = deadline  # Checkpoint work shares, never renews, this budget.
    owner._query_refresh_serial = getattr(owner, "_observation_serial", 0)
    wake = getattr(owner, "_observer_wake_event", None)
    if wake is not None:
        wake.set()
    if not hasattr(owner, "_visual_update_event"):
        owner._visual_update_event = asyncio.Event()
    try:
        owner._push_current_visual_context(await asyncio.wait_for(
            owner._current_visual_evidence(), timeout=max(.001, deadline - time.monotonic())))
        while time.monotonic() < deadline:
            if not owner._observer_status.get("enabled") or getattr(owner, "_observer_stop_requested", False):
                trace["observer_unavailable"] = True
                return None
            owner._visual_update_event.clear()
            evidence = await asyncio.wait_for(owner._current_visual_evidence(), timeout=max(.001, deadline - time.monotonic()))
            if not require_characters and not require_content:
                snapshot = getattr(owner, "_reusable_observations", {}).get("navigation" if require_navigation else "scene")
                if snapshot is None and require_navigation:
                    candidate = getattr(owner, "_reusable_observations", {}).get("scene")
                    if candidate and candidate["evidence"]["page_type"] != "exploration_hud":
                        snapshot = candidate
                serial = ((snapshot or {}).get("evidence", {}).get("screen_facts") or {}).get("observation_serial", 0)
                snapshot_pending = query_pending(owner, {
                    "observation_serial": serial,
                    "observed_at": (snapshot or {}).get("evidence", {}).get("observed_at"),
                })
                if serial > owner._query_refresh_serial and snapshot_pending:
                    # A just-finished read can start before the question and
                    # still be current. Validate its live evidence instead of
                    # throwing it away solely because of its capture time.
                    reused = await asyncio.wait_for(reuse_selected(), timeout=max(.001, deadline - time.monotonic()))
                    if ready(reused):
                        trace_refresh(owner, "completed_read_revalidated", serial=serial)
                        completed = True
                        return reused
                    # try_reuse may revoke the observation while awaiting the
                    # live frame. Never finish with the pre-check evidence.
                    evidence = await asyncio.wait_for(owner._current_visual_evidence(), timeout=max(.001, deadline - time.monotonic()))
            if require_navigation:
                # A title preview has not read the task stack yet. A fresh
                # completed navigation/full pass may return partial/unknown,
                # but never return an older title as the new task answer.
                pending_navigation = query_pending(owner, {
                    "observation_serial": (evidence.get("screen_facts") or {}).get("observation_serial"),
                    "observed_at": evidence.get("observed_at"),
                })
                if not pending_navigation and evidence.get("fresh") and evidence.get("read_scope") in {"navigation", "full"}:
                    selected = await asyncio.wait_for(validated_delivery(owner, evidence, plan=execution_plan), timeout=max(.001, deadline - time.monotonic()))
                    if ready(selected):
                        completed = True
                        return selected
            if require_content and evidence.get("read_scope", "full") == "full" and not query_pending(owner, {
                "observation_serial": (evidence.get("screen_facts") or {}).get("observation_serial"),
                "observed_at": evidence.get("observed_at"),
            }) and evidence.get("fresh") and evidence.get("status") not in {"recognizing_characters", "scene_not_recognized"}:
                selected = await asyncio.wait_for(validated_delivery(owner, evidence, plan=execution_plan), timeout=max(.001, deadline - time.monotonic()))
                if ready(selected):
                    completed = True
                    return selected
            if not require_navigation and not require_characters and (evidence.get("scene") or {}).get("verified"):
                selected = await asyncio.wait_for(validated_delivery(owner, evidence, plan=execution_plan), timeout=max(.001, deadline - time.monotonic()))
                if ready(selected):
                    completed = True
                    return selected
            # Startup has no new result yet. Waiting/expired persisted frames
            # must not finish this fresh query before the observer captures.
            pending = {"updating", "confirming", "invalidated", "missing", "waiting", "expired"}
            if require_characters:
                pending.add("recognizing_characters")
            if (not require_navigation and (not require_characters or evidence.get("read_scope", "full") == "full")
                    and evidence.get("fresh") and evidence.get("status") not in pending):
                selected = await asyncio.wait_for(validated_delivery(owner, evidence, plan=execution_plan), timeout=max(.001, deadline - time.monotonic()))
                if ready(selected):
                    completed = True
                    return selected
            if (getattr(owner, "_reusable_observations", None)
                    and not getattr(owner, "_observation_invalidated", False)):
                # A just-completed result gets its normal producer/delivery
                # path FIRST. Only then recover an older/same-serial candidate
                # whose capture/verification was transiently unavailable.
                # Keep recovery within the original deadline and one worker.
                recovered = await asyncio.wait_for(reuse_selected(),
                    timeout=max(.001, deadline - time.monotonic()))
                if ready(recovered):
                    trace_refresh(owner, "reuse_recovered")
                    completed = True
                    return recovered
            try:
                await asyncio.wait_for(owner._visual_update_event.wait(), timeout=min(1.0, deadline - time.monotonic()))
            except TimeoutError:
                pass
    except TimeoutError:
        return None
    except asyncio.CancelledError:
        trace["cancelled"] = True
        raise
    finally:
        outcome = "observer_unavailable" if trace.get("observer_unavailable") else "cancelled" if trace.get("cancelled") else completion_state if completed else "timeout"
        if getattr(owner, "_active_refresh_trace", None) is trace:
            trace_refresh(owner, "query_finished", outcome=outcome)
        owner._last_refresh_diagnostics = {
            "outcome": outcome,
            "wait_seconds": round(time.monotonic() - trace["started"], 3),
            "stages": list(trace["stages"]),
        }
        if getattr(owner, "_active_refresh_trace", None) is trace:
            owner._active_refresh_trace = None
        owner._visual_query_waiters -= 1
        if not require_characters:
            owner._scene_query_waiters -= 1
        if require_navigation:
            owner._navigation_query_waiters -= 1
        if require_content:
            owner._content_query_waiters -= 1
        owner._last_visual_query_at = time.monotonic()
