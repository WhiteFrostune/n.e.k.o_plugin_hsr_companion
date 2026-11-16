"""Test harness coverage only, not game recognition or cat acceptance."""
from plugin.plugins.hsr_companion.scripts.verify_page_roundtrip import baseline_state, steps


def test_one_cycle_covers_both_directions_of_three_families_and_role_subpages():
    family = {'character_detail': 'role', 'relic_detail': 'role',
              'challenge_setup': 'challenge', 'exploration_hud': 'exploration'}
    previous, edges = 'exploration', set()
    plan = steps()
    assert len({row[0] for row in plan}) == len(plan) == 8
    for _, _, _, _, _, page in plan:
        target = family[page]
        if target != previous:
            edges.add((previous, target))
        previous = target
    assert edges == {(a, b) for a in family.values() for b in family.values() if a != b}
    assert [row[0] for row in plan[:3]] == ['attributes', 'relic', 'cone']
    assert previous == 'exploration'


def test_missing_baseline_is_a_retained_failure_not_a_pass_or_a_reason_to_skip_every_other_direction():
    assert baseline_state({}) == {'expected_page': 'exploration_hud', 'observed_page': None,
                                  'confirmed': False, 'failure_retained': True}
    assert not baseline_state({'page': {'page_type': 'character_detail'}})['confirmed']
    assert baseline_state({'page': {'page_type': 'exploration_hud'}})['confirmed']
