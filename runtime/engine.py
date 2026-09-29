"""Host-independent scene stabilization and event production."""

from __future__ import annotations

import time
from typing import Any

from .arbiter import EventArbiter
from .contracts import CompanionEvent


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
        self.last_analysis: dict[str, Any] = {}
        self.last_decisions: list[dict[str, str]] = []
        self.last_meaningful_event_at = 0.0
        self.last_idle_chatter_at = 0.0

    def reset(self) -> None:
        self.arbiter.reset()
        self.stable_scene = "unknown"
        self.stable_scene_since = 0.0
        self.pending_scene = ""
        self.pending_count = 0
        self.stable_character_ids = ()
        self.pending_character_ids = ()
        self.pending_character_count = 0
        self.missing_character_count = 0
        self.last_analysis = {}
        self.last_decisions = []
        self.last_meaningful_event_at = 0.0
        self.last_idle_chatter_at = 0.0

    def ingest(
        self, analysis: dict[str, Any], *, now: float | None = None
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
                self.pending_count = (
                    self.pending_count + 1 if self.pending_scene == scene else 1
                )
                self.pending_scene = scene
                required = (
                    1
                    if self.stable_scene == "unknown" and confidence == "high"
                    else self.confirmation_frames
                )
                if self.pending_count >= required:
                    previous = self.stable_scene
                    self.stable_scene = scene
                    self.stable_scene_since = observed_at
                    self.pending_scene = ""
                    self.pending_count = 0
                    proactive = scene in self._PROACTIVE_SCENES
                    candidates.append(
                        CompanionEvent(
                            event_id=f"scene:{scene}",
                            kind="scene_changed",
                            priority=self._SCENE_PRIORITY.get(scene, 1),
                            ts=observed_at,
                            payload={"previous": previous, "current": scene},
                            cooldown_seconds=self._SCENE_COOLDOWN.get(scene, 12.0),
                            proactive=proactive,
                            preempt=scene == "combat",
                        )
                    )
                    self.last_meaningful_event_at = observed_at

        ids = tuple(
            sorted(
                str(item.get("entity_id"))
                for item in analysis.get("characters") or []
                if isinstance(item, dict) and item.get("entity_id")
            )
        )
        if ids:
            self.missing_character_count = 0
            if ids == self.stable_character_ids:
                self.pending_character_ids = ()
                self.pending_character_count = 0
            else:
                self.pending_character_count = (
                    self.pending_character_count + 1
                    if self.pending_character_ids == ids
                    else 1
                )
                self.pending_character_ids = ids
                if self.pending_character_count >= self.confirmation_frames:
                    self.stable_character_ids = ids
                    self.pending_character_ids = ()
                    self.pending_character_count = 0
                    names = [
                        str(item.get("name"))
                        for item in analysis.get("characters") or []
                        if isinstance(item, dict) and item.get("entity_id") in ids
                    ]
                    candidates.append(
                        CompanionEvent(
                            event_id="characters:" + ",".join(ids),
                            kind="characters_identified",
                            priority=4,
                            ts=observed_at,
                            payload={"entity_ids": list(ids), "names": names},
                            max_age_seconds=15.0,
                            cooldown_seconds=90.0,
                            cooldown_key="characters_identified",
                            proactive=True,
                        )
                    )
                    self.last_meaningful_event_at = observed_at
        elif verified and self.stable_character_ids:
            self.missing_character_count += 1
            if (
                scene != "menu"
                or self.missing_character_count >= self.confirmation_frames
            ):
                self.stable_character_ids = ()
                self.pending_character_ids = ()
                self.pending_character_count = 0
                self.missing_character_count = 0

        if (
            not candidates
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

        selected, decisions = self.arbiter.decide(candidates, now=observed_at)
        if selected is not None and selected.kind == "exploration_idle":
            self.last_idle_chatter_at = observed_at
        self.last_decisions = decisions
        return selected, decisions

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
