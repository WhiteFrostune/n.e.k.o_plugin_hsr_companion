"""At-most-one event arbiter with freshness, cooldown and global pacing."""

from __future__ import annotations

from .contracts import CompanionEvent


class EventArbiter:
    def __init__(self, *, global_rate_seconds: float = 18.0) -> None:
        self.global_rate_seconds = max(0.0, float(global_rate_seconds))
        self._last_output_at = -1e9
        self._last_by_key: dict[str, float] = {}

    def reset(self) -> None:
        self._last_output_at = -1e9
        self._last_by_key.clear()

    def decide(
        self, candidates: list[CompanionEvent], *, now: float
    ) -> tuple[CompanionEvent | None, list[dict[str, str]]]:
        decisions: list[dict[str, str]] = []
        survivors: list[CompanionEvent] = []
        for event in candidates:
            if now - event.ts > event.max_age_seconds:
                decisions.append({"event_id": event.event_id, "result": "dropped", "reason": "stale"})
                continue
            cooldown_key = event.cooldown_key or event.event_id
            if now - self._last_by_key.get(cooldown_key, -1e9) < event.cooldown_seconds:
                decisions.append({"event_id": event.event_id, "result": "dropped", "reason": "cooldown"})
                continue
            survivors.append(event)
        if not survivors:
            return None, decisions
        chosen = max(survivors, key=lambda item: (item.priority, item.ts))
        if (
            chosen.proactive
            and not chosen.preempt
            and now - self._last_output_at < self.global_rate_seconds
        ):
            for event in survivors:
                decisions.append({"event_id": event.event_id, "result": "dropped", "reason": "global_rate"})
            return None, decisions
        if chosen.proactive:
            self._last_output_at = now
        self._last_by_key[chosen.cooldown_key or chosen.event_id] = now
        decisions.append({"event_id": chosen.event_id, "result": "selected", "reason": "highest_priority"})
        for event in survivors:
            if event is not chosen:
                decisions.append({"event_id": event.event_id, "result": "dropped", "reason": "lost_to_higher_priority"})
        return chosen, decisions


__all__ = ["EventArbiter"]
