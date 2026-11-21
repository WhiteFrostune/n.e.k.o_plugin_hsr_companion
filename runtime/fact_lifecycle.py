"""Live candidates outlive refresh requests; scoped proofs have their own clock.

Never changes capture/recognition timestamps or promotes persisted history.
This module does not recognize anything or authorize a delivery on its own.
"""
from __future__ import annotations

import math
import time
from collections import Counter
from datetime import datetime, timezone

from .contracts import FRESH_LIVE_AGE_SECONDS, STALE_MAX_AGE_SECONDS
from .observation_evidence import VISUAL_TTL_SECONDS, public_visual_evidence


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


def observation_tick(value):
    """Compare ISO observations/receipts at their serialized precision.

    Raw helper frame clocks retain their full precision and separate bounds.
    This does not add a grace period or rewrite an observation timestamp.
    """
    return datetime.fromtimestamp(value, timezone.utc).timestamp()


def candidate_evidence(raw):
    """Internal unexpired SHAPE for review; not permission to answer or display.

    Only live callers may use this. They still check epoch/process/current
    capture and every returned dependency before issuing a receipt.
    """
    captured = timestamp(raw.get("observed_at"))
    # ISO observations have microsecond precision; time.time() can retain a
    # fractional microsecond on Windows. Compare at the SAME precision, or a
    # same-tick second confirmation can be rejected as a future read and leave
    # the first, still-empty identity cache in place. This is not a time grace:
    # a timestamp one representable microsecond ahead still fails. Original
    # timestamps, fresh-frame proof and object/literal guards remain unchanged.
    current_tick = observation_tick(time.time())
    if captured is None or captured > current_tick:
        return public_visual_evidence({}, {"enabled": True, "state": "observing"})
    value = public_visual_evidence(raw, {"enabled": True, "state": "observing"},
                                   now=datetime.fromtimestamp(captured, timezone.utc))
    # The shape is intentionally unexpired for review; its AGE must still be
    # honest to a caller that reads it as current evidence.
    age = max(0.0, current_tick - captured)
    value["age_seconds"] = round(age, 1)
    value["freshness"] = "stale" if age > FRESH_LIVE_AGE_SECONDS else "fresh"
    if value["freshness"] == "stale":
        value["age_label"] = f"这是 {max(1, int(round(age)))} 秒前的画面，现在可能变了"
        if not str(value.get("message") or "").startswith(value["age_label"]):
            value["message"] = value["age_label"] + "：" + str(value.get("message") or "")
    return value


def late_read_proved(owner, analysis):
    """Admit a validated candidate for storage, NOT a current-answer receipt.

    Native verification can outlive its input frame and a new question can
    arrive during it. Those revoke the fast answer receipt, not the independent
    producer result. Consumers still need the existing fresh, scoped review.
    """
    proof = analysis.get("_candidate_validation") or {}
    checked = proof.get("checked_frame_at")
    return bool(proof and isinstance(checked, (int, float)) and not isinstance(checked, bool)
        and math.isfinite(checked) and 0 <= checked <= time.time()
        and proof.get("epoch") == getattr(owner, "_observation_epoch", 0)
        and proof.get("game_pid") == getattr(owner, "_analysis_game_pid", 0)
        and not getattr(owner, "_observer_stop_requested", False)
        and (analysis.get("scene") or {}).get("verified")
        and getattr(owner, "_next_frame_page_regions", None)
        and getattr(owner, "_next_frame_query_guard", None))


def incomplete_same_page_read(owner, analysis):
    """Missing observations are not replacements or semantic contradictions.

    Keep ONE existing commit and its OWN guards, not a merge of two items.
    Pixel challenges still withhold old values until independently checked.
    Any new/conflicting fact or identity, or real page/lifecycle change, wins.
    """
    old = getattr(owner, "_committed_observation", None) or {}
    recognition = analysis.get("recognition") or {}
    if (not old or getattr(owner, "_assistant_client", None) is None
            or not getattr(owner, "_accepted_frame_delivery_guard", None)
            or getattr(owner, "_observation_invalidated", False)
            or getattr(owner, "_scene_preview", None)
            or recognition.get("scene_only_query") or recognition.get("navigation_only_query")
            or (old.get("page") or {}).get("page_type") in {None, "unknown"}
            or (old.get("page") or {}).get("page_type") != (analysis.get("page") or {}).get("page_type")):
        return False
    from .delivery_boundary import semantic_key

    def facts(value):
        # Use the SAME fact identity as delivery revocation. A flat and a
        # percentage substat may have the same label; a dict keyed by label
        # silently discarded one and could hide real progress/replacement.
        return Counter(semantic_key(f) for f in (value.get("screen_facts") or {}).get("facts", [])
                       if f.get("kind") not in {"unreadable_stat", "equipment_binding_missing"})
    previous, incoming = facts(old), facts(analysis)
    if incoming - previous:
        return False
    from .delivery_scope import identity_key
    old_actors = {identity_key(a) for a in old.get("characters", [])}
    new_actors = {identity_key(a) for a in analysis.get("characters", [])}
    return bool(new_actors <= old_actors and (incoming < previous or new_actors < old_actors))


def record_verified_regions(owner, coordinates, checked_at, *, serial=None):
    """Renew only current-version regions proved on an actually fresh frame."""
    raw = getattr(owner, "_committed_observation", None) or {}
    current = getattr(owner, "_observation_serial", 0)
    if (not raw or serial is not None and serial != current
            or getattr(owner, "_observation_invalidated", False)
            or not isinstance(checked_at, (int, float)) or not 0 <= time.time() - checked_at <= 2.0):
        return
    identity = (getattr(owner, "_observation_epoch", 0), current, raw.get("observed_at"))
    lease = getattr(owner, "_live_field_verification", None)
    if not lease or lease["identity"] != identity:
        lease = {"identity": identity, "regions": {}}
        owner._live_field_verification = lease
    lease["regions"].update({tuple(c): checked_at for c in coordinates})


def current_evidence(owner, raw, observer):
    """Current projection follows field proof time, not producer completion.

    Capture errors/pauses still withhold current claims; successful old data is
    retained separately. The query reviewer can recover it using a fresh frame.
    """
    lease = getattr(owner, "_live_field_verification", None)
    identity = (getattr(owner, "_observation_epoch", 0), getattr(owner, "_observation_serial", 0), raw.get("observed_at"))
    captured = timestamp(raw.get("observed_at"))
    if captured is None or observation_tick(time.time()) - captured > STALE_MAX_AGE_SECONDS:
        # Delivery, not review: past the hard cap the current view is
        # unavailable, never a stale one-name or zero-name roster.
        return public_visual_evidence(raw, observer)
    if (not lease or lease["identity"] != identity
            or not observer.get("enabled") or observer.get("state") not in {"observing", "updating"}
            or getattr(owner, "_observation_invalidated", False)
            or getattr(owner, "_observation_pending_reason", "").startswith(("capture_error", "capture_unavailable"))):
        return public_visual_evidence(raw, observer)
    from .delivery_scope import coordinates, withhold_regions
    controls = {coordinates(p) for p in getattr(owner, "_accepted_frame_page_regions", ())}
    now = time.time()
    captured = timestamp(raw.get("observed_at")) or 0
    def valid(c):
        return 0 <= now - lease["regions"].get(c, captured) <= VISUAL_TTL_SECONDS
    deferred = getattr(owner, "_deferred_verification", None)
    challenged = deferred[2] if deferred and deferred[:2] == identity[:2] else set()
    if not controls or not all(valid(c) and c not in challenged for c in controls):
        return public_visual_evidence(raw, {**observer, "state": "updating"})
    value = candidate_evidence(raw)
    screen = raw.get("screen_facts") or {}
    proofs = [p for f in screen.get("facts", []) for p in f.get("evidence", [])
              if p.get("validation_scope") != "control_presence"]
    proofs += [*screen.get("visible_text", []), *raw.get("characters", [])]
    expired = {coordinates(p) for p in proofs if not valid(coordinates(p))}
    value = withhold_regions(value, expired | challenged, identity_proofs=raw.get("characters", []))
    value["last_verified_at"] = datetime.fromtimestamp(min(lease["regions"].get(c, captured) for c in controls), timezone.utc).isoformat()
    value["freshness_basis"] = "scoped_live_verification"
    return value
