"""19:43 acceptance: real admission/entry, synthetic OCR receipts, no host writes."""
import asyncio
import copy
import importlib
import json
from types import SimpleNamespace

import pytest
from plugin.plugins.hsr_companion.adapters.text_verification import proof_key
from plugin.plugins.hsr_companion.runtime.fact_lifecycle import candidate_evidence
from plugin.plugins.hsr_companion.runtime.query_plan import build_query_plan
from plugin.plugins.hsr_companion.runtime.query_reuse import region_stamp, review_snapshot
from plugin.plugins.hsr_companion.tests.test_chain_audit_20261017 import audit_owner, commit_raw
from plugin.plugins.hsr_companion.tests.test_current_observation import analysis
from plugin.plugins.hsr_companion.tests.test_delivery_fix_20261012 import frame
from plugin.plugins.hsr_companion.tests.test_equipment_turn_delivery import control, image

ENTRY = importlib.import_module('plugin.plugins.hsr_companion')
QUESTION = '真珠这面板怎么样，接下来先养哪儿？'
SUBJECT = {'scope': 'current_panel', 'subject_state': 'specified', 'character': '真珠'}


def saved_roster():
    return {'scope': 'player_confirmed_builds', 'characters': [{
        'identity_id': 'character.pearl', 'form_id': 'character_form.1503', 'name': '真珠',
        'panel': {'state': 'provided', 'source': 'player_manual', 'values': {'def': '3132', 'spd': '143'},
                  'stats': [{'label': '防御力', 'value': '3132'}]},
        'light_cone': {'name': '献给明日的色彩', 'superimposition': 1}}]}


async def panel_owner(tmp_path, *, numeric_proved=True):
    owner, values, sent = audit_owner(tmp_path)
    controls = [control('角色详情', .03), control('光锥', .2), control('行迹', .3), control('星魂', .4)]
    literal = control('3335', .55, x=.75)
    literal.pop('control')
    raw = analysis(roles=False, scene='menu')
    raw['page'] = {'page_type': 'character_detail', 'label': '角色详情'}
    raw['screen_facts'] = {'facts': [{'kind': 'stat', 'scope': 'attributes', 'label': '防御力',
                                    'value': '3335', 'evidence': [literal]}], 'visible_text': []}
    original, latest = image('black'), image('white')
    await commit_raw(owner, raw, original)
    regions = [*controls, literal]
    guard = (regions, region_stamp(original, regions))
    owner._accepted_frame_page_regions = controls
    owner._accepted_frame_full_query_guard = guard
    owner._accepted_frame_query_guard = guard
    owner._accepted_frame_delivery_guard = guard
    snapshot = {'evidence': candidate_evidence(owner._committed_observation),
                'stamp': owner._accepted_frame_stamp, 'game_pid': 1, 'epoch': getattr(owner, '_observation_epoch', 0),
                'guard': guard, 'page_controls': controls}
    owner._reusable_observations = {'full': snapshot}
    owner._assistant_client = SimpleNamespace(fetch_frame=lambda seq: frame(latest))
    proved = {'角色详情', '光锥', '星魂'} | ({'3335'} if numeric_proved else set())
    owner._vision.verify_text_regions = lambda data, proofs, **kw: {proof_key(p) for p in proofs if p['text'] in proved}
    return owner, snapshot, values, sent


@pytest.mark.parametrize('path', ['review', 'formal'])
def test_weak_redundant_tab_cannot_revoke_the_admitted_panel_at_final_boundary(tmp_path, path):
    async def run():
        owner, snapshot, _, _ = await panel_owner(tmp_path)
        if path == 'review':
            from plugin.plugins.hsr_companion.runtime.observation_store import ObservationSnapshot
            plan = build_query_plan(QUESTION, 'assess_panel')
            selected = await review_snapshot(owner, snapshot, plan=plan)
            assert selected and selected['screen_facts']['facts']
            result = ObservationSnapshot.select(owner, selected, fields=plan.fields).delivery(owner)
        else:
            packet = await owner.hsr_companion_prepare(query=QUESTION, intent='assess_panel', consultation=SUBJECT)
            assert not packet.get('is_error'), packet
            assert packet['query_answer']['state'] == 'ready', packet.get('query_timing')
            result = packet['current_visual_evidence']
        assert result['fresh'], result.get('delivery_boundary')
        assert [f['value'] for f in result['screen_facts']['facts']] == ['3335']
        assert len(owner._accepted_frame_page_regions) == 4  # No global proof relaxation.
    asyncio.run(run())


def test_page_proof_still_cannot_certify_a_failed_number(tmp_path):
    async def run():
        owner, snapshot, _, _ = await panel_owner(tmp_path, numeric_proved=False)
        result = await review_snapshot(owner, snapshot, plan=build_query_plan(QUESTION, 'assess_panel'))
        assert not any(f.get('value') == '3335' for f in (result or {}).get('screen_facts', {}).get('facts', []))
    asyncio.run(run())


@pytest.mark.parametrize('mutation', ['used_control', 'number', 'new_page', 'empty_page_receipt'])
def test_admitted_subset_does_not_bypass_new_challenges_or_actual_page_changes(tmp_path, mutation):
    async def run():
        from plugin.plugins.hsr_companion.runtime.delivery_scope import coordinates
        from plugin.plugins.hsr_companion.runtime.observation_store import ObservationSnapshot, ObservationStore
        owner, snapshot, _, _ = await panel_owner(tmp_path)
        plan = build_query_plan(QUESTION, 'assess_panel')
        selected = await review_snapshot(owner, snapshot, plan=plan)
        if mutation == 'empty_page_receipt':
            selected['reuse']['page_control_coordinates'] = []
        detached = ObservationSnapshot.select(owner, selected, fields=plan.fields)
        if mutation in {'used_control', 'number'}:
            proof = snapshot['page_controls'][0] if mutation == 'used_control' else snapshot['evidence']['screen_facts']['facts'][0]['evidence'][0]
            ObservationStore(owner).defer_regions({coordinates(proof)})
        elif mutation == 'new_page':
            owner._committed_observation['page'] = {'page_type': 'exploration_hud', 'label': '探索'}
        result = detached.delivery(owner)
        assert not any(f.get('value') == '3335' for f in result['screen_facts']['facts'])
    asyncio.run(run())


def test_ready_current_panel_keeps_only_current_totals_while_preserving_saved_account_conditions(tmp_path):
    async def run():
        owner, _, _, _ = await panel_owner(tmp_path)
        roster = saved_roster()
        before = copy.deepcopy(roster)

        async def imported():
            return roster

        owner._imported_roster = imported
        packet = await owner.hsr_companion_prepare(query=QUESTION, intent='assess_panel', consultation=SUBJECT)
        assert packet['query_answer']['state'] == 'ready'
        assert '3335' in json.dumps(packet['guidance_answer']['assessment'], ensure_ascii=False)
        assert '3132' not in json.dumps(packet, ensure_ascii=False)
        assert packet['guidance_answer']['imported_builds']['characters'][0]['light_cone']['superimposition'] == 1
        assert roster == before
    asyncio.run(run())


@pytest.mark.parametrize('screen', [True, False])
def test_current_panel_packet_does_not_offer_saved_totals_as_a_substitute(tmp_path, monkeypatch, screen):
    async def run():
        owner, _, _ = audit_owner(tmp_path)
        roster = saved_roster()
        before = copy.deepcopy(roster)

        async def imported():
            return roster

        async def refresh(*args, **kwargs):
            return None

        owner._imported_roster = imported
        monkeypatch.setattr(ENTRY, 'refresh_for_query', refresh)
        if screen:
            packet = await owner.hsr_companion_prepare(query=QUESTION, intent='assess_panel', consultation=SUBJECT)
            assert packet['query_answer']['state'] == 'unconfirmed'
            assert '3132' not in json.dumps(packet, ensure_ascii=False)
        else:
            packet = await owner.hsr_companion_prepare(query='按我保存的资料，真珠防御多少？', intent='build_guide')
            assert '3132' in json.dumps(packet['guidance_answer']['imported_builds'], ensure_ascii=False)
        assert roster == before  # No player storage edits.
    asyncio.run(run())
