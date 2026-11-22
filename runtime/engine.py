"""Host-independent scene stabilization and event production."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import replace
from typing import Any

from .arbiter import EventArbiter
from .contracts import CompanionEvent, native_name_confirmed
from .recognition_metrics import RESET_LIMIT, count_reset

IDENTITY_CONFIRMATION_SECONDS = 70.0
_IDENTITY_PAGES = {"character_detail", "character_attributes", "team_setup"}


class CompanionRuntime:
    _PROACTIVE_SCENES = {"combat", "team_setup", "warp", "reward"}
    _SCENE_PRIORITY = {
        "combat": 8,
        "reward": 6,
        "warp": 5,
        "team_setup": 4,
        "story": 2,
        "exploration": 2,
        "menu": 2,
        "loading": 1,
        "other": 1,
    }
    _SCENE_COOLDOWN = {
        "combat": 45.0,
        "reward": 60.0,
        "warp": 60.0,
        "team_setup": 90.0,
    }

    def __init__(
        self,
        *,
        confirmation_frames: int = 2,
        global_rate_seconds: float = 18.0,
        idle_chatter_seconds: float = 150.0,
    ) -> None:
        self.confirmation_frames = max(1, int(confirmation_frames))
        self.idle_chatter_seconds = max(30.0, float(idle_chatter_seconds))
        self.arbiter = EventArbiter(global_rate_seconds=global_rate_seconds)
        self.stable_scene = "unknown"
        self.stable_scene_since = 0.0
        self.pending_scene = ""
        self.pending_count = 0
        self.stable_character_ids: tuple[str, ...] = ()
        self.pending_character_ids: tuple[str, ...] = ()
        self.pending_character_count = 0
        self.missing_character_count = 0
        self._identity_resets = deque(maxlen=RESET_LIMIT)
        self._identity_counts: dict[str, tuple[str, int]] = {}
        self._confirmation_page = ""
        self._confirmation_started = None
        self._confirmation_reason = ""
        self.last_analysis: dict[str, Any] = {}
        self.last_decisions: list[dict[str, str]] = []
        self.last_meaningful_event_at = 0.0
        self.last_idle_chatter_at = 0.0
        self._deferred_events: list[CompanionEvent] = []

    def reset(self) -> None:
        self._note_identity_reset("runtime_reset")
        self._clear_identity_confirmation()
        self.arbiter.reset()
        self._deferred_events.clear()
        self.stable_scene = "unknown"
        self.stable_scene_since = 0.0
        self.pending_scene = ""
        self.pending_count = 0
        self.stable_character_ids = ()
        self.pending_character_ids = ()
        self.pending_character_count = 0
        self.missing_character_count = 0
        self._identity_counts.clear()
        self.last_analysis = {}
        self.last_decisions = []
        self.last_meaningful_event_at = 0.0
        self.last_idle_chatter_at = 0.0

    def invalidate_observation(self, *, reason="observation_revoked") -> None:
        """Forget screen facts, not event cooldowns or the player's history."""
        self._note_identity_reset(reason)
        self._clear_identity_confirmation()
        self.stable_scene = "unknown"
        self.stable_scene_since = 0.0
        self.pending_scene = ""
        self.pending_count = 0
        self.stable_character_ids = ()
        self.pending_character_ids = ()
        self.pending_character_count = 0
        self.missing_character_count = 0
        self._identity_counts.clear()
        self.last_analysis = {}
        self._deferred_events.clear()

    def ingest(
        self, analysis: dict[str, Any], *, now: float | None = None,
        proactive_enabled: bool = False, idle_chatter_enabled: bool = False, track_identity: bool = True,
    ) -> tuple[CompanionEvent | None, list[dict[str, str]]]:
        observed_at = time.monotonic() if now is None else float(now)
        self.last_analysis = dict(analysis)
        candidates: list[CompanionEvent] = []

        scene = str((analysis.get("scene") or {}).get("primary_state") or "unknown")
        confidence = str((analysis.get("scene") or {}).get("confidence") or "low")
        verified = bool((analysis.get("scene") or {}).get("verified"))
        if scene != "unknown" and confidence != "low" and verified:
            if scene == self.stable_scene:
                self.pending_scene = ""
                self.pending_count = 0
            else:
                self.pending_count = self.pending_count + 1 if self.pending_scene == scene else 1
                self.pending_scene = scene
                required = 1 if self.stable_scene == "unknown" and confidence == "high" else self.confirmation_frames
                if self.pending_count >= required:
                    previous = self.stable_scene
                    self.stable_scene = scene
                    self.stable_scene_since = observed_at
                    self.pending_scene = ""
                    self.pending_count = 0
                    proactive = proactive_enabled and scene in self._PROACTIVE_SCENES
                    candidates.append(
                        CompanionEvent(
                            event_id=f"scene:{scene}",
                            kind="scene_changed",
                            priority=self._SCENE_PRIORITY.get(scene, 1),
                            ts=observed_at,
                            payload={"previous": previous, "current": scene},
                            cooldown_seconds=self._SCENE_COOLDOWN.get(scene, 12.0),
                            proactive=proactive,
                            preempt=proactive and scene == "combat",
                        )
                    )
                    self.last_meaningful_event_at = observed_at

        else:
            self.pending_scene = ""
            self.pending_count = 0
            # A new unknown frame is not evidence that the previous page is
            # still on screen. Identity confirmation remains independent.
            self.stable_scene = "unknown"
            self.stable_scene_since = 0.0

        ids = tuple(
            sorted(
                str(item.get("entity_id"))
                for item in analysis.get("characters") or []
                if isinstance(item, dict) and item.get("entity_id")
            )
        )
        previous_pending = self.pending_character_ids
        if ids:
            self.missing_character_count = 0
            previous_ids = self.stable_character_ids
            next_counts = {}
            for item in analysis.get("characters") or []:
                entity_id = str(item.get("entity_id") or "")
                scope = str(item.get("identity_scope") or "unknown")
                if scope in {"hud_label", "hud_portrait"}:
                    scope = "hud"
                key = f"{scope}:{item.get('slot') or entity_id}"
                previous_id, count = self._identity_counts.get(key, ("", 0))
                count = count + 1 if previous_id == entity_id else 1
                if native_name_confirmed(item):
                    count = max(count, self.confirmation_frames)
                next_counts[key] = (entity_id, count)
            # Each position is independent. An intermittently readable fourth
            # name cannot restart confirmation for the other three positions.
            # Missing/replaced positions are dropped, never replayed as current.
            removed = [value for key, value in self._identity_counts.items() if key not in next_counts]
            replaced = [value for key, value in self._identity_counts.items()
                        if key in next_counts and next_counts[key][0] != value[0]]
            for reason, cleared in (("slot_missing", removed), ("slot_replaced", replaced)):
                count_reset(self._identity_resets, reason, slots=len(cleared),
                            stable=sum(count >= self.confirmation_frames for _, count in cleared))
            self._identity_counts = next_counts
            self.stable_character_ids = tuple(
                sorted({entity_id for entity_id, count in next_counts.values() if count >= self.confirmation_frames})
            )
            self.pending_character_ids = tuple(sorted(set(ids) - set(self.stable_character_ids)))
            self.pending_character_count = min(
                (count for _, count in next_counts.values() if count < self.confirmation_frames),
                default=0,
            )
            if self.stable_character_ids and self.stable_character_ids != previous_ids:
                confirmed_ids = self.stable_character_ids
                names = [
                    str(item.get("name"))
                    for item in analysis.get("characters") or []
                    if isinstance(item, dict) and item.get("entity_id") in confirmed_ids
                ]
                candidates.append(
                    CompanionEvent(
                        event_id="characters:" + ",".join(confirmed_ids),
                        kind="characters_identified",
                        priority=4,
                        ts=observed_at,
                        payload={
                            "entity_ids": list(confirmed_ids),
                            "names": names,
                            "identities": [
                                {key: item.get(key) for key in ("entity_id", "name", "identity_scope", "evidence")}
                                for item in analysis.get("characters") or []
                                if isinstance(item, dict) and item.get("entity_id") in confirmed_ids
                            ],
                        },
                        max_age_seconds=30.0,
                        cooldown_seconds=90.0,
                        cooldown_key="characters_identified",
                        proactive=proactive_enabled,
                    )
                )
                self.last_meaningful_event_at = observed_at
        else:
            count_reset(self._identity_resets, "empty_character_list", slots=len(self._identity_counts))
            self._identity_counts.clear()
            self.pending_character_ids = ()
            self.pending_character_count = 0
            self.missing_character_count += 1
            if (verified and scene != "menu") or self.missing_character_count >= self.confirmation_frames:
                count_reset(self._identity_resets, "verified_non_menu" if verified and scene != "menu"
                            else "missing_frame_limit", stable=len(self.stable_character_ids))
                self.stable_character_ids = ()
                self.pending_character_ids = ()
                self.pending_character_count = 0
                self.missing_character_count = 0

        if track_identity:
            self._track_identity_confirmation(analysis, observed_at, previous_pending)

        if (
            proactive_enabled and idle_chatter_enabled
            and not candidates
            and verified
            and scene == self.stable_scene
            and self.stable_scene == "exploration"
            and self.stable_scene_since > 0
            and observed_at - self.stable_scene_since >= self.idle_chatter_seconds
            and observed_at - self.last_meaningful_event_at >= self.idle_chatter_seconds
            and observed_at - self.last_idle_chatter_at >= self.idle_chatter_seconds
        ):
            candidates.append(
                CompanionEvent(
                    event_id="exploration_idle",
                    kind="exploration_idle",
                    priority=1,
                    ts=observed_at,
                    max_age_seconds=12.0,
                    cooldown_seconds=self.idle_chatter_seconds,
                    cooldown_key="exploration_idle",
                    proactive=True,
                )
            )

        deferred = [
            candidate if proactive_enabled else replace(candidate, proactive=False, preempt=False)
            for candidate in self._deferred_events
            if observed_at - candidate.ts <= candidate.max_age_seconds
            and (
                (
                    candidate.kind == "characters_identified"
                    and tuple(sorted(candidate.payload.get("entity_ids") or [])) == ids
                )
                or (candidate.kind == "scene_changed" and verified and candidate.payload.get("current") == scene)
            )
        ]
        combined = {candidate.event_id: candidate for candidate in [*deferred, *candidates]}
        selected, decisions = self.arbiter.decide(list(combined.values()), now=observed_at)
        retry_ids = {
            item["event_id"] for item in decisions if item["reason"] in {"global_rate", "lost_to_higher_priority"}
        }
        self._deferred_events = [candidate for candidate in combined.values() if candidate.event_id in retry_ids]
        if selected is not None and selected.kind == "exploration_idle":
            self.last_idle_chatter_at = observed_at
        self.last_decisions = decisions
        return selected, decisions

    def _clear_identity_confirmation(self):
        self._confirmation_page = ""
        self._confirmation_started = None
        self._confirmation_reason = ""

    def observe_identity_page(self, page, *, now=None, expects_identity=False):
        if page != self._confirmation_page:
            self._clear_identity_confirmation()
            self._confirmation_page = page
        if (expects_identity or page in _IDENTITY_PAGES) and self._confirmation_started is None:
            self._confirmation_started = time.monotonic() if now is None else now
            self._confirmation_reason = "insufficient_confirmations"

    def identity_confirmation(self, *, page=None, now=None):
        if self._confirmation_started is None or page not in (None, "unknown", self._confirmation_page):
            return None
        elapsed = max(0.0, (time.monotonic() if now is None else now) - self._confirmation_started)
        return {"state": "failed" if elapsed >= IDENTITY_CONFIRMATION_SECONDS else "pending",
                "reason": self._confirmation_reason, "elapsed_seconds": round(elapsed, 1),
                "limit_seconds": IDENTITY_CONFIRMATION_SECONDS}

    def note_identity_blocker(self, reason, *, now=None):
        state = self.identity_confirmation(now=now)
        if state and state["state"] == "pending":
            self._confirmation_reason = reason

    def retry_identity_confirmation(self):
        state = self.identity_confirmation()
        if state and state["state"] == "failed":
            self._confirmation_started = time.monotonic()
            self._confirmation_reason = "insufficient_confirmations"

    def _track_identity_confirmation(self, analysis, now, previous_pending):
        page = (analysis.get("page") or {}).get("page_type")
        scene = analysis.get("scene") or {}
        recognition = analysis.get("recognition") or {}
        if not scene.get("verified") or not page or page == "unknown" or recognition.get("navigation_only_query"):
            return
        if self.stable_character_ids and not self.pending_character_ids:
            self._clear_identity_confirmation()
            return
        self.observe_identity_page(page, now=now, expects_identity=bool(self.pending_character_ids))
        if previous_pending and set(previous_pending) - set(self.pending_character_ids):
            self.note_identity_blocker("confirmation_reset", now=now)
        self.note_identity_read_issue(analysis, now=now)

    def note_identity_read_issue(self, analysis, *, now=None):
        page = (analysis.get("page") or {}).get("page_type")
        if page != self._confirmation_page:
            return
        reader = (analysis.get("recognition") or {}).get("text_reader") or {}
        error = str((analysis.get("ocr") or {}).get("error") or "").lower()
        if "ocr_time_budget" in (reader.get("errors") or ()) or "timeout" in error:
            self.note_identity_blocker("ocr_timeout", now=now)

    def _note_identity_reset(self, reason):
        count_reset(self._identity_resets, reason, slots=len(self._identity_counts),
                    stable=len(self.stable_character_ids))

    def identity_diagnostics(self) -> dict[str, Any]:
        return {"required": self.confirmation_frames,
                "tracked_counts": sorted(count for _, count in self._identity_counts.values()),
                "pending_count": self.pending_character_count,
                "confirmed_count": len(self.stable_character_ids),
                "missing_count": self.missing_character_count,
                "recent_resets": [dict(row) for row in self._identity_resets],
                "confirmation": self.identity_confirmation()}

    def snapshot(self) -> dict[str, Any]:
        return {
            "stable_scene": self.stable_scene,
            "stable_scene_since": self.stable_scene_since,
            "pending_scene": self.pending_scene,
            "pending_scene_confirmations": self.pending_count,
            "stable_character_ids": list(self.stable_character_ids),
            "pending_character_ids": list(self.pending_character_ids),
            "pending_character_confirmations": self.pending_character_count,
            "missing_character_confirmations": self.missing_character_count,
            "last_decisions": list(self.last_decisions),
            "idle_chatter_seconds": self.idle_chatter_seconds,
            "last_idle_chatter_at": self.last_idle_chatter_at,
        }


__all__ = ["CompanionRuntime"]
