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
FRESH_LIVE_AGE_SECONDS = 15.0
STALE_MAX_AGE_SECONDS = 60.0
LIVE_PAGE_TYPES = frozenset({"exploration_hud", "map_or_quest", "combat", "story_dialogue"})


def team_roster_state(page_type: Any, characters: Any, slots: Any = None) -> dict[str, Any]:
    """Team setup has four positions; empty cards are not missing members.

    With slot states, completeness is "every filled card has a name"; a
    three-member team is complete, while a filled card without a name is
    incomplete. Without slots, keep the conservative four-position rule.
    """
    if page_type != "team_setup":
        return {}
    if slots:
        filled = [slot for slot in slots if slot.get("state") in {"filled_named", "filled_unread"}]
        read = sum(1 for slot in slots if slot.get("state") == "filled_named")
        unread = [slot for slot in slots if slot.get("state") == "filled_unread"]
        unknown = [slot for slot in slots if slot.get("state") == "unknown"]
        return {
            "team_members_expected": len(filled),
            "team_members_read": read,
            "characters_complete": not unread,
            "team_roster_known": not unknown,
            "team_slots": [{key: slot.get(key) for key in ("slot", "state", "name") if slot.get(key) is not None}
                           for slot in slots],
        }
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


__all__ = ["CompanionEvent", "TEAM_MEMBER_SLOTS", "FRESH_LIVE_AGE_SECONDS",
           "STALE_MAX_AGE_SECONDS", "LIVE_PAGE_TYPES", "team_roster_state"]
