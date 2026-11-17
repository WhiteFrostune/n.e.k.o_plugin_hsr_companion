"""Regression cases initially RED on the retained real inputs.

Failures are intentional defect reproductions. No screenshots, OCR, helper,
host chat, native worker, or production-store write is involved.
"""
import asyncio
import copy
import json
import os
import socket
import time
from pathlib import Path

import pytest
from plugin.plugins.hsr_companion.data import ExternalKnowledgeCatalog
from plugin.plugins.hsr_companion.detectors.exploration import distinct_rows, task_stacks
from plugin.plugins.hsr_companion.detectors.layout import classify_supported_page
from plugin.plugins.hsr_companion.runtime.answer_message import render_followup
from plugin.plugins.hsr_companion.runtime.consultation_contract import validate_declaration
from plugin.plugins.hsr_companion.runtime.equipment_view import equipment_card
from plugin.plugins.hsr_companion.runtime.followup import AnswerRequest, answer_ready, reply_data
from plugin.plugins.hsr_companion.runtime.guidance import build_guidance, load_guidance_catalog
from plugin.plugins.hsr_companion.runtime.query_coordinator import satisfied_fields
from plugin.plugins.hsr_companion.runtime.query_plan import build_query_plan
from plugin.plugins.hsr_companion.runtime.semantic_guidance import plan_semantic_guidance
from plugin.plugins.hsr_companion.scripts.replay_roundtrip_breakpoints import (
    historical_visual,
    phase_events,
    phase_row,
)

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.environ.get('HSR_ROUNDTRIP_DIAGNOSTIC_RED') != '1',
    reason='Opt-in saved-input regression; original RED reports remain immutable')


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Offline contract tests must not connect anywhere')
    # Windows event-loop plumbing precedes the network prohibition.
    with asyncio.Runner() as runner:
        runner.get_loop()
        monkeypatch.setattr(socket.socket, 'connect', forbidden)
        yield runner


@pytest.fixture
def local_text_channel(monkeypatch):
    # Field projection only, not host token capacity. The first diagnostic
    # batch hit a missing tokenizer cache in the sandbox; retain its failure
    # instead of confusing it with a relic-row regression. Real run receipts
    # separately show the five-row relic and the empty cone notification.
    monkeypatch.setattr('plugin.plugins.hsr_companion.runtime.answer_message.bridge_preview', lambda text: text)


@pytest.fixture(scope='module')
def records():
    return json.loads((ROOT / 'dist/page-roundtrip-live-20261020-2.json').read_text(encoding='utf-8'))


@pytest.fixture(scope='module')
def diagnosis():
    return json.loads((ROOT / 'dist/page-roundtrip-breakpoints-20261020-3.json').read_text(encoding='utf-8'))


@pytest.mark.parametrize('phase', ['challenge_from_cone', 'challenge_from_exploration'])
def test_retained_moc_heading_and_independent_controls_should_identify_the_page(records, phase):
    event = phase_events(records, phase, 'ocr_to_parser')[0]
    # Retained left-region input, not a full screenshot replay. Its observed
    # production classification was also unknown, as the original run records.
    actual = classify_supported_page('\n'.join(b['text'] for b in event['boxes']), event['boxes'],
                                     width=2560, height=1600)
    assert actual['page_type'] == 'challenge_setup', actual


def test_complete_task_objective_should_survive_same_frame_prefix_duplicate(records):
    event = phase_events(records, 'exploration_from_attributes', 'ocr_to_parser')[0]
    assert len(task_stacks(event['boxes'], width=2560, height=1600)) == 1


def test_new_confirmed_location_should_not_expire_as_no_new_facts(diagnosis, records, monkeypatch, no_network, local_text_channel):
    from plugin.plugins.hsr_companion.runtime import followup
    from plugin.plugins.hsr_companion.tests.test_page_followup import followup_owner

    partial = diagnosis['exploration']
    assert partial['partial_answer_status']['confirmed_fields'] == ['location']
    # The old diagnosis only invoked the loop AFTER its deadline, which must
    # not grant a new speech permission. Reproduce the actual lost progress:
    # location existed during the original pending window, before expiry.
    # Admission and activity are explicit local fixtures, not pixel evidence.
    visual = historical_visual(phase_events(records, 'exploration_from_attributes', 'store_commit')[-1]['observation'])
    plan = build_query_plan('现在在哪，任务目标是什么？', 'read_screen', screen_fields=['location', 'quest'])
    owner, sent = followup_owner(monkeypatch, value=visual)
    request = AnswerRequest('offline-retained-partial', plan.query, '猫娘', time.time(), time.monotonic(),
        time.monotonic() + .08, 'unknown', False, True, True, False, plan=plan)
    owner._pending_answer = request
    no_network.run(followup.deliver_followup(owner, request))
    assert len(sent) == 1 and sent[0]['metadata']['outcome'] == 'partial'
    assert owner._followup_status['state'] == 'expired'


def test_verified_light_cone_name_should_survive_followup_projection(diagnosis, local_text_channel):
    visual = {'fresh': True, 'page_type': 'character_detail', 'read_scope': 'full',
              'scene': {'verified': True}, 'screen_facts': {'facts': diagnosis['cone']['reply_facts']}}
    plan = build_query_plan('这把光锥叫什么，效果是什么？', 'read_screen', screen_fields=['equipment'])
    request = AnswerRequest('offline-red-cone', plan.query, 'local-only', 0., 0., 0., 'character_detail',
                            False, False, True, False, plan=plan)
    assert answer_ready(visual, request)
    body, receipt = render_followup(reply_data(visual, request), plan=plan, ready=True)
    assert '献给明日的色彩' in body, receipt


def test_known_different_panel_subject_should_not_complete_pearl_assessment(records):
    row = phase_row(records, 'attributes_from_challenge')
    visual = copy.deepcopy(row['packet']['current_visual_evidence'])
    registry = json.loads((ROOT / 'resources/character_registry.json').read_text(encoding='utf-8'))
    catalog = ExternalKnowledgeCatalog(registry=registry)
    book = load_guidance_catalog(ROOT / 'resources/guidance.cn-4.6.json')
    subject = catalog.resolve_character('绯英')
    # Diagnostic counterexample, not a claim that OCR confirmed this subject
    # during the real run (its characters were empty; the human confirmed it).
    visual['characters'] = [{'entity_id': subject['id'], 'name': subject['name'], 'identity_scope': 'detail_subject'}]
    query = '真珠这面板怎么样，接下来先养哪儿？'
    base = build_query_plan(query, 'assess_panel')
    declaration = validate_declaration({'scope': 'current_panel', 'subject_state': 'specified', 'character': '真珠'},
                                       intent=base.intent)
    plan = plan_semantic_guidance(query, base, catalog=catalog, book=book, declaration=declaration)
    guidance = build_guidance(plan, query, book=book, catalog=catalog, profile={}, visual=visual)
    assert guidance['completion']['evaluation_complete'] is False


def test_genuine_conflicting_task_rows_remain_ambiguous(diagnosis):
    complete = copy.deepcopy(diagnosis['exploration']['nearest_row_groups'][1]['rows'][0])
    conflicting = {**complete, 'text': '询问其他人关于完全不同任务', 'score': complete['score'] - .001}
    assert distinct_rows([complete, conflicting]) == []


def relic_visual(records):
    return historical_visual(phase_events(records, 'relic', 'store_commit')[-1]['observation'])


def test_complete_relic_main_and_four_substats_still_survive_projection(records, local_text_channel):
    visual = relic_visual(records)
    card = equipment_card(visual)
    assert card['complete'] and len(card['sub_stats']) == 4
    plan = build_query_plan('完整报出这件遗器的主词条和四项副词条。', 'read_screen', screen_fields=['equipment'])
    request = AnswerRequest('offline-full-relic', plan.query, 'local-only', 0., 0., 0., 'relic_detail',
                            False, False, True, False, plan=plan)
    body, receipt = render_followup(reply_data(visual, request), plan=plan, ready=answer_ready(visual, request))
    assert receipt['complete'] and receipt['required_rows'] == receipt['message_rows'] == 5
    assert all(r['label'] + r['value'] in body for r in [card['main_stat'], *card['sub_stats']])


def test_page_and_four_substats_cannot_certify_a_missing_main(records):
    visual = relic_visual(records)
    visual['screen_facts']['facts'] = [f for f in visual['screen_facts']['facts'] if f.get('kind') != 'main_stat']
    plan = build_query_plan('完整报出这件遗器的主词条和四项副词条。', 'read_screen', screen_fields=['equipment'])
    assert 'equipment' not in satisfied_fields(plan, visual)
