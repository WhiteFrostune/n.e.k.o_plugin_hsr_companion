"""Current-evidence read/revocation boundary and detached delivery snapshots.

Compatibility state still lives on the plugin while producer migration is
incremental. Historical display is deliberately not an answer cache.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from plugin.sdk.plugin import unwrap_or

from .contracts import STALE_MAX_AGE_SECONDS
from .observation_evidence import public_visual_evidence


def richer_last_successful(latest, previous, *, now=None):
    """A same-page older full read may be richer than a current partial commit.

    This is still an old reading: the caller labels its age and the pixel
    guards decide whether it can be used. It never merges rows.
    """
    if not isinstance(latest, dict) or not isinstance(previous, dict):
        return latest
    latest_page = str(((latest.get("page") or {}).get("page_type")) or "")
    previous_page = str(((previous.get("page") or {}).get("page_type")) or "")
    if not latest_page or latest_page != previous_page:
        return latest
    if len(previous.get("characters") or []) <= len(latest.get("characters") or []):
        return latest
    try:
        captured = datetime.fromisoformat(str(previous.get("observed_at") or "").replace("Z", "+00:00"))
        if captured.tzinfo is None:
            captured = captured.replace(tzinfo=timezone.utc)
        age = ((now or datetime.now(timezone.utc)) - captured).total_seconds()
    except (TypeError, ValueError):
        return latest
    return previous if 0 <= age <= STALE_MAX_AGE_SECONDS else latest


def field_projection(value, fields):
    """Compare only answer dependencies; never equate a new page with an old one."""
    facts = (value.get("screen_facts") or {}).get("facts") or []
    projection = {"fresh": value.get("fresh"), "page_type": value.get("page_type"), "scene": value.get("scene")}
    if not fields:
        projection.update(facts=facts, visible_text=(value.get("screen_facts") or {}).get("visible_text") or [],
                          characters=value.get("characters"), controlled_character=value.get("controlled_character"))
    if "attributes" in fields:
        projection["panel_subjects"] = [a for a in value.get("characters") or [] if a.get("identity_scope") == "detail_subject"]
    kinds = {"location": {"current_location"}, "quest": {"tracked_quest", "quest_objective", "quest_destination"}}
    for name in fields:
        if name in {"characters", "controlled_character"}:
            projection[name] = value.get(name)
        elif name == "visible_text":
            projection[name] = (value.get("screen_facts") or {}).get("visible_text") or []
        elif name != "page":
            projection[name] = [f for f in facts if f.get("kind") in kinds.get(name, set()) or f.get("scope") == name]
    return json.dumps(projection, sort_keys=True, ensure_ascii=True)


def richest_usable(latest, candidates, *, now=None):
    """Pick the most complete same-page reading that is still within the cap."""
    best = latest
    for candidate in candidates:
        best = richer_last_successful(best, candidate, now=now)
    return best


def attach_team_roster(latest, slots):
    """Attach completeness to an old full frame using the last slot table."""
    if not isinstance(latest, dict) or not slots:
        return latest
    page = str(((latest.get("page") or {}).get("page_type")) or "")
    if page != "team_setup":
        return latest
    from ..detectors.team import assign_slot_names
    from .contracts import team_roster_state

    normalized = []
    for slot in slots:
        state = slot.get("state")
        normalized.append({**slot, "state": "filled" if state in {"filled_named", "filled_unread"} else state})
    named = [item for item in latest.get("characters") or [] if isinstance(item, dict) and item.get("region")]
    assigned = assign_slot_names(normalized, named)
    latest.update(team_roster_state(page, latest.get("characters"), slots=assigned))
    return latest


@dataclass(frozen=True)
class ObservationSnapshot:
    epoch: int
    serial: int
    _value: dict = field(repr=False)
    _fields: frozenset = field(default_factory=frozenset, repr=False)
    _equipment_key: tuple = field(default_factory=tuple, repr=False)
    _generation: int | None = field(default=None, repr=False)
    _capture_revision: int | None = field(default=None, repr=False)

    @classmethod
    def select(cls, owner, value: dict, *, fields=frozenset()):
        handoff = value.get("handoff", value.get("reuse", {}))
        epoch = handoff.get("epoch", getattr(owner, "_observation_epoch", 0))
        serial = (value.get("screen_facts") or {}).get("observation_serial") or getattr(owner, "_observation_serial", 0)
        from .delivery_boundary import equipment_key
        object_key = equipment_key(value)
        raw = getattr(owner, "_committed_observation", None)
        if object_key and raw and raw.get("observation_serial") == serial:
            raw_key = equipment_key(raw)
            if set(object_key) <= set(raw_key):
                # A main-only query still tracks the whole object's identity;
                # unrequested rows are NEVER added to the returned packet.
                object_key = raw_key
        return cls(epoch, serial, copy.deepcopy(value), frozenset(fields), object_key,
                   getattr(owner, "_answer_query_generation", None), getattr(owner, "_capture_failure_revision", 0))

    def matches(self, owner) -> bool:
        decision = self.delivery(owner)["delivery_boundary"]
        if "attributes" in self._fields and "characters" in decision["withheld_fields"]:
            return False  # A changed owner cannot certify the old assessment.
        return decision["state"] != "rejected" and not set(decision["withheld_fields"]) & (
            self._fields or {"page", "location", "quest", "characters", "controlled_character", "visible_text",
                             "equipment", "attributes", "skill", "enemy", "dialogue", "resource"})

    def delivery(self, owner, *, generation=None) -> dict:
        from .delivery_boundary import review
        return review(self, owner, generation=generation)

    def evidence(self) -> dict:
        return copy.deepcopy(self._value)


class ObservationStore:
    def __init__(self, owner):
        self.owner = owner

    async def current(self) -> dict:
        from .review_policy import projection_status
        owner = self.owner
        epoch = getattr(owner, "_observation_epoch", 0)
        latest = getattr(owner, "_committed_observation", None)
        live_commit = latest is not None
        candidates = [getattr(owner, "_last_successful_observation", None)]
        reusable = getattr(owner, "_reusable_observations", {}) or {}
        for key in ("full", "full_previous"):
            snapshot = reusable.get(key)
            if isinstance(snapshot, dict):
                candidates.append(snapshot.get("evidence"))
        chosen = richest_usable(latest, candidates)
        if chosen is not latest:
            latest = copy.deepcopy(chosen)
            live_commit = False
        slots = getattr(owner, "_team_slot_states", None)
        if latest is not None and slots:
            latest = copy.deepcopy(latest)
            stable = set(((latest.get("runtime") or {}).get("stable_character_ids")) or ())
            if stable:
                latest["characters"] = [item for item in latest.get("characters") or []
                                        if item.get("entity_id") in stable]
            attach_team_roster(latest, slots)
        if latest is None:
            latest = unwrap_or(await owner._get_store("last_local_vision"), {})
        latest = getattr(owner, "_scene_preview", None) or latest
        observer = projection_status(owner)
        if not live_commit:
            # Disk history has no live proof; unlike a resident commit it must
            # not become current merely because a question was asked.
            from .live_refresh import query_pending
            if query_pending(owner, latest or {}):
                observer["state"] = "updating"
        if (epoch != getattr(owner, "_observation_epoch", 0)
                or getattr(owner, "_observation_invalidated", False)):
            latest = {"invalidated": True, "source": "assistant_local_ocr"}
        if getattr(owner, "_observer_stop_requested", False):
            observer.update(enabled=False, state="stopped")
        # A busy verifier withholds challenged fields without erasing the commit.
        if live_commit and not getattr(owner, "_scene_preview", None):
            from .fact_lifecycle import current_evidence
            value = current_evidence(owner, latest, observer)
        else:
            value = copy.deepcopy(public_visual_evidence(latest, observer))
        deferred = getattr(owner, "_deferred_verification", None)
        if deferred and deferred[:2] == (getattr(owner, "_observation_epoch", 0), getattr(owner, "_observation_serial", 0)):
            from .delivery_scope import withhold_regions
            value = withhold_regions(value, deferred[2], identity_proofs=(latest or {}).get("characters", []))
        return value

    def commit(self, analysis, *, archive=True):
        """Atomically replace current state; never promote historical display."""
        owner = self.owner
        from .live_refresh import cancel_guard_review
        cancel_guard_review(owner)  # The new immutable commit owns its own proof work.
        owner._committed_observation = copy.deepcopy(analysis)
        team_slots = (analysis.get("recognition") or {}).get("team_slots")
        if team_slots:
            owner._team_slot_states = copy.deepcopy(team_slots)
        owner._producer_validation = None  # Set atomically with live guards by the producer only.
        owner._live_field_verification = None
        owner._deferred_verification = None
        owner._observation_pending_reason = ""
        public = public_visual_evidence(analysis, {"state": "observing", "enabled": True})
        if archive and (public.get("scene", {}).get("verified") or public.get("characters") or public["screen_facts"].get("facts")):
            previous = getattr(owner, "_last_successful_observation", None)
            from .query_reuse import content_coverage
            # A page-only/navigation pass must not replace the last full read
            # of that page in the display archive. No fields are merged.
            if (not previous or public["read_scope"] == "full"
                    and not (content_coverage(previous or {}) - content_coverage(public))
                    or (previous.get("page") or {}).get("page_type") != public["page_type"]):
                owner._last_successful_observation = copy.deepcopy(analysis)

    def suspend(self, reason: str) -> None:
        """A missing capture revokes answer permission, not successful data."""
        owner = self.owner
        if reason.startswith(("capture_error", "capture_unavailable")):
            owner._capture_failure_revision = getattr(owner, "_capture_failure_revision", 0) + 1
        owner._observation_pending_reason = reason
        owner._cancel_pending_speech("capture_pending")
        owner._observer_last_signature = None
        if hasattr(owner, "_visual_update_event"):
            owner._visual_update_event.set()
        if hasattr(owner, "_observer_wake_event"):
            owner._observer_wake_event.set()

    async def display(self) -> dict:
        """Panel-only projection. History is never passed to chat/proactive tools."""
        current = await self.current()
        previous = getattr(self.owner, "_last_successful_observation", None)
        if previous is None:
            previous = unwrap_or(await self.owner._get_store("last_local_vision"), {})
        history = {}
        if isinstance(previous, dict) and previous.get("observed_at"):
            try:
                captured = datetime.fromisoformat(previous["observed_at"].replace("Z", "+00:00"))
                history = public_visual_evidence({**previous, "invalidated": False},
                    {"enabled": True, "state": "observing"}, now=captured)
                history.update(fresh=False, status="historical", can_answer_character_identity=False,
                               instruction="仅供面板展示上次读取结果，不能用于当前画面回答。")
                history["screen_facts"]["fresh"] = False
                history["screen_facts"]["equipment_card"]["historical_display_only"] = True
            except (TypeError, ValueError):
                pass
        return {"current": current, "last_successful": history,
                "pending_reason": getattr(self.owner, "_observation_pending_reason", ""),
                "revision": getattr(self.owner, "_observation_serial", 0)}

    def defer_regions(self, coordinates):
        owner = self.owner
        owner._review_challenge_serial = getattr(owner, "_review_challenge_serial", 0) + 1
        from .delivery_scope import coordinates as region_key
        revision = (getattr(owner, "_observation_epoch", 0), getattr(owner, "_observation_serial", 0))
        previous = getattr(owner, "_deferred_verification", None)
        regions = set(previous[2]) if previous and previous[:2] == revision else set()
        owner._deferred_verification = (*revision, regions | coordinates)
        if coordinates & {region_key(p) for p in getattr(owner, "_accepted_frame_page_regions", ())}:
            self.suspend("verification_pending_control")

    def clear_deferred(self, coordinates):
        owner = self.owner
        pending = getattr(owner, "_deferred_verification", None)
        revision = (getattr(owner, "_observation_epoch", 0), getattr(owner, "_observation_serial", 0))
        if pending and pending[:2] == revision:
            remaining = pending[2] - coordinates
            owner._deferred_verification = (*pending[:2], remaining) if remaining else None
            if remaining != pending[2] and hasattr(owner, "_visual_update_event"):
                # A same-revision review is useful progress, not a new OCR
                # commit. Wake waiting questions to reselect scoped evidence.
                owner._visual_update_event.set()

    def withdraw_regions(self, coordinates):
        """Revoke only fields depending on changed pixels; no new facts are added."""
        owner = self.owner
        deferred = getattr(owner, "_deferred_verification", None)
        previous_revision = (getattr(owner, "_observation_epoch", 0), getattr(owner, "_observation_serial", 0))
        current = getattr(owner, "_committed_observation", None)
        if not current:
            return False
        value = copy.deepcopy(current)
        screen = value.get("screen_facts") or {}
        def affected(proof):
            region = proof.get("region") or {}
            return tuple(region.get(k, -1) for k in ("left", "top", "right", "bottom")) in coordinates
        removed = [f for f in screen.get("facts") or [] if any(affected(p) for p in f.get("evidence") or []
                                                             if p.get("validation_scope") != "control_presence")]
        rows = [r for r in screen.get("visible_text") or [] if not affected(r)]
        if not removed and len(rows) == len(screen.get("visible_text") or []):
            return False
        owner._observation_serial = getattr(owner, "_observation_serial", 0) + 1
        value["observation_serial"] = owner._observation_serial
        value["screen_facts"] = {**screen, "facts": [f for f in screen.get("facts") or [] if f not in removed],
                                 "visible_text": rows, "unclassified_text": [],
                                 "field_checks": [*screen.get("field_checks", []),
                                     *[{"kind": f.get("kind"), "scope": f.get("scope"), "state": "pending",
                                        "reason": "text_not_revalidated"} for f in removed]][-96:]}
        self.commit(value, archive=False)
        if deferred and deferred[:2] == previous_revision and deferred[2] - coordinates:
            owner._deferred_verification = (previous_revision[0], owner._observation_serial, deferred[2] - coordinates)
        guard = getattr(owner, "_accepted_frame_delivery_guard", None)
        if guard:
            retained = [(r, p) for r, p in zip(*guard) if p[0] not in coordinates]
            owner._accepted_frame_delivery_guard = ([r for r, _ in retained], tuple(p for _, p in retained)) if retained else None
        from .live_refresh import fact_regions
        owner._accepted_frame_characters = [*value.get("characters", []), *fact_regions(value)]
        owner._accepted_frame_scene_regions = [*getattr(owner, "_accepted_frame_page_regions", ()), *fact_regions(value)]
        owner._reusable_observations = {}
        # Unchanged retained regions passed THIS latest frame check. This does
        # not approve removed fields or manufacture a newer capture timestamp.
        owner._query_validated_read = (getattr(owner, "_query_refresh_generation", 0),
                                       getattr(owner, "_observation_epoch", 0), owner._observation_serial)
        if hasattr(owner, "_visual_update_event"):
            owner._visual_update_event.set()
        return True

    def revoke(self, reason="explicit_revocation") -> None:
        """Synchronous; the stop boundary cannot wait for disk or OCR."""
        owner = self.owner
        from .live_refresh import cancel_guard_review
        cancel_guard_review(owner)
        owner._cancel_pending_speech("observation_invalidated")
        owner._committed_observation = None
        owner._observation_pending_reason = ""
        owner._reusable_observations = {}
        before = getattr(owner, "_observation_epoch", 0)
        owner._observation_epoch = before + 1
        from .revocation_trace import record
        record(owner, reason, before)
        for name in ("_accepted_frame_stamp", "_accepted_frame_delivery_guard", "_accepted_frame_query_guard",
                     "_accepted_frame_full_query_guard", "_query_validated_read",
                     "_next_frame_stamp", "_next_frame_query_guard", "_next_frame_delivery_guard",
                     "_next_frame_query_scene_guard", "_scene_preview", "_preview_frame_stamp"):
            setattr(owner, name, None)
        owner._next_frame_full_query_guard = None
        owner._next_frame_identity_guarded = False
        for name in ("_accepted_frame_characters", "_accepted_frame_scene_regions", "_accepted_frame_page_regions",
                     "_next_frame_scene_regions", "_next_frame_page_regions"):
            setattr(owner, name, ())
        owner._accepted_frame_scene_only = False
        owner._accepted_frame_identity_guarded = False
        owner._accepted_frame_page_type = "unknown"
        owner._next_frame_scene_only = False
        owner._observation_invalidated = True
        owner._deferred_verification = None
        owner._runtime.invalidate_observation(reason=reason)
        if hasattr(owner, "_visual_update_event"):
            owner._visual_update_event.set()
