"""One resident exploration -> character -> challenge -> exploration source run.

Reuses the approved isolated helper/pause/restore harness. Formal tool calls
are scripted, confirmation events are local fixtures, and no cat is messaged.
"""
import argparse
import asyncio
import json
import time
from pathlib import Path

from . import verify_page_activity_candidate as harness


async def phases(owner, args, result, emit):
    from plugin.plugins.hsr_companion.runtime.query_coordinator import satisfied_fields
    from plugin.plugins.hsr_companion.runtime.query_plan import build_query_plan

    signals = Path(args.signals).resolve()
    root = Path(__file__).resolve().parents[1]
    if not signals.is_relative_to(root / 'dist') or signals.exists():
        raise ValueError('Use a new plugin-owned marker directory')
    signals.mkdir()
    result['major_page_cycle'] = []
    raw = getattr(owner, '_committed_observation', None) or {}
    if raw.get('page', {}).get('page_type') != 'exploration_hud':
        raise RuntimeError('Exploration baseline not confirmed; do not count the cycle')
    publisher = owner._publish_scene_preview

    async def traced_preview(value):
        before = getattr(owner, '_observation_epoch', 0)
        await publisher(value)
        emit('page_preview', page=value.get('page', {}).get('page_type'),
             epoch_before=before, epoch_after=getattr(owner, '_observation_epoch', 0))

    owner._publish_scene_preview = traced_preview

    async def formal(phase, query, intent, fields=None, consultation=None):
        import time
        started = time.monotonic()
        kwargs = {'query': query, 'intent': intent}
        if fields is not None:
            kwargs['screen_fields'] = fields
        if consultation is not None:
            kwargs['consultation'] = consultation
        packet = await owner.hsr_companion_prepare(**kwargs)
        visual = packet.get('current_visual_evidence') or {}
        row = {'phase': phase, 'seconds': round(time.monotonic() - started, 3),
               'intent': intent, 'plan': packet.get('query_plan'), 'page': visual.get('page_type'),
               'answer': packet.get('query_answer'), 'timing': packet.get('query_timing'),
               'facts': visual.get('screen_facts', {}).get('facts', []),
               'guidance': packet.get('guidance_answer'), 'followup': packet.get('followup'),
               'serial': getattr(owner, '_observation_serial', 0)}
        result['major_page_cycle'].append(row)
        emit('formal_entry', phase=phase, seconds=row['seconds'], page=row['page'],
             answer_state=row['answer'].get('state'), fact_count=len(row['facts']),
             followup_pending=row['followup'].get('pending'), plan=row['plan'])
        pending = getattr(owner, '_pending_answer', None)
        if pending is not None:
            before = len(local_submissions)
            deadline, generation = pending.expires_at, owner._answer_query_generation
            simulated_events.append({'type': 'user_message', 'lanlan': 'hsr_isolated_test', 'content': '好', '_ts': time.time()})
            confirmation = await owner.hsr_companion_prepare(query='好', pending_reply={
                'request_id': pending.request_id, 'relation': 'confirm', 'player_message': '好'})
            row['confirmation'] = {'accepted': confirmation['pending_reply']['accepted'],
                'same_deadline': pending.expires_at == deadline,
                'same_generation': owner._answer_query_generation == generation}
            emit('confirmation', phase=phase, **row['confirmation'])
            task = getattr(owner, '_followup_task', None)
            if task is not None:
                await task
            row['followup_status'] = getattr(owner, '_followup_status', {})
            row['followup_submissions'] = [s.get('metadata') for s in local_submissions[before:]
                if s.get('metadata', {}).get('kind') == 'query_followup']
            emit('followup_finished', phase=phase, status=row['followup_status'],
                 submissions=len(row['followup_submissions']))
        row['required_fields'] = sorted(build_query_plan(query, intent, screen_fields=fields).fields)
        row['direct_satisfied_fields'] = sorted(satisfied_fields(build_query_plan(query, intent, screen_fields=fields), visual))

    # Access only the LOCAL fake bus/fixture lists installed by the harness.
    # No host memory, real conversation or real cat notification is involved.
    from types import SimpleNamespace

    from plugin.sdk.plugin import NekoPluginBase
    simulated_events, local_submissions = [], []
    owner.ctx = SimpleNamespace(bus=NekoPluginBase(SimpleNamespace(plugin_id='isolated-major-page-handoff',
        metadata={}, bus=SimpleNamespace(memory=SimpleNamespace(get=lambda **kw: list(simulated_events))))).bus)
    owner._target_character = lambda: 'hsr_isolated_test'
    owner.push_message = lambda **kw: local_submissions.append(kw) or {'submitted': True}

    await formal('exploration_resident', '现在在哪？任务目标是什么？', 'read_screen', ['location', 'quest'])
    sequence = [
        ('character', '真珠这面板怎么样？接下来怎么养？', 'assess_panel', None,
         {'scope': 'current_panel', 'subject_state': 'specified', 'character': '真珠'}),
        ('challenge', '现在是什么挑战？页面上写了什么？', 'explain_page', ['page', 'visible_text'], None),
        ('exploration_return', '现在在哪？任务目标是什么？', 'read_screen', ['location', 'quest'], None),
    ]
    for phase, query, intent, fields, declaration in sequence:
        import time
        emit('awaiting_transition', phase=phase, marker=str(signals / phase))
        until = time.monotonic() + 300
        while not (signals / phase).exists():
            if time.monotonic() >= until:
                raise TimeoutError('User transition missing: ' + phase)
            await asyncio.sleep(.25)
        # No warm-up or manual refresh: use the same already resident owner.
        await formal(phase, query, intent, fields, declaration)
    result['local_followup_submissions'] = [s.get('metadata') for s in local_submissions
        if s.get('metadata', {}).get('kind') == 'query_followup']


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', required=True)
    parser.add_argument('--signals', required=True)
    parser.add_argument('--port', type=int, default=8453)
    args = parser.parse_args()
    args.host = 'http://127.0.0.1:48916'
    args.pending_protocol = args.current_item = args.three_fields_signals = args.three_fields_current = False
    original_action, original_context = harness.ui_action, harness.ui_context

    def action(base, name):
        started = time.monotonic()
        print(json.dumps({'kind': 'environment_action_started', 'action': name}), flush=True)
        try:
            value = original_action(base, name)
        except Exception as exc:
            raise RuntimeError(name + ': ' + type(exc).__name__) from exc
        print(json.dumps({'kind': 'environment_action_completed', 'action': name,
                          'seconds': round(time.monotonic() - started, 3)}), flush=True)
        return value

    def context(base):
        started = time.monotonic()
        value = original_context(base)
        print(json.dumps({'kind': 'environment_state_read', 'enabled': value.get('observer', {}).get('enabled'),
                          'seconds': round(time.monotonic() - started, 3)}), flush=True)
        return value

    harness.ui_action, harness.ui_context = action, context
    harness.delivery_phases = phases
    asyncio.run(harness.verify(args))
