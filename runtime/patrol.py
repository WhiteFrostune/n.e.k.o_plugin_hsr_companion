"""D: suppress unnecessary idle full passes, without granting freshness.

No OCR admission/priority changes, deadline changes, new receipts or retiming.
Unsupported/dynamic pages keep the existing observer schedule.
"""
import asyncio
import hashlib
import math
import time
from io import BytesIO

from ..detectors.patrol_regions import static_content_regions
from .contracts import STALE_MAX_AGE_SECONDS
from .fact_lifecycle import timestamp
from .query_reuse import LIVE_FRAME_MAX_AGE_SECONDS, complete_fact_guard

_MIN_QUIET_SECONDS = 4.0
_RETRY_SECONDS = 40.0
_DEFAULT_LEAD_SECONDS = 33.0


def _fingerprints(payload, regions, expected_size):
    from PIL import Image

    with Image.open(BytesIO(payload)) as image:
        if list(image.size) != expected_size or image.width*image.height > 16_000_000:
            return None
        # Native RGB pixels; no thumbnail can hide a newly visible digit.
        patches = []
        for left, top, right, bottom in regions:
            box = (math.floor(left*image.width), math.floor(top*image.height),
                   math.ceil(right*image.width), math.ceil(bottom*image.height))
            chip = image.crop(box).convert("RGB")
            patches.append((chip.size, hashlib.sha256(chip.tobytes()).digest()))
        return tuple(patches)


def _basis(owner, pid):
    raw = getattr(owner, "_committed_observation", None) or {}
    key = (getattr(owner, "_observation_epoch", 0), getattr(owner, "_last_full_commit_serial", 0),
           getattr(owner, "_capture_failure_revision", 0), pid, (raw.get("page") or {}).get("page_type"))
    return key, raw


def _note(owner, decision, reason, *, fallback=False):
    previous = getattr(owner, "_patrol_status", {})
    owner._patrol_status = {"decision": decision, "reason": reason, "fallback": fallback,
        "checks": previous.get("checks", 0)+1,
        "suppressed": previous.get("suppressed", 0)+int(decision == "skip"),
        "age_refreshes": previous.get("age_refreshes", 0)+int(reason == "age_budget")}


async def remember_capture(owner, payload):
    """Anchor only the exact source of a newly committed full observation."""
    pid = getattr(owner, "_analysis_game_pid", None)
    key, raw = _basis(owner, pid)
    regions = static_content_regions(raw)
    size = (raw.get("recognition") or {}).get("source_size")
    if (not key[1] or not regions or len(regions) > 48 or not size or not complete_fact_guard(raw)):
        owner._patrol_anchor = None
        return
    patches = await asyncio.to_thread(_fingerprints, payload, regions, size)
    if patches is not None and _basis(owner, pid)[0] == key:
        observed = timestamp(raw.get("observed_at"))
        elapsed = time.time() - observed if observed is not None else None
        lead = min(35.0, max(8.0, elapsed + 3.0)) if elapsed is not None and elapsed >= 0 else _DEFAULT_LEAD_SECONDS
        owner._patrol_anchor = {"key": key, "regions": regions, "patches": patches,
                               "first_seq": -1, "first_at": 0, "frames": 0, "stable": False,
                               "observed": observed, "lead": lead, "next_refresh_at": time.monotonic() + _MIN_QUIET_SECONDS}


async def should_scan(owner, payload, *, requested, changed, frame_seq, captured_at, pid):
    """Only replace the old periodic trigger once fresh UI frames are stable."""
    def fallback(reason):
        _note(owner, "full" if requested else "wait", reason, fallback=True)
        return requested

    key, raw = _basis(owner, pid)
    observer = getattr(owner, "_observer_status", {})
    manual = getattr(owner, "_manual_refresh_task", None)
    if (not observer.get("enabled") or getattr(owner, "_observer_stop_requested", False)
            or getattr(owner, "_observation_invalidated", False)
            or getattr(owner, "_observation_pending_reason", "") or getattr(owner, "_scene_preview", None)
            or getattr(owner, "_deferred_verification", None)):
        return fallback("unconfirmed")
    if (type(captured_at) not in {int, float} or not 0 <= time.time()-captured_at <= LIVE_FRAME_MAX_AGE_SECONDS
            or type(frame_seq) is not int or not pid or pid != getattr(owner, "_analysis_game_pid", None)):
        return fallback("capture_unavailable")
    regions = static_content_regions(raw)
    size = (raw.get("recognition") or {}).get("source_size")
    if (not key[1] or not regions or len(regions) > 48 or not size or not complete_fact_guard(raw)
            or not getattr(owner, "_accepted_frame_delivery_guard", None)):
        return fallback("coverage_unavailable")
    state = getattr(owner, "_patrol_anchor", None)
    if state and state["key"] == key and state.get("challenged"):
        return fallback("awaiting_full_commit")
    if state is None or state["key"] != key:
        return fallback("awaiting_full_anchor")
    patches = await asyncio.to_thread(_fingerprints, payload, regions, size)
    if (patches is None or _basis(owner, pid)[0] != key
            or not 0 <= time.time()-captured_at <= LIVE_FRAME_MAX_AGE_SECONDS):
        return fallback("capture_changed")
    if regions != state["regions"] or patches != state["patches"] or changed:
        # Never roll a baseline forward through a cross-fade or a new object.
        state["challenged"] = True
        _note(owner, "full", "content_changed")
        return True
    if frame_seq > state["first_seq"] and captured_at > state["first_at"]:
        state["frames"] = min(2, state["frames"]+1)
        state["first_seq"], state["first_at"] = frame_seq, captured_at
        state["stable"] = state["frames"] >= 2
    runtime = owner._runtime
    regional_reading = getattr(owner, "_region_query_waiters", 0) > 0
    if (changed or getattr(owner, "_observer_last_signature", None) is None
            or manual is not None and not manual.done()
            or runtime.pending_scene or runtime.pending_character_ids
            or (getattr(owner, "_visual_query_waiters", 0) or getattr(owner, "_pending_answer", None))
               and not regional_reading):
        return fallback("demand")
    if state["stable"]:
        now = time.monotonic()
        observed = state["observed"]
        if (not regional_reading and observed is not None and now >= state["next_refresh_at"]
                and time.time() - observed >= STALE_MAX_AGE_SECONDS - state["lead"]):
            state["next_refresh_at"] = now + _RETRY_SECONDS
            _note(owner, "full", "age_budget")
            return True
        _note(owner, "skip", "stable_content")
        return False
    return fallback("confirming_frames")
