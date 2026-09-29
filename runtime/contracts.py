from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CompanionEvent:
    event_id: str
    kind: str
    priority: int
    ts: float
    payload: dict[str, Any] = field(default_factory=dict)
    max_age_seconds: float = 8.0
    cooldown_seconds: float = 8.0
    cooldown_key: str = ""
    proactive: bool = False
    preempt: bool = False


__all__ = ["CompanionEvent"]
