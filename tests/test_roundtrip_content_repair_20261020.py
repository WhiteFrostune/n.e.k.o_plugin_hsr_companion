"""A+B: retained real inputs, real shaping, simulated admission/IPC where noted.

No OCR, helper, host messages, installation or player-store writes. These
cases do NOT certify C's cross-page scheduling or the cat's paraphrase.
"""
import asyncio
import copy
import json
import socket
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from plugin.plugins.hsr_companion.detectors.exploration import distinct_rows, task_stacks
from plugin.plugins.hsr_companion.detectors.layout import classify_supported_page, scene_control_regions
from plugin.plugins.hsr_companion.runtime import answer_message, followup
from plugin.plugins.hsr_companion.runtime.answer_message import render_followup
from plugin.plugins.hsr_companion.runtime.consultation_contract import validate_declaration
from plugin.plugins.hsr_companion.runtime.delivery_scope import project_evidence
from plugin.plugins.hsr_companion.runtime.guidance import build_guidance
from plugin.plugins.hsr_companion.runtime.query_plan import build_query_plan
from plugin.plugins.hsr_companion.runtime.screen_facts import extract_screen_facts
from plugin.plugins.hsr_companion.runtime.semantic_guidance import plan_semantic_guidance
from plugin.plugins.hsr_companion.scripts.replay_roundtrip_breakpoints import historical_visual, phase_events, phase_row
from plugin.plugins.hsr_companion.tests import test_guidance as cases
from plugin.plugins.hsr_companion.tests.test_page_followup import followup_owner

ROOT = Path(__file__).resolve().parents[1]
book = cases.book
catalog = cases.catalog
HOST_PREVIEW = answer_message.bridge_preview


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    # Initialize Windows' internal socketpair BEFORE denying all connections.
    with asyncio.Runner() as runner:
        runner.get_loop()
        monkeypatch.setattr(socket.socket, 'connect', lambda *a, **kw: (_ for _ in ()).throw(AssertionError('offline only')))
        monkeypatch.setattr('plugin.plugins.hsr_companion.runtime.answer_message.bridge_preview', lambda text: text)
        yield runner


@pytest.fixture(scope='module')
def records():
    return json.loads((ROOT / 'dist/page-roundtrip-live-20261020-2.json').read_text(encoding='utf-8'))


@pytest.mark.parametrize('phase', ['challenge_from_cone', 'challenge_from_exploration'])
def test_moc_retained_inputs_and_control_guards(records, phase):
    for event in phase_events(records, phase, 'ocr_to_parser'):
        boxes = event['boxes']
        page = classify_supported_page('\n'.join(b['text'] for b in boxes), boxes, width=2560, height=1600)
        assert page['page_type'] == 'challenge_setup'
        controls = scene_control_regions(page, boxes, width=2560, height=1600)
        assert {'忘却之庭', '挑战目标'} <= {c['text'] for c in controls}


@pytest.mark.parametrize('mutation', ['no_title', 'footer', 'prose', 'one_control', 'unconfirmed_title'])
def test_moc_needs_independent_title_and_controls(records, mutation):
    boxes = copy.deepcopy(phase_events(records, 'challenge_from_cone', 'ocr_to_parser')[0]['boxes'])
    title = next(b for b in boxes if b['text'] == '忘却之庭')
    if mutation == 'no_title':
        boxes.remove(title)
    elif mutation == 'footer':
        title.update(top=1400, bottom=1440)
    elif mutation == 'prose':
        title['text'] = '前往忘却之庭参与挑战目标'
    elif mutation == 'unconfirmed_title':
        title['literal_unconfirmed'] = True
    else:
        boxes = [b for b in boxes if b['text'] not in {'预设编队', '角色列表'}]
    assert classify_supported_page('\n'.join(b['text'] for b in boxes), boxes, width=2560, height=1600)['page_type'] != 'challenge_setup'


def test_retained_task_prefix_survives_and_extracts_full_objective(records, catalog):
    boxes = phase_events(records, 'exploration_from_attributes', 'ocr_to_parser')[0]['boxes']
    stacks = task_stacks(boxes, width=2560, height=1600)
    assert len(stacks) == 1 and stacks[0][1]['text'] == '询问哈托努斯关于宁芙的下落'
    parsed = extract_screen_facts(boxes, width=2560, height=1600, page={'page_type': 'exploration_hud'}, catalog=catalog)
    assert any(f['kind'] == 'quest_objective' and f['value'] == stacks[0][1]['text'] for f in parsed['facts'])


@pytest.mark.parametrize('mutation', ['conflict', 'shifted', 'low_score', 'shorter_wins'])
def test_prefix_protection_counterexamples(mutation):
    long = dict(text='询问哈托努斯关于宁芙的下落', score=.998, left=106, top=424, right=475, bottom=460)
    short = dict(text='询问哈托努斯关于宁芙的下', score=.987, left=106.5, top=427, right=454, bottom=458.5)
    if mutation == 'conflict':
        short['text'] = '询问其他人关于完全不同任务'
    elif mutation == 'shifted':
        short.update(left=125, right=473)
    elif mutation == 'low_score':
        long['score'] = .89
        short['score'] = .92
    else:
        short['score'] = .999
    assert distinct_rows([long, short]) == []


def request_for(plan, *, delivered=frozenset(), duration=1):
    return followup.AnswerRequest('offline-ab', plan.query, '猫娘', time.time(), time.monotonic(),
        time.monotonic() + duration, 'unknown', plan.scene_only, plan.navigation, plan.content, False,
        plan=plan, delivered_fields=delivered)


def test_partial_location_is_delivered_once_then_full_or_expiry(records, monkeypatch, offline):
    summary = phase_events(records, 'exploration_from_attributes', 'store_commit')[-1]['observation']
    visual = historical_visual(summary)
    plan = build_query_plan('现在在哪，任务目标是什么？', 'read_screen', screen_fields=['location', 'quest'])
    owner, sent = followup_owner(monkeypatch, value=visual)
    req = request_for(plan, duration=.08)
    owner._pending_answer = req
    offline.run(followup.deliver_followup(owner, req))
    assert len(sent) == 1 and sent[0]['metadata']['outcome'] == 'partial'
    assert '观景车厢' in sent[0]['parts'][0]['text']
    assert not sent[0]['metadata']['message_projection']['answer_complete']
    assert owner._followup_status['state'] == 'expired'  # No extra failure speech.


@pytest.mark.parametrize('kind', ['already_delivered', 'unverified', 'expired'])
def test_partial_has_no_bypass_or_duplicate_notice(records, monkeypatch, offline, kind):
    visual = historical_visual(phase_events(records, 'exploration_from_attributes', 'store_commit')[-1]['observation'])
    plan = build_query_plan('现在在哪，任务目标是什么？', 'read_screen', screen_fields=['location', 'quest'])
    owner, sent = followup_owner(monkeypatch, value=visual)
    req = request_for(plan, delivered=frozenset({'location'}) if kind == 'already_delivered' else frozenset(),
                      duration=-1 if kind == 'expired' else .08)
    if kind == 'unverified':
        async def rejected(*a, **kw):
            return None
        monkeypatch.setattr(followup, 'validated_delivery', rejected)
    owner._pending_answer = req
    offline.run(followup.deliver_followup(owner, req))
    assert sent == []


@pytest.mark.parametrize('channel', ['field_projection', 'host_formatter'])
def test_cone_name_and_shared_sourced_effect_survive(records, monkeypatch, channel):
    if channel == 'host_formatter':
        monkeypatch.setattr(answer_message, 'bridge_preview', HOST_PREVIEW)
    from plugin.plugins.hsr_companion.data import ExternalKnowledgeCatalog
    registry = json.loads((ROOT / 'resources/character_registry.json').read_text(encoding='utf-8'))
    catalog = ExternalKnowledgeCatalog(registry=registry)
    assert catalog.get('light_cone.23055'), 'This saved-run replay requires its installed read-only knowledge pack'
    summary = phase_events(records, 'cone', 'store_commit')[-1]['observation']
    visual = historical_visual(summary)
    visual['screen_facts']['facts'] += [{'kind': 'entity_label', 'scope': 'displayed_label',
        'entity_type': 'light_cone', **label} for label in summary['equipment_labels']]
    plan = build_query_plan('这把光锥叫什么，效果是什么？', 'read_screen', screen_fields=['equipment'])
    data = followup.reply_data(visual, request_for(plan), catalog=catalog)
    body, receipt = render_followup(data, plan=plan, ready=True)
    assert '献给明日的色彩' in body and receipt['has_content'] and receipt['message_rows'] > 0
    assert any(r['id'] == 'light_cone.23055' for r in data['grounding']['knowledge_results'])
    assert '资料效果' in body and '防御力' in body
    assert receipt['complete'], receipt


def test_empty_notification_is_not_success():
    plan = build_query_plan('这把光锥叫什么？', 'read_screen', screen_fields=['equipment'])
    _, receipt = render_followup({}, plan=plan, ready=True)
    assert not receipt['complete'] and not receipt['has_content'] and not receipt['answer_complete']


@pytest.mark.parametrize('subject', [None, '绯英', '真珠'])
def test_panel_owner_dependency_through_projection(records, book, catalog, subject):
    visual = copy.deepcopy(phase_row(records, 'attributes_from_challenge')['packet']['current_visual_evidence'])
    if subject:
        role = catalog.resolve_character(subject)
        visual['characters'] = [{'entity_id': role['id'], 'name': role['name'], 'identity_scope': 'detail_subject'}]
    base = build_query_plan('真珠这面板怎么样，接下来先养哪儿？', 'assess_panel')
    projected = project_evidence(visual, base)
    declaration = validate_declaration({'scope': 'current_panel', 'subject_state': 'specified', 'character': '真珠'}, intent=base.intent)
    plan = plan_semantic_guidance(base.query, base, catalog=catalog, book=book, declaration=declaration)
    guidance = build_guidance(plan, base.query, book=book, catalog=catalog, profile={}, visual=projected)
    completion = guidance['completion']
    assert completion['reading_complete']
    assert completion['evaluation_complete'] == (subject == '真珠')
    assert completion['panel_subject_state'] == ('confirmed' if subject == '真珠' else 'mismatch' if subject else 'unconfirmed')
    assert projected['screen_facts']['facts']  # Still report correct unassigned numbers.


def test_full_relic_rows_and_missing_main_protection(records):
    from plugin.plugins.hsr_companion.runtime.equipment_view import equipment_card
    from plugin.plugins.hsr_companion.runtime.query_coordinator import satisfied_fields
    visual = historical_visual(phase_events(records, 'relic', 'store_commit')[-1]['observation'])
    plan = build_query_plan('完整报出主词条和四项副词条。', 'read_screen', screen_fields=['equipment'])
    req = request_for(plan)
    body, receipt = render_followup(followup.reply_data(visual, req), plan=plan, ready=followup.answer_ready(visual, req))
    assert receipt['required_rows'] == receipt['message_rows'] == 5
    card = equipment_card(visual)
    assert all(r['label'] + r['value'] in body for r in [card['main_stat'], *card['sub_stats']])
    visual['screen_facts']['facts'] = [f for f in visual['screen_facts']['facts'] if f['kind'] != 'main_stat']
    assert 'equipment' not in satisfied_fields(plan, visual)


def test_partial_progress_then_completion_does_not_repeat_partial(records, monkeypatch, offline):
    visual = historical_visual(phase_events(records, 'exploration_from_attributes', 'store_commit')[-1]['observation'])
    plan = build_query_plan('现在在哪，任务目标是什么？', 'read_screen', screen_fields=['location', 'quest'])
    owner, sent = followup_owner(monkeypatch, value=visual)
    req = request_for(plan)
    calls = 0

    async def current():
        nonlocal calls
        calls += 1
        packet = copy.deepcopy(visual)
        if calls >= 3:
            packet['screen_facts']['facts'].append({'kind': 'quest_objective', 'scope': 'quest', 'value': '询问哈托努斯关于宁芙的下落'})
        return packet

    owner._current_visual_evidence = current
    owner._pending_answer = req
    offline.run(followup.deliver_followup(owner, req))
    assert [s['metadata']['outcome'] for s in sent] == ['partial', 'ready']
    assert owner._pending_answer is None
    assert req.expires_at - req.armed_at <= 1.01


def test_partial_recovered_cache_is_reselected_as_new_progress(records, monkeypatch, offline):
    visual = historical_visual(phase_events(records, 'exploration_from_attributes', 'store_commit')[-1]['observation'])
    plan = build_query_plan('现在在哪，任务目标是什么？', 'read_screen', screen_fields=['location', 'quest'])
    owner, sent = followup_owner(monkeypatch, value={'fresh': False})
    owner._reusable_observations = {'simulated-admitted-cache': True}
    req = request_for(plan, duration=.08)
    owner._pending_answer = req

    async def recovered(*a, **kw):
        return visual  # Simulated cache admission, never a real pixel receipt.

    monkeypatch.setattr('plugin.plugins.hsr_companion.runtime.query_reuse.try_reuse', recovered)
    offline.run(followup.deliver_followup(owner, req))
    assert len(sent) == 1 and sent[0]['metadata']['outcome'] == 'partial'
    assert '观景车厢' in sent[0]['parts'][0]['text']


@pytest.mark.parametrize('proof', [True, False])
def test_panel_identity_proof_is_scoped_with_numbers_or_withheld_alone(proof):
    from plugin.plugins.hsr_companion.runtime.delivery_scope import coordinates, scoped_guard
    def region(y):
        return {'left': .1, 'right': .2, 'top': y, 'bottom': y + .02}
    title = {'text': '角色详情', 'region': region(.1), 'control': True}
    literal = {'text': '3335', 'region': region(.3)}
    actor = {'entity_id': 'character.pearl', 'name': '真珠', 'identity_scope': 'detail_subject', 'region': region(.2)}
    visual = {'fresh': True, 'page_type': 'character_detail', 'scene': {'verified': True}, 'characters': [actor],
        'screen_facts': {'observation_serial': 1, 'facts': [{'kind': 'stat', 'scope': 'attributes',
            'label': '防御力', 'value': '3335', 'evidence': [literal]}]}}
    owner = SimpleNamespace(_committed_observation={'observation_serial': 1, 'characters': [actor] if proof else []},
                            _accepted_frame_identity_guarded=True, _accepted_frame_page_regions=[title])
    regions = [title, literal, actor] if proof else [title, literal]
    guard = (regions, [(coordinates(p), b'placeholder-only-coverage-not-live-proof') for p in regions])
    selected = scoped_guard(owner, visual, build_query_plan('真珠面板怎么样', 'assess_panel'), [guard])
    assert selected and selected[0]['screen_facts']['facts'][0]['value'] == '3335'
    assert bool(selected[0]['characters']) == proof
    assert (coordinates(actor) in {coordinates(p) for p in selected[1][0]}) == proof


@pytest.mark.parametrize('subject', [None, '绯英', '真珠'])
def test_formal_entry_preserves_owner_and_reading_assessment_separation(records, tmp_path, monkeypatch, catalog, subject, offline):
    from plugin.plugins.hsr_companion.tests.test_d1_subject_contract_20261020 import formal_fixture
    visual = copy.deepcopy(phase_row(records, 'attributes_from_challenge')['packet']['current_visual_evidence'])
    # Replay fields through the simulated admission fixture, NOT another
    # owner's old epoch/generation receipt. No historical proof is renewed.
    for key in ('reuse', 'handoff', 'delivery_boundary'):
        visual.pop(key, None)
    if subject:
        role = catalog.resolve_character(subject)
        visual['characters'] = [{'entity_id': role['id'], 'name': role['name'], 'identity_scope': 'detail_subject'}]
    owner, _, sent, _ = formal_fixture(tmp_path, monkeypatch, catalog, visual)
    result = offline.run(owner.hsr_companion_prepare(query='真珠这面板怎么样，接下来先养哪儿？', intent='assess_panel',
        consultation={'scope': 'current_panel', 'subject_state': 'specified', 'character': '真珠'}))
    assert result['query_answer']['state'] == 'ready'  # Numeric reading only.
    assert result['guidance_answer']['completion']['evaluation_complete'] == (subject == '真珠')
    assert result['delivery_receipt']['assessment']['evaluation_complete'] == (subject == '真珠')
    assert not sent


def test_changed_panel_owner_revokes_assessment_not_unassigned_numbers(monkeypatch):
    from plugin.plugins.hsr_companion.runtime.observation_store import ObservationSnapshot
    value = {'fresh': True, 'page_type': 'character_detail', 'scene': {'verified': True},
        'characters': [{'entity_id': 'character.pearl', 'name': '真珠', 'identity_scope': 'detail_subject'}],
        'screen_facts': {'observation_serial': 1, 'facts': [{'kind': 'stat', 'scope': 'attributes',
            'label': '防御力', 'value': '3335'}]}}
    owner = SimpleNamespace(_observation_epoch=0, _observation_serial=1, _answer_query_generation=1,
                            _observer_status={'enabled': True, 'state': 'observing'})
    snapshot = ObservationSnapshot.select(owner, value, fields={'attributes'})
    newer = copy.deepcopy(value)
    newer['characters'] = [{'entity_id': 'character.evanescia', 'name': '绯英', 'identity_scope': 'detail_subject'}]
    owner._observation_serial = 2
    owner._committed_observation = {'observation_serial': 2}
    # Deterministic boundary counterexample, not an OCR/admission substitute.
    monkeypatch.setattr('plugin.plugins.hsr_companion.runtime.fact_lifecycle.candidate_evidence', lambda raw: newer)
    assert not snapshot.matches(owner)
    result = snapshot.delivery(owner)
    assert result['characters'] == []
    assert result['screen_facts']['facts'][0]['value'] == '3335'


def test_multiple_cones_do_not_borrow_one_cones_effect():
    plan = build_query_plan('这把光锥效果是什么？', 'read_screen', screen_fields=['equipment'])
    facts = [{'kind': 'entity_label', 'scope': 'displayed_label', 'entity_type': 'light_cone',
              'entity_id': key, 'label': '光锥', 'value': key} for key in ('one', 'two')]
    body, _ = render_followup({'screen_facts': {'facts': facts}, 'grounding': {
        'needs_target_clarification': True, 'knowledge_results': [{'id': 'one', 'sources': ['fixture'],
        'rank_effect': {'desc': '不可借给另一个对象的效果'}}]}}, plan=plan, ready=True)
    assert '不可借给' not in body
