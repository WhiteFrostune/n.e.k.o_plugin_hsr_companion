"""Manual-refresh demand equivalence for the P7 planner dependency removal."""
from dataclasses import asdict

import pytest
from plugin.plugins.hsr_companion.runtime.query_plan import build_query_plan
from plugin.plugins.hsr_companion.runtime.readiness import fields_for_page


@pytest.mark.parametrize(("page", "fields"), [
    ("relic_detail", ["equipment"]), ("exploration_hud", ["location", "quest"]),
    ("character_detail", ["characters", "attributes"]), ("team_setup", ["characters"]),
])
def test_manual_page_plan_is_identical_to_previous_explicit_demand(page, fields):
    previous = build_query_plan("主副词条" if page == "relic_detail" else "", screen_fields=fields)
    current = fields_for_page(page)
    assert asdict(current) == asdict(previous)
    assert current.required_equipment_kinds == previous.required_equipment_kinds


def test_unknown_manual_page_keeps_the_existing_fallback():
    assert fields_for_page("unknown") is None
