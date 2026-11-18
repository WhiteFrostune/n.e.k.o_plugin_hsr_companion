"""2b.1: a team roster shorter than the four member positions is not complete."""
from datetime import datetime, timezone

from plugin.plugins.hsr_companion.runtime.answer_message import render_followup
from plugin.plugins.hsr_companion.runtime.contracts import team_roster_state
from plugin.plugins.hsr_companion.runtime.observation import public_visual_evidence
from plugin.plugins.hsr_companion.runtime.query_coordinator import evaluate_readiness
from plugin.plugins.hsr_companion.runtime.query_plan import build_query_plan


def member(index):
    return {"entity_id": f"character.m{index}", "name": f"队员{index}",
            "identity_scope": "team_selection"}


def roster(count):
    return [member(index) for index in range(1, count + 1)]


def plan():
    return build_query_plan("队伍里都有谁？", screen_fields=["characters"])


def test_four_names_complete_three_names_incomplete():
    assert team_roster_state("team_setup", roster(4)) == {
        "team_members_expected": 4, "team_members_read": 4, "characters_complete": True}
    assert team_roster_state("team_setup", roster(3)) == {
        "team_members_expected": 4, "team_members_read": 3, "characters_complete": False}
    assert team_roster_state("relic_detail", roster(3)) == {}


def test_short_roster_is_available_but_not_confirmed():
    visual = {"fresh": True, "page_type": "team_setup", "read_scope": "full",
              "scene": {"verified": True}, "screen_facts": {"facts": []},
              "characters": roster(3), "characters_complete": False,
              "team_members_read": 3, "team_members_expected": 4}
    status = evaluate_readiness(plan(), visual)
    assert "characters" not in status.confirmed
    assert "characters" in status.available


def test_full_frame_read_without_a_roster_marker_keeps_its_contract():
    # Only the name-region reader knows the member positions. A synthetic or
    # full-frame packet without that marker is not relabeled here.
    visual = {"fresh": True, "page_type": "team_setup", "read_scope": "full",
              "scene": {"verified": True}, "screen_facts": {"facts": []},
              "characters": roster(1)}
    assert "characters" in evaluate_readiness(plan(), visual).confirmed


def test_public_evidence_states_the_incomplete_roster():
    value = {
        "ok": True,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "source": "assistant_local_ocr",
        "page": {"page_type": "team_setup", "label": "队伍编成"},
        "scene": {"primary_state": "menu", "verified": True, "confidence": "high"},
        "runtime": {"stable_scene": "menu",
                    "stable_character_ids": [item["entity_id"] for item in roster(3)]},
        "characters": roster(3),
        "characters_complete": False,
        "team_members_read": 3,
        "team_members_expected": 4,
        "screen_facts": {"facts": []},
    }
    public = public_visual_evidence(value, {"state": "observing", "enabled": True})
    assert public["team_members_read"] == 3
    assert public["team_members_expected"] == 4
    assert public["characters_complete"] is False
    assert "名单不完整" in public["instruction"]
    assert "3/4" in public["instruction"]


def test_rendered_answer_does_not_present_three_as_the_whole_team():
    data = {"characters": roster(3), "characters_complete": False,
            "team_members_read": 3, "team_members_expected": 4,
            "screen_facts": {"facts": []}, "answer_progress": {"complete": False}}
    text, receipt = render_followup(data, plan=plan(), ready=True)
    assert "名单不完整" in text
    assert "3/4" in text
    assert receipt["complete"] is not None
