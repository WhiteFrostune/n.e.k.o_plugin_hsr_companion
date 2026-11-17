"""2b: regions come from fact evidence; one native read per derived region."""
from types import SimpleNamespace

import pytest
from PIL import Image

from plugin.plugins.hsr_companion.adapters.text_reader import read_region
from plugin.plugins.hsr_companion.runtime.regions import cached_region_plan, region_plan, remember_region_plan


def fact(kind, left, top, right, bottom):
    return {"kind": kind, "evidence": [{"region": {"left": left, "top": top, "right": right, "bottom": bottom}}]}


def plan(*fields):
    return SimpleNamespace(fields=set(fields))


def test_team_name_strip_comes_from_entity_label_evidence():
    visual = {"page": {"page_type": "team_setup"},
              "screen_facts": {"facts": [fact("entity_label", .06, .694, .846, .775)]}}
    selected = region_plan(visual, plan("characters"))
    assert selected["page_type"] == "team_setup"
    assert selected["regions"][0]["fields"] == ["characters"]
    assert selected["regions"][0]["region"] == pytest.approx(
        {"left": .057, "top": .691, "right": .849, "bottom": .778})


def test_exploration_quest_and_location_map_to_their_evidence():
    visual = {"page": {"page_type": "exploration_hud"},
              "screen_facts": {"facts": [fact("quest_objective", .04, .26, .18, .32),
                                         fact("current_location", .1, .02, .4, .08)]}}
    selected = region_plan(visual, plan("location", "quest"))
    fields = {tuple(item["fields"]) for item in selected["regions"]}
    assert fields == {("location",), ("quest",)}


def test_relic_equipment_region_includes_panel_controls_and_rows():
    visual = {"page": {"page_type": "relic_detail"},
              "screen_facts": {"facts": [fact("equipment_slot", .55, .2, .65, .3),
                                         fact("main_stat", .65, .25, .95, .4),
                                         fact("sub_stat", .65, .4, .95, .6)]}}
    selected = region_plan(visual, plan("equipment"))
    assert selected["page_type"] == "relic_detail"
    assert selected["regions"][0]["region"]["left"] < .6
    assert selected["regions"][0]["region"]["bottom"] > .59


def test_unknown_page_has_no_region():
    visual = {"page": {"page_type": "warp"}, "screen_facts": {"facts": [fact("entity_label", .1, .1, .2, .2)]}}
    assert region_plan(visual, plan("characters")) is None


def test_cached_region_only_reuses_the_same_page_and_fields():
    owner = SimpleNamespace()
    visual = {"page": {"page_type": "team_setup"}}
    remember_region_plan(owner, {"page_type": "team_setup", "regions": [
        {"fields": ["characters"], "region": {"left": .1, "top": .2, "right": .3, "bottom": .4}}]})
    assert cached_region_plan(owner, visual, plan("characters"))["regions"]
    assert cached_region_plan(owner, {"page": {"page_type": "warp"}}, plan("characters")) is None
    assert cached_region_plan(owner, visual, plan("equipment")) is None


def test_region_read_offsets_boxes_to_full_frame():
    class Backend:
        def __init__(self):
            self.size = None

        def extract_text_with_boxes(self, image):
            self.size = image.size
            return "", [{"text": "名字", "score": .9, "left": 0, "top": 0, "right": 5, "bottom": 5}]

    backend = Backend()
    boxes = read_region(backend, Image.new("RGB", (100, 100)),
                        {"left": .1, "top": .2, "right": .5, "bottom": .6})
    assert backend.size == (40, 40)
    assert boxes[0]["left"] == 10 and boxes[0]["top"] == 20
    assert boxes[0]["right"] == 15 and boxes[0]["bottom"] == 25
