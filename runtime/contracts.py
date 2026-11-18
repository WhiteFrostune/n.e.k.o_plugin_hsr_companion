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


TEAM_MEMBER_SLOTS = 4


def team_roster_state(page_type: Any, characters: Any) -> dict[str, Any]:
    """Team setup has four member positions; fewer names is an incomplete list.

    Empty positions and unread names are not distinguishable from the strip
    alone, so a short list may never be delivered as the complete team.
    """
    if page_type != "team_setup":
        return {}
    seen = {
        item.get("entity_id") or item.get("name")
        for item in characters or []
        if isinstance(item, dict)
        and item.get("identity_scope") in {None, "team_selection"}
        and (item.get("entity_id") or item.get("name"))
    }
    read = len(seen)
    return {
        "team_members_expected": TEAM_MEMBER_SLOTS,
        "team_members_read": read,
        "characters_complete": read >= TEAM_MEMBER_SLOTS,
    }


__all__ = ["CompanionEvent", "TEAM_MEMBER_SLOTS", "team_roster_state"]
