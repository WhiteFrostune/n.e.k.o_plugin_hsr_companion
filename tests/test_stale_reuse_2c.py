"""2c: age policy, stale wording, and unknown-page clarification."""
from datetime import datetime, timedelta, timezone

from plugin.plugins.hsr_companion.runtime.answer_message import render_followup
from types import SimpleNamespace

from plugin.plugins.hsr_companion.runtime.fact_lifecycle import candidate_evidence, current_evidence
from plugin.plugins.hsr_companion.runtime.observation import public_visual_evidence
from plugin.plugins.hsr_companion.detectors.team import TEAM_SLOT_CARDS
from plugin.plugins.hsr_companion.runtime.observation_store import attach_team_roster, richer_last_successful
from plugin.plugins.hsr_companion.runtime.query_coordinator import answer_status
from plugin.plugins.hsr_companion.runtime.query_plan import build_query_plan
from plugin.plugins.hsr_companion.runtime.query_reuse import prefer_richer_roster, stale_state


def plan(query="队伍里都有谁？", fields=("characters",)):
    return build_query_plan(query, screen_fields=list(fields))


def test_stale_age_policy_boundaries():
    assert stale_state("team_setup", 0) == "fresh"
    assert stale_state("team_setup", 15) == "fresh"
    assert stale_state("team_setup", 15.1) == "stale"
    assert stale_state("exploration_hud", 15.1) == "not_allowed"
    assert stale_state("exploration_hud", 15.1, allow_stale=True) == "stale"
    assert stale_state("team_setup", 60.1) == "expired"


def test_stale_fallback_prefers_the_richer_roster():
    def snapshot(*ids):
        return {"evidence": {"characters": [{"entity_id": item} for item in ids]}}
    one, three = snapshot("a"), snapshot("a", "b", "c")
    ordered = prefer_richer_roster([one, three], plan(), allow_stale=True)
    assert ordered[0] is three
    assert prefer_richer_roster([one, three], plan(), allow_stale=False)[0] is one


def test_public_evidence_does_not_prefix_the_age_twice():
    now = datetime.now(timezone.utc)
    label = "这是 20 秒前的画面，现在可能变了"
    value = {
        "ok": True, "source": "assistant_local_ocr", "freshness": "stale",
        "observed_at": (now - timedelta(seconds=20)).isoformat(),
        "message": label + "：已用新采集画面核对；未确认的字段仍未知。",
        "page": {"page_type": "team_setup"}, "scene": {"primary_state": "menu", "verified": True, "confidence": "high"},
        "runtime": {"stable_scene": "menu", "stable_character_ids": ["character.a"]},
        "characters": [{"entity_id": "character.a", "name": "甲", "identity_scope": "team_selection"}],
        "screen_facts": {"facts": []},
    }
    public = public_visual_evidence(value, {"state": "observing", "enabled": True}, now=now)
    assert public["message"].count(public["age_label"]) == 1


def test_candidate_evidence_uses_real_age():
    now = datetime.now(timezone.utc)
    base = {
        "ok": True, "source": "assistant_local_ocr",
        "page": {"page_type": "team_setup", "label": "队伍编成"},
        "scene": {"primary_state": "menu", "verified": True, "confidence": "high"},
        "runtime": {"stable_scene": "menu", "stable_character_ids": ["character.a"]},
        "characters": [{"entity_id": "character.a", "name": "甲", "identity_scope": "team_selection"}],
        "screen_facts": {"facts": []},
    }
    stale = candidate_evidence({**base, "observed_at": (now - timedelta(seconds=20)).isoformat()})
    assert stale["freshness"] == "stale" and len(stale["characters"]) == 1
    assert stale["age_seconds"] >= 20
    assert stale["message"].startswith(stale["age_label"])


def test_old_roster_uses_last_slots_to_mark_incomplete():
    def member(center):
        return {"entity_id": str(center), "name": str(center),
                "region": {"left": center - .02, "top": .66, "right": center + .02, "bottom": .70}}
    slots = [{"slot": index, "state": "filled_named",
              "region": TEAM_SLOT_CARDS[index - 1]["region"]} for index in range(1, 5)]
    latest = {"page": {"page_type": "team_setup"}, "characters": [member(.08), member(.62)]}
    attach_team_roster(latest, slots)
    assert latest["team_members_expected"] == 4 and latest["team_members_read"] == 2
    assert latest["characters_complete"] is False

    three = [{"slot": 1, "state": "filled_named", "region": TEAM_SLOT_CARDS[0]["region"]},
             {"slot": 2, "state": "empty", "region": TEAM_SLOT_CARDS[1]["region"]},
             {"slot": 3, "state": "filled_named", "region": TEAM_SLOT_CARDS[2]["region"]},
             {"slot": 4, "state": "filled_named", "region": TEAM_SLOT_CARDS[3]["region"]}]
    latest = {"page": {"page_type": "team_setup"},
              "characters": [member(.08), member(.62), member(.84)]}
    attach_team_roster(latest, three)
    assert latest["team_members_expected"] == 3 and latest["team_members_read"] == 3
    assert latest["characters_complete"] is True


def test_richer_last_successful_prefers_a_same_page_full_read():
    now = datetime.now(timezone.utc)
    latest = {"page": {"page_type": "team_setup"}, "observed_at": now.isoformat(),
              "characters": [{"entity_id": "a"}, {"entity_id": "b"}]}
    previous = {"page": {"page_type": "team_setup"}, "observed_at": (now - timedelta(seconds=30)).isoformat(),
                "characters": [{"entity_id": item} for item in "abcd"]}
    assert richer_last_successful(latest, previous, now=now) is previous
    old = {**previous, "observed_at": (now - timedelta(seconds=61)).isoformat()}
    assert richer_last_successful(latest, old, now=now) is latest


def test_current_evidence_never_delivers_past_the_hard_cap():
    now = datetime.now(timezone.utc)
    stamp = (now - timedelta(seconds=61)).isoformat()
    raw = {
        "ok": True, "source": "assistant_local_ocr", "observed_at": stamp,
        "page": {"page_type": "team_setup"}, "scene": {"primary_state": "menu", "verified": True, "confidence": "high"},
        "runtime": {"stable_scene": "menu", "stable_character_ids": ["character.a"]},
        "characters": [{"entity_id": "character.a", "name": "甲", "identity_scope": "team_selection"}],
        "screen_facts": {"facts": []},
    }
    owner = SimpleNamespace(
        _observation_epoch=0, _observation_serial=0,
        _live_field_verification={"identity": (0, 0, stamp), "regions": {}},
        _observer_status={"enabled": True, "state": "observing"})
    value = current_evidence(owner, raw, {"enabled": True, "state": "observing"})
    assert value["freshness"] == "unavailable"
    assert value["characters"] == []


def test_public_evidence_labels_a_twenty_second_old_snapshot():
    now = datetime.now(timezone.utc)
    value = {
        "ok": True, "source": "assistant_local_ocr",
        "observed_at": (now - timedelta(seconds=20)).isoformat(),
        "page": {"page_type": "team_setup", "label": "队伍编成"},
        "scene": {"primary_state": "menu", "verified": True, "confidence": "high"},
        "runtime": {"stable_scene": "menu", "stable_character_ids": ["character.a"]},
        "characters": [{"entity_id": "character.a", "name": "甲", "identity_scope": "team_selection"}],
        "screen_facts": {"facts": []},
    }
    public = public_visual_evidence(value, {"state": "observing", "enabled": True}, now=now)
    assert public["freshness"] == "stale"
    assert public["age_seconds"] == 20
    assert "20 秒前的画面" in public["age_label"]
    assert public["message"].startswith(public["age_label"])
    assert "不得说成现在" in public["instruction"]


def test_sixty_second_cap_hides_the_old_identity():
    now = datetime.now(timezone.utc)
    value = {
        "ok": True, "source": "assistant_local_ocr",
        "observed_at": (now - timedelta(seconds=61)).isoformat(),
        "page": {"page_type": "team_setup"},
        "scene": {"primary_state": "menu", "verified": True, "confidence": "high"},
        "runtime": {"stable_scene": "menu", "stable_character_ids": ["character.a"]},
        "characters": [{"entity_id": "character.a", "name": "甲", "identity_scope": "team_selection"}],
        "screen_facts": {"facts": []},
    }
    public = public_visual_evidence(value, {"state": "observing", "enabled": True}, now=now)
    assert public["freshness"] == "unavailable"
    assert public["characters"] == []


def test_answer_status_carries_age():
    visual = {"fresh": True, "page_type": "team_setup", "freshness": "stale",
              "age_seconds": 20, "age_label": "这是 20 秒前的画面，现在可能变了",
              "scene": {"verified": True}, "screen_facts": {"facts": []},
              "characters": [{"name": "甲"}]}
    status = answer_status(plan(), visual, {"enabled": True, "state": "observing"})
    assert status["freshness"] == "stale"
    assert status["age_seconds"] == 20


def test_followup_starts_with_the_age_and_never_says_now():
    data = {"characters": [{"name": "甲"}], "freshness": "stale", "age_seconds": 20,
            "age_label": "这是 20 秒前的画面，现在可能变了", "screen_facts": {"facts": []}}
    text, receipt = render_followup(data, plan=plan(), ready=True)
    assert "20 秒前的画面" in text
    assert receipt["complete"] is not None


def test_unknown_page_uses_the_clarification_not_a_page_guess():
    data = {"page_type": "unknown", "characters": [], "screen_facts": {"facts": []}, "scene": {}}
    text, _ = render_followup(data, plan=plan("现在是什么界面", ("page",)), ready=True)
    assert "这个界面我还看不太懂" in text


def test_known_page_with_missing_names_does_not_use_the_unknown_page_line():
    data = {"page_type": "team_setup", "characters": [], "screen_facts": {"facts": []}, "scene": {}}
    text, _ = render_followup(data, plan=plan(), ready=True)
    assert "这个界面我还看不太懂" not in text


def test_invalidated_observation_is_unavailable_not_a_zero_person_answer():
    public = public_visual_evidence({"invalidated": True, "source": "assistant_local_ocr"},
                                    {"state": "observing", "enabled": True})
    assert public["freshness"] == "unavailable"
    assert public["characters"] == []
    text, _ = render_followup({**public, "screen_facts": {"facts": []}}, plan=plan(), ready=True)
    assert "角色：0" not in text and "0 人" not in text
    assert "这个界面我还看不太懂" in text
