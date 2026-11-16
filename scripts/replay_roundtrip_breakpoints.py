"""Offline causal replay of the retained eight-transition run; NEVER repairs.

Only plugin-owned records/output. No screenshots, native OCR, helper/host
connection, player-store writes, installation, or production-module patches.
Historical fact shapes are replayed at their recorded admission state, NOT
renewed into live evidence. The probe saved only part of the OCR box list;
each comparison states that limitation explicitly.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import inspect
import json
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_run(path):
    path = Path(path).resolve(strict=True)
    if not path.is_relative_to(ROOT / 'dist'):
        raise ValueError('Only retained plugin-owned records are accepted')
    return json.loads(path.read_text(encoding='utf-8'))


def phase_row(run, phase):
    return next(row for row in run['roundtrip'] if row['phase'] == phase)


def phase_events(run, phase, kind):
    row = phase_row(run, phase)
    return [event for event in run['pipeline_trace'] if event['kind'] == kind
            and row['asked_at'] <= event['at'] <= row['asked_at'] + row['total_seconds']]


def capture_locals(function, *args, names=(), **kwargs):
    """Call the original once; copy selected locals without changing control flow."""
    captured = {}
    previous = sys.gettrace()
    code = function.__code__

    def trace(frame, event, value):
        if frame.f_code is not code:
            return None
        if event == 'return':
            captured.update({key: copy.deepcopy(frame.f_locals[key]) for key in names if key in frame.f_locals})
        return trace

    try:
        sys.settrace(trace)
        result = function(*args, **kwargs)
    finally:
        sys.settrace(previous)
    return result, captured


def classify_trace(boxes, width, height):
    from plugin.plugins.hsr_companion.detectors.layout import classify_supported_page
    source, start = inspect.getsourcelines(classify_supported_page)
    gate = start + next(i for i, line in enumerate(source) if 'if not strong and len(anchors)' in line)
    rules = []
    previous = sys.gettrace()
    code = classify_supported_page.__code__

    def trace(frame, event, value):
        if frame.f_code is not code:
            return None
        if event == 'line' and frame.f_lineno == gate:
            local, rule = frame.f_locals, frame.f_locals['rule']
            rules.append({'page_type': rule['page_type'], 'strong': list(local['strong']),
                          'anchors': list(local['anchors']), 'minimum': rule['minimum']})
        return trace

    try:
        sys.settrace(trace)
        result = classify_supported_page('\n'.join(b['text'] for b in boxes), boxes, width=width, height=height)
    finally:
        sys.settrace(previous)
    return result, rules


def challenge_replay(run, width, height):
    result = []
    for phase in ('challenge_from_cone', 'challenge_from_exploration'):
        attempts = []
        for event in phase_events(run, phase, 'ocr_to_parser'):
            page, rules = classify_trace(event['boxes'], width, height)
            attempts.append({'at': event['at'], 'recorded_page': event['page'], 'replayed_page': page['page_type'],
                'matched_rule_gates': rules, 'text': [b['text'] for b in event['boxes']],
                'dimensions': [width, height], 'coverage': 'retained_left_55_percent_top_60_percent_not_full_frame'})
        result.append({'phase': phase, 'attempts': attempts,
                       'recorded_followup': phase_row(run, phase).get('followup_status')})
    return result


def historical_visual(summary):
    """Shape-only fixture. Never passed to a real observer, store or model."""
    return {'fresh': True, 'page_type': summary['page'], 'scene': {'verified': True, 'label': summary['page']},
            'read_scope': 'full', 'observed_at': summary.get('observed_at'), 'characters': [],
            'screen_facts': {'facts': copy.deepcopy(summary.get('facts') or []), 'visible_text': [],
                             'field_checks': copy.deepcopy(summary.get('field_checks') or [])}}


async def expire_partial(visual, plan):
    from plugin.plugins.hsr_companion.runtime import followup
    request = followup.AnswerRequest('offline-expired-original-deadline', plan.query, 'local-only',
                                    0., 0., 0., visual['page_type'], plan.scene_only, plan.navigation,
                                    plan.content, False, plan=plan)

    async def current():
        return copy.deepcopy(visual)

    submitted = []
    owner = SimpleNamespace(_pending_answer=request, _observer_status={'enabled': True, 'state': 'observing'},
        _observer_stop_requested=False, _target_character=lambda: 'local-only', _current_visual_evidence=current,
        _reusable_observations={}, push_message=lambda **kw: submitted.append(kw))
    # Test only: skip the speech grace sleep. No budget, deadline or gate is
    # extended; the recorded partial is inspected at an already expired date.
    with patch.object(followup, 'FOLLOWUP_GRACE_SECONDS', 0):
        await followup.deliver_followup(owner, request)
    return {'status': owner._followup_status, 'submitted': len(submitted)}


def exploration_replay(run, catalog, width, height, loop):
    from plugin.plugins.hsr_companion.detectors.exploration import distinct_rows, task_stacks
    from plugin.plugins.hsr_companion.runtime.followup import AnswerRequest, answer_ready
    from plugin.plugins.hsr_companion.runtime.query_coordinator import answer_status
    from plugin.plugins.hsr_companion.runtime.query_plan import build_query_plan
    from plugin.plugins.hsr_companion.runtime.screen_facts import _boxes, extract_screen_facts

    phase = 'exploration_from_attributes'
    event = phase_events(run, phase, 'ocr_to_parser')[0]
    boxes = _boxes(event['boxes'], width, height)
    stacks, local = capture_locals(task_stacks, boxes, width=width, height=height,
        names=('nav', 'h', 'candidates', 'groups', 'above'))
    groups = []
    for group in local.get('groups', [])[-2:]:
        kept = distinct_rows(group)
        ranked = sorted(group, key=lambda b: (-b['score'], -len(b['text'])))
        groups.append({'rows': group, 'retained': kept,
                       'top_two_score_gap': ranked[0]['score'] - ranked[1]['score'] if len(ranked) > 1 else None})
    # Counterfactual diagnosis ONLY: remove one already-retained same-frame
    # proper-prefix rival. This is not a production deduplication algorithm.
    fragments = [b for b in boxes if any(other is not b and other['text'].startswith(b['text'])
        and other['text'] != b['text'] and abs(other['left'] - b['left']) <= 1
        and abs(other['top'] - b['top']) <= 4 for other in boxes)]
    ablations = []
    for fragment in fragments:
        remaining = [b for b in boxes if b is not fragment]
        output = extract_screen_facts(remaining, width=width, height=height,
                                     page={'page_type': 'exploration_hud'}, catalog=catalog)
        ablations.append({'removed_same_frame_row': fragment,
                          'quest_facts': [f for f in output['facts'] if f.get('scope') == 'quest']})
    summary = phase_events(run, phase, 'store_commit')[-1]['observation']
    visual = historical_visual(summary)
    plan = build_query_plan('现在在哪，任务目标是什么？', 'read_screen', screen_fields=['location', 'quest'])
    req = AnswerRequest('offline-readiness', plan.query, 'local-only', 0., 0., 0., 'exploration_hud',
                        False, True, True, False, plan=plan)
    return {'recorded_quest_facts': [f for f in event['facts'] if f.get('scope') == 'quest'],
            'navigation': local.get('nav'), 'navigation_row_height': local.get('h'), 'task_stack_count': len(stacks),
            'nearest_row_groups': groups, 'above_count': len(local.get('above', [])), 'single_row_ablations': ablations,
            'recorded_commit_facts': summary['facts'],
            'partial_answer_status': answer_status(plan, visual, {'enabled': True, 'state': 'observing'}),
            'followup_answer_ready': answer_ready(visual, req),
            'at_original_expiration': loop.run_until_complete(expire_partial(visual, plan)),
            'coverage': 'quest_ROI_saved; Enter_control_outside_probe_is_not_fabricated; not_full_frame_parser_equivalence'}


def cone_replay(run, catalog):
    from plugin.plugins.hsr_companion.runtime.answer_message import render_followup
    from plugin.plugins.hsr_companion.runtime.followup import AnswerRequest, answer_ready, reply_data
    from plugin.plugins.hsr_companion.runtime.query_plan import build_query_plan

    phase = 'cone'
    summary = phase_events(run, phase, 'store_commit')[-1]['observation']
    visual = historical_visual(summary)
    labels = summary['equipment_labels']
    # The summary retains a committed label, NOT its complete proof row.
    # main_task_capture_at alone cannot identify a checkpoint's real frame.
    # Reconstruct only the documented entity-label shape for this projection
    # experiment; NEVER borrow parser geometry or call it live admission.
    recovered = [{'kind': 'entity_label', 'scope': 'displayed_label', 'label': '光锥',
                  'entity_type': 'light_cone', 'entity_id': label['entity_id'], 'value': label['value'],
                  'source': 'offline_reconstructed_committed_label_no_pixel_permission'} for label in labels]
    visual['screen_facts']['facts'].extend(copy.deepcopy(recovered))
    plan = build_query_plan('这把光锥叫什么，效果是什么？', 'read_screen', screen_fields=['equipment'])
    req = AnswerRequest('offline-cone', plan.query, 'local-only', 0., 0., 0., 'character_detail',
                        False, False, True, False, plan=plan)
    data = reply_data(visual, req, catalog=catalog)
    text, projection = render_followup(data, plan=plan, ready=answer_ready(visual, req))
    from plugin.plugins.hsr_companion.runtime.grounding import build_grounding
    grounded = build_grounding(plan.query, visual, catalog, plan=plan)
    return {'recorded_label': labels, 'source_capture_at': summary['observed_at'],
            'label_reconstruction_basis': 'committed_summary_label_schema_only_no_parser_geometry_or_frame_merge',
            'readiness': answer_ready(visual, req), 'plan': plan.public(),
            'reply_facts': data['screen_facts']['facts'], 'rendered_text': text, 'projection': projection,
            'direct_grounding_knowledge': [{'id': r.get('id'), 'name': r.get('name'), 'record_keys': sorted(r)}
                                           for r in grounded['knowledge_results']],
            'followup_data_has_grounding_or_knowledge': bool(data.get('grounding') or data.get('knowledge_results')),
            'recorded_text': [part['text'] for s in phase_row(run, phase)['submissions'] for part in s['parts']],
            'recorded_projection': phase_row(run, phase)['submissions'][0]['metadata']['message_projection']}


def subject_replay(run, catalog, book):
    from plugin.plugins.hsr_companion.runtime.consultation_contract import validate_declaration
    from plugin.plugins.hsr_companion.runtime.delivery_scope import project_evidence
    from plugin.plugins.hsr_companion.runtime.guidance import build_guidance
    from plugin.plugins.hsr_companion.runtime.query_plan import build_query_plan
    from plugin.plugins.hsr_companion.runtime.semantic_guidance import plan_semantic_guidance

    row = phase_row(run, 'attributes_from_challenge')
    visual = row['packet']['current_visual_evidence']
    query = '真珠这面板怎么样，接下来先养哪儿？'
    base = build_query_plan(query, 'assess_panel')
    declaration = validate_declaration({'scope': 'current_panel', 'subject_state': 'specified', 'character': '真珠'},
                                       intent='assess_panel')
    plan = plan_semantic_guidance(query, base, catalog=catalog, book=book, declaration=declaration)
    results = {}
    name = catalog.resolve_character('绯英')
    different = [{'entity_id': name['id'], 'name': name['name'], 'identity_scope': 'detail_subject'}]
    for key, characters in [('recorded_no_visual_subject', visual.get('characters', [])),
            ('counterfactual_known_different_subject', different),
            ('counterfactual_different_subject_through_real_field_projection', different)]:
        candidate = {**visual, 'characters': copy.deepcopy(characters)}
        if key.endswith('through_real_field_projection'):
            candidate = project_evidence(candidate, base)
        guidance = build_guidance(plan, query, book=book, catalog=catalog, profile={}, visual=candidate,
                                  now=datetime.fromtimestamp(row['asked_at'], timezone.utc))
        assessment = guidance['assessment']
        results[key] = {'input_characters': characters, 'visual_characters': candidate.get('characters'),
                       'attribute_subject_ids': [f.get('subject_entity_id') or f.get('subject_id')
                                                 for f in candidate['screen_facts']['facts'] if f.get('scope') == 'attributes'],
                       'subject_ids': assessment['subject_ids'],
                       'visual_subject_confirmed': assessment['visual_subject_confirmed'],
                       'evaluation_available': assessment['evaluation_available'], 'completion': guidance['completion'],
                       'reply_rows': guidance['reply_plan']['evidence_rows']}
    return results


def run(path, *, width=2560, height=1600, candidate=False):
    from plugin.plugins.hsr_companion.data import ExternalKnowledgeCatalog
    from plugin.plugins.hsr_companion.runtime.build_info import BUILD_REVISION
    from plugin.plugins.hsr_companion.runtime.guidance import load_guidance_catalog

    recorded = load_run(path)
    current = {key: hashlib.sha256((ROOT / key).read_bytes()).hexdigest() for key in recorded['source_hashes']}
    # The original run's probe did not fingerprint these three content paths.
    # Record them now without inventing a historical comparison for them.
    extra = {key: hashlib.sha256((ROOT / key).read_bytes()).hexdigest() for key in (
        'detectors/layout.py', 'runtime/answer_message.py', 'runtime/consultation_contract.py') if key not in current}
    if current != recorded['source_hashes'] and not candidate:
        raise AssertionError('Tracked source no longer matches retained run; do not claim same-code replay')
    registry = json.loads((ROOT / 'resources/character_registry.json').read_text(encoding='utf-8'))
    # Reads only the optional game knowledge pack, never player-store contents.
    catalog = ExternalKnowledgeCatalog(registry=registry)
    book = load_guidance_catalog(ROOT / 'resources/guidance.cn-4.6.json')
    # Windows creates an internal socketpair for its event loop. Initialize
    # that plumbing before prohibiting ALL connections during the replay.
    with asyncio.Runner() as runner:
        loop = runner.get_loop()
        with patch.object(socket.socket, 'connect', side_effect=AssertionError('Offline replay attempted networking')):
            result = {'scope': 'candidate_repair_shape_replay_not_live_admission' if candidate else 'diagnose_only_not_repair_or_live_admission',
                'production_changed': current != recorded['source_hashes'],
                'source_report': str(Path(path).resolve()), 'source_sha256': hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                'source_revision': recorded['build_revision'], 'source_hashes_match_recorded': current == recorded['source_hashes'],
                'candidate_hashes': current,
                'additional_candidate_hashes_without_recorded_baseline': extra,
                'candidate_revision': BUILD_REVISION,
                'changed_since_recording': [key for key in current if current[key] != recorded['source_hashes'][key]],
                'capture_geometry': {'width': width, 'height': height, 'basis': 'original_helper_2560x1600_no_new_capture'},
                'challenges': challenge_replay(recorded, width, height),
                'exploration': exploration_replay(recorded, catalog, width, height, loop),
                'cone': cone_replay(recorded, catalog), 'subjects': subject_replay(recorded, catalog, book)}
    result['source_hashes_unchanged_after_replay'] = {**current, **extra} == {
        key: hashlib.sha256((ROOT / key).read_bytes()).hexdigest() for key in {*current, *extra}}
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--candidate', action='store_true', help='Explicitly compare changed candidate; never claim same-code reproduction')
    args = parser.parse_args()
    output = Path(args.output).resolve()
    if not output.is_relative_to(ROOT / 'dist') or output.exists():
        raise ValueError('Use a new plugin-owned diagnosis output')
    outcome = run(args.report, candidate=args.candidate)
    with output.open('x', encoding='utf-8') as stream:
        json.dump(outcome, stream, ensure_ascii=False, indent=2)
    print(json.dumps({'report': str(output), 'source_hashes_unchanged': outcome['source_hashes_unchanged_after_replay'],
                      'diagnosis_only': not args.candidate, 'read_only_replay': True}))
