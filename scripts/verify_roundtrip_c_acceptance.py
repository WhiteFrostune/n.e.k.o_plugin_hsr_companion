"""D: three real transitions on the frozen C source; no installation or cat IO.

Only capture/trace/fixture transport are diagnostic. The real recognizer,
query budget, review, commit and formal entry are never replaced. Questions
start on the first substantial screen change, not after the human replies.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import socket
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from . import trace_character_handoff as handoff
from .field_pipeline_probe import summary
from .trace_current_panel_boundary import pearl_archive
from .trace_reader_budget import ReaderBudgetProbe

FROZEN_C = {
    'runtime/page_handoff.py': '7d8a5ef23427961111f7e71e3e44e10cca0d0aeddc588ad350be307e99f250f3',
    'runtime/live_refresh.py': '1da774c4a70436b011ddf8d3cf296fd65662f751aa9f1f9214701b23fd386725',
    'runtime/diagnostics.py': 'db32d1011a7c19522a8e7e007aa4f8254bab4216e5f5047c741de2a03294e920',
    'runtime/build_info.py': '7f5806b5e71194b78bb3c6e81d6f9729390d2e7a458a3e8d298e30ed43777912',
}


def steps():
    return [
        ('attributes', '真珠这面板怎么样，接下来先养哪儿？', 'assess_panel', None,
         {'scope': 'current_panel', 'subject_state': 'specified', 'character': '真珠'}, 'character_detail'),
        ('challenge', '现在是哪种挑战，页面上写了什么？', 'explain_page', ['page', 'visible_text'], None, 'challenge_setup'),
        ('exploration_return', '现在在哪，任务目标是什么？', 'read_screen', ['location', 'quest'], None, 'exploration_hud'),
    ]


def loopback_only(original):
    def connect(sock, address):
        if not isinstance(address, tuple) or address[0] not in {'127.0.0.1', '::1', 'localhost'}:
            raise PermissionError('D acceptance permits only local host/helper traffic')
        return original(sock, address)
    return connect


async def first_change(owner, emit, *, phase, until):
    from plugin.plugins.hsr_companion.runtime.live_refresh import frame_changed, frame_stamp

    initial = await asyncio.to_thread(owner._assistant_client.fetch_frame, 0)
    if initial is None:
        raise RuntimeError('No initial acquisition for transition trigger')
    # Diagnostic stimulus only: literal-proof sensitivity is intentionally NOT
    # reused as a human transition detector. It fires on translucent HUD
    # background animation even when the player has not switched pages.
    before = await asyncio.to_thread(frame_stamp, initial.image_bytes)
    emit('awaiting_transition', phase=phase, basis='substantial_coarse_change_not_page_assertion',
         initial_seq=initial.frame_seq, initial_capture_at=initial.timestamp,
         prequery=summary(getattr(owner, '_committed_observation', None)),
         producer_active=getattr(owner, '_vision_schedule_role', 'idle') == 'producer')
    last_seq = initial.frame_seq
    while time.monotonic() < until:
        if getattr(owner, '_D_abort_marker').exists():
            raise RuntimeError('Diagnostic operator aborted; retain partial run')
        frame = await asyncio.to_thread(owner._assistant_client.fetch_frame, 0)
        if frame is not None and (not frame.frame_seq or frame.frame_seq != last_seq):
            last_seq = frame.frame_seq
            now = await asyncio.to_thread(frame_stamp, frame.image_bytes)
            changed = frame_changed(before, now)
            if changed:
                value = {'phase': phase, 'frame_seq': frame.frame_seq, 'captured_at': frame.timestamp,
                         'payload_sha256': hashlib.sha256(frame.image_bytes).hexdigest(),
                         'page_assertion': False, 'pixel_trigger_only': True}
                emit('transition_question_trigger', **value)
                return value
        await asyncio.sleep(.25)
    raise TimeoutError('No user page transition detected: ' + phase)


async def phases(owner, args, result, emit):
    from plugin.plugins.hsr_companion.runtime import live_refresh
    from plugin.plugins.hsr_companion.runtime.build_info import BUILD_REVISION
    from plugin.sdk.plugin import NekoPluginBase

    signals = Path(args.signals).resolve()
    root = Path(__file__).resolve().parents[1]
    if not signals.is_relative_to(root / 'dist') or signals.exists():
        raise ValueError('Use a new plugin-owned marker directory')
    signals.mkdir()
    owner._D_abort_marker = signals / 'abort'
    result.update(build_revision=BUILD_REVISION, test_scope='D_frozen_C_three_real_transitions',
                  roundtrip=[], diagnostic_refresh_trace=[], host_chat_submissions=0,
                  original_archive_read_only=True, trigger='first_substantial_coarse_change_not_human_reply',
                  retries_per_destination=0, cat_paraphrase_acceptance=False)
    baseline = summary(getattr(owner, '_committed_observation', None))
    result['exploration_baseline'] = baseline
    if baseline.get('page') != 'exploration_hud':
        raise RuntimeError('Exploration baseline missing; no transition counted')

    events, submissions = [], []
    owner.ctx = SimpleNamespace(bus=NekoPluginBase(SimpleNamespace(plugin_id='isolated-roundtrip-D', metadata={},
        bus=SimpleNamespace(memory=SimpleNamespace(get=lambda **kw: list(events))))).bus)
    owner._target_character = lambda: 'hsr_isolated_test'
    owner.push_message = lambda **kw: submissions.append(kw) or {'submitted': True}
    current = {'phase': 'baseline'}
    original_trace = live_refresh.trace_refresh

    def trace(target, stage, **data):
        original_trace(target, stage, **data)
        if target is owner and len(result['diagnostic_refresh_trace']) < 5000:
            result['diagnostic_refresh_trace'].append({'at': time.time(), 'phase': current['phase'], 'stage': stage, **data})

    with patch.object(live_refresh, 'trace_refresh', trace):
        for phase, query, intent, fields, declaration, expected in steps():
            current['phase'] = phase
            # This diagnostic gate starts BEFORE asking the human to switch.
            # It never waits for a destination acknowledgement to ask the tool.
            trigger = await first_change(owner, emit, phase=phase, until=time.monotonic() + 300)
            before = len(submissions)
            row = {'phase': phase, 'expected_page': expected, 'trigger': trigger,
                   'prequery': summary(getattr(owner, '_committed_observation', None)), 'asked_at': time.time()}
            result['roundtrip'].append(row)  # Keep failures even if later code raises.
            kwargs = {'query': query, 'intent': intent}
            if fields is not None:
                kwargs['screen_fields'] = fields
            if declaration is not None:
                kwargs['consultation'] = declaration
            started = time.monotonic()
            packet = await owner.hsr_companion_prepare(**kwargs)
            row.update(packet=packet, direct_seconds=round(time.monotonic() - started, 3))
            emit('roundtrip_direct', phase=phase, seconds=row['direct_seconds'],
                 page=(packet.get('current_visual_evidence') or {}).get('page_type'),
                 answer=packet.get('query_answer'), assessment=(packet.get('guidance_answer') or {}).get('assessment'))
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
                            raise RuntimeError('Diagnostic operator aborted; retain partial run')
                        await asyncio.wait({task}, timeout=.25)
                    await task
                row['followup_status'] = copy.deepcopy(getattr(owner, '_followup_status', {}))
            else:
                row['local_confirmation'] = {'state': 'not_triggered'}
            row.update(submissions=copy.deepcopy([s for s in submissions[before:]
                if s.get('metadata', {}).get('kind') == 'query_followup']),
                total_seconds=round(time.monotonic() - started, 3),
                final_committed=summary(getattr(owner, '_committed_observation', None)),
                answer_deliveries=copy.deepcopy(getattr(owner, '_answer_deliveries', [])))
            emit('roundtrip_phase_finished', phase=phase, seconds=row['total_seconds'],
                 followup=row.get('followup_status'), submissions=len(row['submissions']),
                 final_page=row['final_committed'].get('page'))


async def run(args):
    from plugin.plugins.hsr_companion.tests import test_current_observation as fixtures

    root = Path(__file__).resolve().parents[1]
    if any(hashlib.sha256((root / name).read_bytes()).hexdigest() != digest for name, digest in FROZEN_C.items()):
        raise RuntimeError('C source changed; do not label this the frozen candidate')
    saved, original_fixture = pearl_archive(), fixtures.plugin_fixture
    original_collector = handoff.collect_probe_report

    def fixture(*values, **kwargs):
        owner, store, sent = original_fixture(*values, **kwargs)
        store.update(copy.deepcopy(saved))
        return owner, store, sent

    def collect(result, probes, checks):
        original_collector(result, probes, checks)
        result['test_scope'] = 'D_frozen_C_three_real_transitions'
        result['frozen_C_hashes_match'] = all(hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
            for name, digest in FROZEN_C.items())
        result['acceptance_harness_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

    args.delivery_phases, args.preload_backend = phases, True
    args.on_page_discovery = args.on_control_change = False
    with patch.object(fixtures, 'plugin_fixture', fixture), \
            patch.object(handoff, 'ReviewProbe', ReaderBudgetProbe), \
            patch.object(handoff, 'collect_probe_report', collect), \
            patch.object(socket.socket, 'connect', loopback_only(socket.socket.connect)):
        await handoff.run(args)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', required=True)
    parser.add_argument('--signals', required=True)
    parser.add_argument('--port', type=int, default=8481)
    asyncio.run(run(parser.parse_args()))
