"""Bounded, in-memory ownership of automatic cues before SDK submission.

Not a playback scheduler: host coalescing/pacing still owns submitted messages.
No claim is made that a tool result was spoken, or that a submitted cue can be
retracted through the public SDK.
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass

from .contracts import CompanionEvent


@dataclass(frozen=True)
class PendingSpeech:
    event: CompanionEvent
    epoch: int
    scene: str
    page_type: str
    ready_at: float


class SpeechArbiter:
    def __init__(self, *, hold_seconds: float = 3, quiet_seconds: float = 15,
                 coverage_seconds: float = 30) -> None:
        self.hold_seconds = max(0, hold_seconds)
        self.quiet_seconds = max(0, quiet_seconds)
        self.coverage_seconds = max(0, coverage_seconds)
        self.pending: PendingSpeech | None = None
        self.last_query_at = -1e9
        self._covered: tuple[int, str, str, float] | None = None
        self._counts: Counter[str] = Counter()
        self._history: deque[dict] = deque(maxlen=32)

    def _record(self, cue: PendingSpeech, reason: str) -> None:
        self._counts[reason] += 1
        self._history.append({"event_id": cue.event.event_id, "kind": cue.event.kind,
                              "epoch": cue.epoch, "reason": reason})

    def cancel(self, reason: str) -> None:
        if self.pending is not None:
            self._record(self.pending, reason)
            self.pending = None

    def query_started(self, *, now: float) -> None:
        self.last_query_at = now
        self.cancel("query_started")

    def user_activity(self, *, now: float) -> None:
        self.last_query_at = max(self.last_query_at, now)
        self.cancel("user_activity")

    def covered_by_tool(self, *, epoch: int, scene: str, page_type: str,
                        fresh: bool, verified: bool, now: float) -> None:
        # Coverage is only evidence returned by a tool, never a playback receipt.
        if fresh and verified and scene != "unknown":
            self._covered = (epoch, scene, page_type, now + self.coverage_seconds)

    def offer(self, event: CompanionEvent, *, epoch: int, scene: str,
              page_type: str, now: float) -> PendingSpeech | None:
        self.cancel("coalesced_newer")
        cue = PendingSpeech(event, epoch, scene, page_type, now + self.hold_seconds)
        self.pending = cue
        reason = self.check(cue, now=now, epoch=epoch, scene=scene,
                            page_type=page_type, available=True, query_busy=False)
        if reason not in {"ready", "holding"}:
            self.cancel(reason)
            return None
        self._record(cue, "queued")
        return cue

    def check(self, cue: PendingSpeech, *, now: float, epoch: int, scene: str,
              page_type: str, available: bool, query_busy: bool) -> str:
        if self.pending is not cue:
            return "superseded"
        if now < cue.event.ts or now - cue.event.ts >= cue.event.max_age_seconds:
            return "expired"
        if not available:
            return "unavailable"
        if epoch != cue.epoch or scene != cue.scene or page_type != cue.page_type:
            return "observation_changed"
        if query_busy or now - self.last_query_at < self.quiet_seconds:
            return "recent_query"
        if self._covered is not None:
            covered_epoch, covered_scene, covered_page, until = self._covered
            if (epoch, scene, page_type) == (covered_epoch, covered_scene, covered_page) and now < until:
                return "covered_by_tool"
        return "holding" if now < cue.ready_at else "ready"

    def finish(self, cue: PendingSpeech, reason: str) -> None:
        if self.pending is cue:
            self._record(cue, reason)
            self.pending = None

    def snapshot(self) -> dict:
        return {"pending_count": int(self.pending is not None),
                "counts": dict(self._counts), "recent": list(self._history),
                "ack_scope": "sdk_submission_only", "can_retract_submitted": False}
