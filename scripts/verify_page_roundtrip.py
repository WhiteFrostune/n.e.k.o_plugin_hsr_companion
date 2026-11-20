"""One bounded three-family round trip; real source/tools, no installed changes.

Human controls the game and acknowledges each destination. This is resident
use, NOT a cold-first-frame benchmark. No warm-up queries, retries or refresh.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from . import trace_character_handoff as handoff
from .trace_current_panel_boundary import pearl_archive


def steps():
    panel = {'scope': 'current_panel', 'subject_state': 'specified', 'character': '真珠'}
    return [
        ('attributes', '真珠这面板怎么样，接下来先养哪儿？', 'assess_panel', None, panel, 'character_detail'),
        ('relic', '完整报出这件遗器的主词条和四项副词条。', 'read_screen', ['equipment'], None, 'relic_detail'),
        ('cone', '这把光锥叫什么，效果是什么？', 'read_screen', ['equipment'], None, 'character_detail'),
        ('challenge_from_cone', '现在是哪种挑战，页面上写了什么？', 'explain_page', ['page', 'visible_text'], None, 'challenge_setup'),
        ('attributes_from_challenge', '真珠这面板怎么样，接下来先养哪儿？', 'assess_panel', None, panel, 'character_detail'),
        ('exploration_from_attributes', '现在在哪，任务目标是什么？', 'read_screen', ['location', 'quest'], None, 'exploration_hud'),
        ('challenge_from_exploration', '现在是哪种挑战，页面上写了什么？', 'explain_page', ['page', 'visible_text'], None, 'challenge_setup'),
        ('exploration_from_challenge', '现在在哪，任务目标是什么？', 'read_screen', ['location', 'quest'], None, 'exploration_hud'),
    ]


def baseline_state(raw):
    page = (raw.get('page') or {}).get('page_type')
    return {'expected_page': 'exploration_hud', 'observed_page': page,
            'confirmed': page == 'exploration_hud',
            'failure_retained': page != 'exploration_hud'}


async def phases(owner, args, result, emit):
    from plugin.plugins.hsr_companion.runtime.build_info import BUILD_REVISION
    from plugin.sdk.plugin import NekoPluginBase

    signals = Path(args.signals).resolve()
    root = Path(__file__).resolve().parents[1]
    if not signals.is_relative_to(root / 'dist') or signals.exists():
        raise ValueError('Use a new plugin-owned marker directory')
    baseline = baseline_state(getattr(owner, '_committed_observation', None) or {})
    signals.mkdir()
    result.update(build_revision=BUILD_REVISION, roundtrip=[],
                  exploration_baseline=baseline,
                  trigger='formal_query_after_human_destination_ack_not_cold_capture',
                  host_chat_submissions=0, retries_per_destination=0)
    emit('roundtrip_baseline', **baseline)
    events, submissions = [], []
    owner.ctx = SimpleNamespace(bus=NekoPluginBase(SimpleNamespace(plugin_id='isolated-page-roundtrip',
        metadata={}, bus=SimpleNamespace(memory=SimpleNamespace(get=lambda **kw: list(events))))).bus)
    owner._target_character = lambda: 'hsr_isolated_test'
    owner.push_message = lambda **kw: submissions.append(kw) or {'submitted': True}

    for phase, query, intent, fields, declaration, expected in steps():
        emit('awaiting_roundtrip_transition', phase=phase, expected_page=expected, marker=str(signals / phase))
        until = time.monotonic() + 300
        while not (signals / phase).exists():
            if time.monotonic() >= until:
                raise TimeoutError('Destination acknowledgment absent: ' + phase)
            await asyncio.sleep(.25)
        raw = getattr(owner, '_committed_observation', None) or {}
        row = {'phase': phase, 'expected_page': expected, 'asked_at': time.time(),
               'prequery_page': raw.get('page', {}).get('page_type'),
               'prequery_observed_at': raw.get('observed_at'), 'prequery_serial': raw.get('observation_serial')}
        kwargs = {'query': query, 'intent': intent}
        if fields is not None:
            kwargs['screen_fields'] = fields
        if declaration is not None:
            kwargs['consultation'] = declaration
        before = len(submissions)
        started = time.monotonic()
        packet = await owner.hsr_companion_prepare(**kwargs)
        visual = packet.get('current_visual_evidence') or {}
        row.update(direct_seconds=round(time.monotonic() - started, 3), packet=packet,
                   direct_page=visual.get('page_type'), direct_page_matches=visual.get('page_type') == expected)
        result['roundtrip'].append(row)  # Failure is retained before any followup.
        emit('roundtrip_direct', phase=phase, seconds=row['direct_seconds'], page=row['direct_page'],
             state=packet.get('query_answer', {}).get('state'), page_matches=row['direct_page_matches'])
        pending = getattr(owner, '_pending_answer', None)
        if pending is not None:
            expires = pending.expires_at
            events.append({'type': 'user_message', 'lanlan': 'hsr_isolated_test', 'content': '好', '_ts': time.time()})
            acknowledgment = await owner.hsr_companion_prepare(query='好', pending_reply={
                'request_id': pending.request_id, 'relation': 'confirm', 'player_message': '好'})
            row['local_confirmation'] = {'accepted': acknowledgment['pending_reply']['accepted'],
                                         'deadline_unchanged': expires == pending.expires_at}
            task = getattr(owner, '_followup_task', None)
            if task is not None:
                await task
            row['followup_status'] = copy.deepcopy(getattr(owner, '_followup_status', {}))
        row['submissions'] = [s for s in submissions[before:] if s.get('metadata', {}).get('kind') == 'query_followup']
        row['total_seconds'] = round(time.monotonic() - started, 3)
        row['after_wait_page'] = (getattr(owner, '_committed_observation', None) or {}).get('page', {}).get('page_type')
        emit('roundtrip_destination_finished', phase=phase, total_seconds=row['total_seconds'],
             followup=row.get('followup_status'), after_wait_page=row['after_wait_page'],
             submissions=len(row['submissions']))


async def run(args):
    from plugin.plugins.hsr_companion.tests import test_current_observation as fixtures

    original_fixture = fixtures.plugin_fixture
    saved = pearl_archive()

    def fixture(*values, **kwargs):
        owner, store, sent = original_fixture(*values, **kwargs)
        store.update(copy.deepcopy(saved))
        return owner, store, sent

    args.delivery_phases = phases
    args.preload_backend = True
    args.on_page_discovery = args.on_control_change = False
    with patch.object(fixtures, 'plugin_fixture', fixture):
        await handoff.run(args)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', required=True)
    parser.add_argument('--signals', required=True)
    parser.add_argument('--port', type=int, default=8465)
    asyncio.run(run(parser.parse_args()))
