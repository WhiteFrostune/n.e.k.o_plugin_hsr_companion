"""2c: empty team cards are not missing members; only filled cards can be unread."""
from PIL import Image, ImageDraw

from plugin.plugins.hsr_companion.detectors.team import (
    TEAM_SLOT_CARDS,
    assign_slot_names,
    slot_card_states,
)
from plugin.plugins.hsr_companion.runtime.contracts import team_roster_state


def slot(number, state="filled"):
    return {**TEAM_SLOT_CARDS[number - 1], "state": state}


def name(entity_id, center, label=None):
    return {"entity_id": entity_id, "name": label or entity_id,
            "region": {"left": center - .02, "top": .66, "right": center + .02, "bottom": .70}}


def flat():
    return Image.new("RGB", (333, 384), (205, 205, 210))


def busy():
    image = Image.new("RGB", (333, 384), (180, 180, 190))
    draw = ImageDraw.Draw(image)
    for x in range(0, 333, 12):
        draw.line((x, 0, x, 384), fill=(25, 25, 30), width=3)
    for y in range(0, 384, 12):
        draw.line((0, y, 333, y), fill=(245, 245, 245), width=2)
    return image


def test_flat_card_is_empty_and_busy_card_is_filled():
    assert slot_card_states(flat(), [TEAM_SLOT_CARDS[1]])[0]["state"] == "empty"
    assert slot_card_states(busy(), [TEAM_SLOT_CARDS[1]])[0]["state"] == "filled"


def test_three_named_slots_plus_one_empty_is_complete():
    slots = [slot(1), slot(2, "empty"), slot(3), slot(4)]
    assigned = assign_slot_names(slots, [name("a", .08), name("c", .60), name("d", .84)])
    assert [item["state"] for item in assigned] == ["filled_named", "empty", "filled_named", "filled_named"]
    roster = team_roster_state("team_setup", [], slots=assigned)
    assert roster["team_members_expected"] == 3
    assert roster["team_members_read"] == 3
    assert roster["characters_complete"] is True
    assert roster["team_roster_known"] is True


def test_filled_unread_slot_is_incomplete():
    slots = [slot(1), slot(2), slot(3), slot(4)]
    assigned = assign_slot_names(slots, [name("a", .08), name("c", .60), name("d", .84)])
    assert assigned[1]["state"] == "filled_unread"
    roster = team_roster_state("team_setup", [], slots=assigned)
    assert roster["team_members_expected"] == 4
    assert roster["team_members_read"] == 3
    assert roster["characters_complete"] is False


def test_unknown_slot_is_not_falsely_incomplete():
    slots = [slot(1), slot(2, "unknown"), slot(3), slot(4)]
    assigned = assign_slot_names(slots, [name("a", .08), name("c", .60), name("d", .84)])
    roster = team_roster_state("team_setup", [], slots=assigned)
    assert roster["characters_complete"] is True
    assert roster["team_roster_known"] is False


def test_name_far_from_every_card_is_not_assigned():
    slots = [slot(index) for index in range(1, 5)]
    assigned = assign_slot_names(slots, [name("a", .08), name("c", .60), name("d", .84), name("x", .45)])
    assert all(item.get("entity_id") != "x" for item in assigned)
    assert assigned[1]["state"] == "filled_unread"
