"""R4: unchanged R3 candidate, two resident live transitions, no cat/installation.

Recognition, verification, deadlines and completion are never replaced. Only
diagnostic wrappers, read-only archive copy and local SDK transport are used.
This cannot accept autonomous model selection or cat paraphrase.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import re
import socket
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from . import trace_character_handoff as handoff
from .field_pipeline_probe import summary
from .trace_current_panel_boundary import pearl_archive
from .trace_reader_budget import ReaderBudgetProbe
from .verify_roundtrip_c_acceptance import first_change, loopback_only, steps

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'dist/page-body-r3-result-20261021.json'
BUILD = 'demo-page-body-r3-20261021.1'


def frozen_sources():
    """Check BOTH R3 edits and its R1/R2 protected source set before pausing."""
    latest = json.loads(MANIFEST.read_text(encoding='utf-8'))
    assert latest['candidate']['build_revision'] == BUILD
    assert latest['status']['final_fixed_batch_green'] and not latest['status']['installed']
    reference = latest['protected_verification']
    raw = (ROOT / reference['reference']).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == reference['reference_sha256']
    previous = json.loads(raw)
    expected = {r['path']: r['sha256'] for r in latest['candidate_hashes']}
    excluded = reference['explicit_r3_changes_excluded']
    for row in previous['protected_files'] + previous['candidate_hashes']:
        if row['path'] not in excluded:
            expected[row['path']] = row['sha256']
    for name, digest in expected.items():
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != digest:
            raise RuntimeError('Frozen source changed: ' + name)
    return expected


def receiving_text(message):
    from plugin.server.messaging.proactive_bridge import ProactiveBridge
    events = []
    ProactiveBridge._dispatch(None, {'plugin_id': 'hsr_companion', **message},
                             SimpleNamespace(send_json=lambda event, *args: events.append(event)))
    return next(e['text'] for e in events if e['event_type'] == 'proactive_message')


def compact(text):
    # Display separators only. Never repair digits, names, signs or OCR words.
    return re.sub(r'[\s：:]', '', text)


def assess_phase(row):
    """Content/service gate, not a replacement for runtime evidence validation."""
    direct = row.get('packet') or {}
    candidates = []
    if (direct.get('query_answer') or {}).get('state') == 'ready':
        candidates.append(('direct_tool', direct, None))
    for item in row.get('followups', []):
        payload = item['payload']
        progress = payload.get('request_completion') or {}
        receipt = item.get('projection') or {}
        if progress.get('delivered_complete') and receipt.get('complete') and receipt.get('answer_complete'):
            candidates.append(('followup_bridge', payload, item.get('received_text', '')))
    reasons = []
    for channel, payload, body in candidates:
        if row['phase'] == 'attributes':
            guide = payload.get('guidance_answer') or {}
            complete = guide.get('completion') or {}
            if not (complete.get('evaluation_complete') and complete.get('panel_subject_state') == 'confirmed'
                    and complete.get('subject_ids') == ['character.pearl']):
                reasons.append('evaluation_or_panel_owner_incomplete')
                continue
            reply = guide.get('reply_plan') or {}
            if not reply.get('conclusion') or not reply.get('evaluation_available'):
                reasons.append('evaluation_body_missing')
                continue
            if body is not None and not ('真珠' in body and '依据：' in body
                    and compact(reply['conclusion']) in compact(body) and all(
                    compact(str(r['label']) + str(r['value'])) in compact(body)
                    for r in reply.get('evidence_rows') or []
                    if reply.get('mode') == 'full' or r.get('speak_value'))):
                reasons.append('evaluation_did_not_survive_bridge')
                continue
            return {'accepted': True, 'channel': channel, 'evaluation_complete': True,
                    'panel_subject_state': 'confirmed'}
        visual = payload.get('current_visual_evidence', payload)
        if visual.get('page_type') != 'challenge_setup' or not (visual.get('scene') or {}).get('verified'):
            reasons.append('challenge_page_unconfirmed')
            continue
        if body is None:
            # Direct tool is a different channel; do not pretend it triggered a
            # real followup. Dry-run the same admitted packet through the bridge.
            from ..runtime.answer_message import bridge_preview, render_followup
            from ..runtime.query_plan import build_query_plan
            selected = build_query_plan(row['query'], 'explain_page', screen_fields=['page', 'visible_text'])
            body, receipt = render_followup(visual, plan=selected, ready=True)
            if not receipt.get('complete') or bridge_preview(body) != body:
                reasons.append('direct_packet_notification_projection_incomplete')
                continue
        minimum = json.loads((ROOT / 'tests/fixtures/page_body_r3_20261021.json').read_text(encoding='utf-8'))[
            'ordinary_summary_minimum_for_this_sample']
        literals = [minimum['page'], minimum['stage_title'], *minimum['objectives']]
        if not all(compact(t) in compact(body) for t in literals):
            reasons.append('required_stage_or_objective_body_missing')
            continue
        return {'accepted': True, 'channel': channel, 'required_sections': ['page', 'stage_title', 'three_objectives'],
                'followup_triggered': channel == 'followup_bridge'}
    return {'accepted': False, 'reason': list(dict.fromkeys(reasons)) or ['no_complete_delivery'],
            'followup_triggered': bool(row.get('followups'))}


def accept_run(rows):
    if [r['phase'] for r in rows] != ['attributes', 'challenge']:
        return False
    for row in rows:
        if not assess_phase(row)['accepted'] or len(row.get('followups', [])) > 2:
            return False
        confirmation = row.get('local_confirmation') or {}
        if confirmation.get('state') != 'not_triggered' and not all(confirmation.get(key) is True
                for key in ('accepted', 'deadline_unchanged', 'generation_unchanged')):
            return False
    return True


def baseline_record(baseline, *, operator_confirmed=False):
    # A test starting point is not a permission to publish game facts. Keep the
    # classifier's unknown intact; neither this note nor the human assertion
    # enters the observer, cache, page guards or destination acceptance gates.
    if baseline.get('page') == 'exploration_hud':
        return {'classifier_page': 'exploration_hud', 'basis': 'runtime_page', 'page_accepted': True}
    if baseline.get('page') == 'unknown' and baseline.get('serial') and operator_confirmed:
        return {'classifier_page': 'unknown', 'basis': 'operator_confirmed_start_only', 'page_accepted': False}
    raise RuntimeError('Exploration baseline absent; no cross-page acceptance')


async def phases(owner, args, result, emit):
    from plugin.sdk.plugin import NekoPluginBase

    from ..runtime import followup, live_refresh
    from ..runtime.answer_message import bridge_preview, render_followup
    from ..runtime.query_plan import build_query_plan

    signals = Path(args.signals).resolve()
    if not signals.is_relative_to(ROOT / 'dist') or signals.exists():
        raise ValueError('Use a new plugin-owned signal directory')
    signals.mkdir()
    owner._D_abort_marker = signals / 'abort'
    baseline = summary(getattr(owner, '_committed_observation', None))
    result.update(test_scope='R4_frozen_R3_two_live_transitions', build_revision=BUILD, roundtrip=[],
                  exploration_baseline=baseline, diagnostic_refresh_trace=[], host_chat_submissions=0,
                  cat_paraphrase_acceptance=False, retries_per_destination=0)
    result['baseline_provenance'] = baseline_record(baseline,
        operator_confirmed=getattr(args, 'operator_baseline_exploration', False))
    events, submissions, deliveries = [], [], []
    owner.ctx = SimpleNamespace(bus=NekoPluginBase(SimpleNamespace(plugin_id='isolated-request-chain-R4',
        metadata={}, bus=SimpleNamespace(memory=SimpleNamespace(get=lambda **kw: list(events))))).bus)
    owner._target_character = lambda: 'hsr_isolated_test'
    owner.push_message = lambda **kw: submissions.append(copy.deepcopy(kw)) or {'submitted': True}
    active = {'phase': 'baseline'}
    record, trace = followup.record_answer_delivery, live_refresh.trace_refresh

    def capture(target, **kw):
        receipt = record(target, **kw)
        if target is owner:
            deliveries.append({'phase': active['phase'], 'payload': copy.deepcopy(kw['payload']),
                               'projection': copy.deepcopy(kw.get('message_projection') or {}),
                               'request_id': kw.get('request_id')})
        return receipt

    def traced(target, stage, **kw):
        trace(target, stage, **kw)
        if target is owner and len(result['diagnostic_refresh_trace']) < 5000:
            result['diagnostic_refresh_trace'].append({'phase': active['phase'], 'at': time.time(), 'stage': stage, **kw})

    with patch.object(followup, 'record_answer_delivery', capture), patch.object(live_refresh, 'trace_refresh', traced):
        for phase, query, intent, fields, declaration, expected in steps()[:2]:
            active['phase'] = phase
            trigger = await first_change(owner, emit, phase=phase, until=time.monotonic() + 300)
            row = {'phase': phase, 'query': query, 'intent': intent, 'screen_fields': fields,
                   'expected_page': expected, 'trigger': trigger, 'asked_at': time.time(),
                   'prequery': summary(getattr(owner, '_committed_observation', None))}
            result['roundtrip'].append(row)
            started = time.monotonic()
            kwargs = {'query': query, 'intent': intent}
            if fields is not None:
                kwargs['screen_fields'] = fields
            if declaration is not None:
                kwargs['consultation'] = declaration
            row['packet'] = await owner.hsr_companion_prepare(**kwargs)
            row['direct_seconds'] = round(time.monotonic() - started, 3)
            emit('r4_direct', phase=phase, seconds=row['direct_seconds'],
                 answer=row['packet'].get('query_answer'), completion=row['packet'].get('request_completion'))
            pending = getattr(owner, '_pending_answer', None)
            if pending is not None:
                expires, generation = pending.expires_at, owner._answer_query_generation
                events.append({'type': 'user_message', 'lanlan': 'hsr_isolated_test', 'content': '好', '_ts': time.time()})
                reply = await owner.hsr_companion_prepare(query='好', pending_reply={
                    'request_id': pending.request_id, 'relation': 'confirm', 'player_message': '好'})
                row['local_confirmation'] = {'accepted': reply['pending_reply']['accepted'],
                    'deadline_unchanged': expires == pending.expires_at,
                    'generation_unchanged': generation == owner._answer_query_generation,
                    'simulated_relation_not_real_cat': True}
                task = getattr(owner, '_followup_task', None)
                if task is not None:
                    while not task.done():
                        if owner._D_abort_marker.exists():
                            raise RuntimeError('Operator aborted; keep partial run')
                        await asyncio.wait({task}, timeout=.25)
                    await task
            else:
                row['local_confirmation'] = {'state': 'not_triggered'}
            row['followup_status'] = copy.deepcopy(getattr(owner, '_followup_status', {}))
            row['followups'] = []
            for delivery in (d for d in deliveries if d['phase'] == phase):
                sent = next((s for s in submissions if s.get('metadata', {}).get('request_id') == delivery['request_id']
                             and (s.get('metadata', {}).get('message_projection') or {}).get('text_sha256')
                             == delivery['projection'].get('text_sha256')), None)
                row['followups'].append({**delivery, 'received_text': receiving_text(sent) if sent else ''})
            row['total_seconds'] = round(time.monotonic() - started, 3)
            row['final_committed'] = summary(getattr(owner, '_committed_observation', None))
            row['page_probe_budget'] = {key: (getattr(owner, '_page_check_budget', None) or {}).get(key)
                                       for key in ('attempts', 'native_calls', 'native_seconds')}
            row['verdict'] = assess_phase(row)
            if phase == 'challenge':
                selected = (row['followups'][-1]['payload'] if row['followups'] else
                            row['packet'].get('current_visual_evidence') or {})
                full = build_query_plan('逐字报出完整页面文字。', 'explain_page',
                                       screen_fields=['page', 'visible_text'], response_detail='full')
                text, receipt = render_followup(selected, plan=full, ready=True)
                row['explicit_full_same_packet_dry_run'] = {'projection': receipt,
                    'formatter_unchanged': bridge_preview(text) == text,
                    'not_a_new_tool_request_or_cat_delivery': True}
            emit('r4_phase_finished', phase=phase, seconds=row['total_seconds'], verdict=row['verdict'],
                 confirmation=row['local_confirmation'], probes=row['page_probe_budget'],
                 followup=row['followup_status'])
    result['live_core_accepted'] = accept_run(result['roundtrip'])


async def run(args):
    from ..tests import test_current_observation as fixtures
    expected = frozen_sources()
    saved, fixture = pearl_archive(), fixtures.plugin_fixture
    original_collect = handoff.collect_probe_report

    def isolated_fixture(*a, **kw):
        owner, values, sent = fixture(*a, **kw)
        values.update(copy.deepcopy(saved))
        return owner, values, sent

    def collect(result, probes, checks):
        original_collect(result, probes, checks)
        result['frozen_r3_sources_before'] = expected
        try:
            result['frozen_r3_sources_unchanged'] = expected == frozen_sources()
        except Exception as exc:
            # Cleanup/report must survive a failed final hash check too.
            result['frozen_r3_sources_unchanged'] = False
            result['final_source_check_error'] = type(exc).__name__
        result['archive_source'] = 'read_only_pearl_copy_in_memory'
        result['r4_harness_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

    args.delivery_phases, args.preload_backend = phases, True
    args.on_page_discovery = args.on_control_change = False
    with patch.object(fixtures, 'plugin_fixture', isolated_fixture), \
            patch.object(handoff, 'ReviewProbe', ReaderBudgetProbe), \
            patch.object(handoff, 'collect_probe_report', collect), \
            patch.object(socket.socket, 'connect', loopback_only(socket.socket.connect)):
        await handoff.run(args)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', required=True)
    parser.add_argument('--signals', required=True)
    parser.add_argument('--port', type=int, default=8485)
    parser.add_argument('--operator-baseline-exploration', action='store_true',
                        help='Only after explicit human confirmation; unknown remains unconfirmed, not product acceptance.')
    asyncio.run(run(parser.parse_args()))
