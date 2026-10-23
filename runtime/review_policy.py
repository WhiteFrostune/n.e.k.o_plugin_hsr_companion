"""Shared lifecycle and candidate selection for all live answer consumers.

Candidate data is not answer permission. One native review mints a scoped
receipt; downstream consumers perform cheap revocation checks, never more OCR.
"""
from __future__ import annotations

import time
from datetime import datetime


def observable(owner):
    status = getattr(owner, "_observer_status", {})
    return bool(status.get("enabled") and status.get("state") in {"observing", "updating", "error"}
                and not getattr(owner, "_observer_stop_requested", False)
                and not getattr(owner, "_observation_invalidated", False))


def offline_image_context(owner):
    """Manual image integrations have no resident capture to revalidate.

    Never use this for a timed-out live observer or persisted panel history.
    """
    raw = getattr(owner, "_committed_observation", None) or {}
    return bool(raw and getattr(owner, "_assistant_client", None) is None
                and getattr(owner, "_observer_task", None) is None)


def review_status(owner):
    """Allowlisted support state: no pixels, OCR, paths or character names."""
    pending = getattr(owner, "_observation_pending_reason", "")
    deferred = getattr(owner, "_deferred_verification", None)
    return {"policy": "shared_live_review_v1", "has_committed_candidate": bool(getattr(owner, "_committed_observation", None)),
            "has_reusable_candidate": bool(getattr(owner, "_reusable_observations", None)),
            "invalidated": bool(getattr(owner, "_observation_invalidated", False)),
            "epoch": getattr(owner, "_observation_epoch", 0), "revision": getattr(owner, "_observation_serial", 0),
            "pending": "control_verification" if pending == "verification_pending_control" else
                       "capture" if pending.startswith(("capture_error", "capture_unavailable")) else "other" if pending else "none",
            "challenged_regions": len(deferred[2]) if deferred else 0}


def projection_status(owner):
    """Refresh scheduling is not a global data invalidation."""
    status = dict(owner._observer_status)
    pending = getattr(owner, "_observation_pending_reason", "")
    if status.get("enabled") and status.get("state") == "updating" and not pending:
        status["state"] = "observing"
    if pending and status.get("state") == "observing":
        if pending == "verification_pending_control":
            from .delivery_scope import coordinates
            deferred = getattr(owner, "_deferred_verification", None)
            revision = (getattr(owner, "_observation_epoch", 0), getattr(owner, "_observation_serial", 0))
            challenged = deferred[2] if deferred and deferred[:2] == revision else set()
            controls = {coordinates(p) for p in getattr(owner, "_accepted_frame_page_regions", ())}
            if challenged & controls:
                status["state"] = "updating"
        else:
            status["state"] = "updating"
    return status


def checked_receipt(owner, evidence, *, plan=None):
    """A receipt binds data, revision, scope and question, not host speech."""
    from .fact_lifecycle import observation_tick
    from .observation_store import ObservationSnapshot
    receipt = evidence.get("reuse") or {}
    if not observable(owner) or receipt.get("answer_generation") != getattr(owner, "_answer_query_generation", 0):
        return False
    fields = frozenset(plan.fields) if plan is not None else frozenset()
    reviewed = receipt.get("reviewed_fields")
    if reviewed is not None and (plan is None or not fields <= set(reviewed)):
        return False
    try:
        age = observation_tick(time.time()) - datetime.fromisoformat(receipt["validated_at"]).timestamp()
    except (ValueError, TypeError, KeyError):
        return False
    return bool(0 <= age <= 2.0 and ObservationSnapshot.select(owner, evidence, fields=fields).matches(owner))


async def review_current(owner, *, plan=None, allow_partial=True):
    """Adapt the current immutable commit to the SAME cache-review policy."""
    from .fact_lifecycle import candidate_evidence
    from .live_refresh import delivery_turn, mark_delivery_verified
    from .query_reuse import _SUPERSEDED, review_snapshot
    if not observable(owner) or getattr(owner, "_scene_preview", None):
        return None
    raw = getattr(owner, "_committed_observation", None) or {}
    evidence = candidate_evidence(raw)
    if not evidence.get("fresh"):
        return None
    guards = [getattr(owner, name, None) for name in (
        "_accepted_frame_delivery_guard", "_accepted_frame_full_query_guard", "_accepted_frame_query_guard")]
    pairs = {patch[0]: (region, patch) for guard in guards if guard for region, patch in zip(*guard)}
    if plan is None:
        # Withdrawal may leave old patches in broader cache guards. Review
        # only values still exposed by THIS commit, plus its page controls.
        from ..adapters.text_verification import identity_literal
        from .delivery_scope import coordinates
        screen = raw.get("screen_facts") or {}
        wanted = [*getattr(owner, "_accepted_frame_page_regions", ()),
                  *[p for f in screen.get("facts", []) for p in f.get("evidence", []) if p.get("validation_scope") != "control_presence"],
                  *screen.get("visible_text", []), *[(identity_literal(a) or a) for a in raw.get("characters", [])]]
        if any(not p.get("region") or coordinates(p) not in pairs for p in wanted):
            return None
        pairs = {coordinates(p): (p, pairs[coordinates(p)][1]) for p in wanted}
    if not pairs:
        return None
    snapshot = {"evidence": evidence, "guard": ([r for r, _ in pairs.values()], tuple(p for _, p in pairs.values())),
                "stamp": getattr(owner, "_accepted_frame_stamp", None),
                "epoch": getattr(owner, "_observation_epoch", 0),
                "game_pid": getattr(owner, "_analysis_game_pid", 0),
                "page_controls": getattr(owner, "_accepted_frame_page_regions", ()),
                "control_trace": (raw.get("recognition") or {}).get("control_trace") or {},
                "identity_proofs": raw.get("characters", []) if getattr(owner, "_accepted_frame_identity_guarded", False) else []}
    async with delivery_turn(owner):
        result = await review_snapshot(owner, snapshot, navigation=bool(plan and plan.navigation), full=True,
                                       plan=plan, allow_partial=allow_partial)
        if result is _SUPERSEDED:
            return None
        if result is not None:
            mark_delivery_verified(owner)
        return result


def producer_receipt(owner, evidence, *, plan=None):
    """Consume this question's newly committed producer proof, not a new OCR.

    The producer already validated literal data AND controls on its final
    frame. This shortcut is unavailable to older questions, disk history,
    previews, stale frames or dependencies challenged after that commit.
    """
    from .delivery_scope import coordinates, select_scoped_guard
    from .observation_store import ObservationSnapshot
    from .query_reuse import LIVE_FRAME_MAX_AGE_SECONDS
    if (plan is None or not observable(owner) or getattr(owner, "_scene_preview", None)
            or getattr(owner, "_answer_query_generation", 0) <= 0
            or getattr(owner, "_query_refresh_generation", 0) <= 0):
        return None
    raw = getattr(owner, "_committed_observation", None) or {}
    proof = getattr(owner, "_producer_validation", None) or {}
    epoch, serial = getattr(owner, "_observation_epoch", 0), getattr(owner, "_observation_serial", 0)
    if (not evidence.get("fresh") or raw.get("observation_serial") != serial
            or (evidence.get("screen_facts") or {}).get("observation_serial") != serial
            or proof.get("epoch") != epoch
            or proof.get("serial") != serial
            or proof.get("capture_failure_revision") != getattr(owner, "_capture_failure_revision", 0)
            or proof.get("query_generation") != getattr(owner, "_query_refresh_generation", 0)
            or proof.get("answer_generation") != getattr(owner, "_answer_query_generation", 0)
            or not isinstance(proof.get("checked_frame_at"), (int, float))
            or not 0 <= time.time() - proof["checked_frame_at"] <= LIVE_FRAME_MAX_AGE_SECONDS
            or getattr(owner, "_observation_pending_reason", "")):
        return None
    scoped = select_scoped_guard(owner, evidence, plan, [getattr(owner, name, None) for name in (
        "_accepted_frame_delivery_guard", "_accepted_frame_full_query_guard", "_accepted_frame_query_guard")], allow_partial=True)
    if scoped is None:
        return None
    value, guard = scoped
    deferred = getattr(owner, "_deferred_verification", None)
    if deferred and deferred[:2] == (epoch, serial) and deferred[2] & {p[0] for p in guard[1]}:
        return None
    page_dependencies = {coordinates(r) for r in getattr(owner, "_accepted_frame_page_regions", ())}
    value["reuse"] = {"validated_at": datetime.fromtimestamp(proof["checked_frame_at"]).astimezone().isoformat(),
        "epoch": epoch, "validated_revision": serial, "original_serial": serial,
        "answer_generation": getattr(owner, "_answer_query_generation", 0), "reviewed_fields": sorted(plan.fields),
        "reviewed_coordinates": [list(p[0]) for p in guard[1]],
        "page_control_coordinates": [list(p[0]) for p in guard[1] if p[0] in page_dependencies],
        "challenge_revision": getattr(owner, "_review_challenge_serial", 0),
        "capture_failure_revision": getattr(owner, "_capture_failure_revision", 0), "producer_proof": True}
    if not ObservationSnapshot.select(owner, value, fields=plan.fields).matches(owner):
        return None
    from .live_refresh import trace_refresh
    trace_refresh(owner, "producer_proof_consumed", serial=serial)
    return value
