"""C: real native checkpoint loop, synthetic frames/OCR; no live acceptance.

The old task remains resident while a question arrives. Unknown/old first
probes must not exhaust its only chance; cancellation and deadlines still win.
"""
import asyncio
import copy
import socket
import threading
import time
from types import SimpleNamespace

import pytest
from plugin.plugins.hsr_companion.adapters.local_vision import LocalVisionAdapter, serial_ocr
from plugin.plugins.hsr_companion.adapters.text_reader import check_current
from plugin.plugins.hsr_companion.adapters.text_verification import proof_key
from plugin.plugins.hsr_companion.runtime import live_refresh, page_handoff
from plugin.plugins.hsr_companion.tests.test_delivery_fix_20261012 import CONTROL, frame, picture
from plugin.plugins.hsr_companion.tests.test_review_policy_20261017 import resident_owner


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    with asyncio.Runner() as runner:
        runner.get_loop()  # Windows internal socketpair is not external traffic.
        monkeypatch.setattr(socket.socket, 'connect', lambda *a, **kw: (_ for _ in ()).throw(AssertionError('offline only')))
        monkeypatch.setattr(live_refresh, 'LIVE_POLL_SECONDS', .01)
        monkeypatch.setattr(page_handoff, 'PAGE_CHECK_INTERVAL_SECONDS', .025, raising=False)
        yield runner


def preview(page):
    return {'ok': True, 'page': {'page_type': page, 'label': page},
            'scene': {'primary_state': 'menu', 'verified': page != 'unknown', 'confidence': 'high',
                      'evidence': ['synthetic page controls']},
            'recognition': {'scene_regions': [{**CONTROL, 'text': page, 'control': True}]},
            'characters': [], 'screen_facts': {'facts': [], 'visible_text': []}}


async def harness(tmp_path, *, pages, duration=1, frozen_frame=False, invalidate=None, slow_probe=0,
                  start_page='exploration_hud', preexisting=False):
    owner, _, _, _ = await resident_owner(tmp_path)
    if start_page != 'exploration_hud':
        from plugin.plugins.hsr_companion.tests.test_chain_audit_20261017 import commit_raw, raw_page
        await commit_raw(owner, raw_page(start_page))
    original = copy.deepcopy(owner._committed_observation)
    entered, release = threading.Event(), threading.Event()
    calls, threads = [], []
    active = {'changed': False, 'seq': 1}
    owner._active_refresh_trace = {'started': time.monotonic(), 'stages': []}

    class Vision(LocalVisionAdapter):
        @serial_ocr(cooperative=True)
        def analyze(self, payload, *, cancelled=None, on_scene=None, wants_relic=None):
            on_scene(original)
            threads.append(threading.get_ident())
            entered.set()
            while not release.wait(.002):
                check_current(cancelled)
            calls.append('old_completed')
            return copy.deepcopy(original)

        @serial_ocr
        def probe_scene(self, payload, *, cancelled=None):
            threads.append(threading.get_ident())
            calls.append('probe')
            index = calls.count('probe') - 1
            if slow_probe:
                time.sleep(slow_probe)
            if invalidate is not None:
                invalidate(owner)
            check_current(cancelled)
            page = pages[min(index, len(pages) - 1)]
            if page == start_page and pages[-1] != start_page:
                # A later semantic switch must have a later control change.
                # First let the SAME-page check validate its current pixels;
                # the next acquisition then represents the actual new page.
                active['same_page_checked'] = 1
            return preview(page)

        @serial_ocr
        def verify_text_regions(self, payload, proofs, **kwargs):
            return {proof_key(p) for p in proofs}

    owner._vision = Vision(None)

    def capture(seq):
        value = frame(picture('page') if active['changed'] and active.get('same_page_checked') != 2 else picture())
        if active.get('same_page_checked') == 1:
            active['same_page_checked'] = 2
        value.frame_seq = active['seq']
        return value

    def health():
        if active['changed'] and not frozen_frame:
            active['seq'] += 1
        return {'connected': True, 'frame_seq': active['seq'], 'game': {'running': True, 'pid': 1},
                'capture': {'state': 'capturing', 'isolated_window': True}}

    owner._assistant_client = SimpleNamespace(health=health, fetch_frame=capture)
    if preexisting:
        owner._visual_query_waiters = 1
        owner._query_refresh_generation = 1
        owner._query_refresh_deadline = time.monotonic() + duration
    producer = asyncio.create_task(live_refresh.guarded_analysis(owner, picture(), helper=True))
    assert await asyncio.to_thread(entered.wait, 1)
    active.update(changed=True, seq=2)
    owner._visual_query_waiters = 1
    owner._query_refresh_generation = 1
    if not preexisting:
        owner._query_refresh_deadline = time.monotonic() + duration
    try:
        until = time.monotonic() + min(1.5, max(.3, duration + .15))
        while not producer.done() and time.monotonic() < until:
            await asyncio.sleep(.01)
        before_release = producer.done()
        epoch = getattr(owner, '_observation_epoch', 0)
        stages = copy.deepcopy(owner._active_refresh_trace['stages'])
    finally:
        release.set()
        await producer
        await live_refresh.drain_analysis_worker(owner)
    return owner, calls, threads, before_release, epoch, stages


@pytest.mark.parametrize('first', ['unknown', 'exploration_hud'])
@pytest.mark.parametrize('destination', ['character_detail', 'challenge_setup', 'relic_detail'])
def test_first_inconclusive_probe_can_progress_on_a_later_frame(tmp_path, offline, first, destination):
    owner, calls, threads, retired, _, stages = offline.run(harness(tmp_path, pages=[first, destination]))
    assert retired, 'First inconclusive probe exhausted the only handoff opportunity'
    assert calls.count('probe') == 2 and 'old_completed' not in calls
    assert owner._scene_preview['page']['page_type'] == destination
    assert len(set(threads)) == 1, 'Checkpoint must stay on the native producer thread'
    assert any(s['stage'] == 'page_check_result' and s['reason'] in {'unknown_page', 'same_page'} for s in stages)


@pytest.mark.parametrize('start', ['character_detail', 'challenge_setup'])
@pytest.mark.parametrize('preexisting', [False, True])
def test_return_to_exploration_and_already_waiting_requests_progress(tmp_path, offline, start, preexisting):
    owner, calls, _, retired, _, _ = offline.run(harness(tmp_path, pages=['unknown', 'exploration_hud'],
        start_page=start, preexisting=preexisting))
    assert retired and calls.count('probe') == 2 and 'old_completed' not in calls
    assert owner._scene_preview['page']['page_type'] == 'exploration_hud'


def test_repeated_unknown_is_bounded_and_keeps_original_deadline(tmp_path, offline):
    owner, calls, _, retired, _, stages = offline.run(harness(tmp_path, pages=['unknown'], duration=.14))
    assert not retired and 'old_completed' in calls
    assert 1 <= calls.count('probe') <= 6
    assert any(s['stage'] == 'page_check_result' and s['reason'] == 'unknown_page' for s in stages)
    assert owner._query_refresh_deadline < time.monotonic()


def test_identical_helper_frame_is_not_ocr_retried(tmp_path, offline):
    _, calls, _, retired, _, stages = offline.run(harness(tmp_path, pages=['unknown', 'character_detail'],
                                                        duration=.15, frozen_frame=True))
    assert not retired and calls.count('probe') == 1
    assert any(s['stage'] == 'page_check_result' and s['reason'] == 'same_frame' for s in stages)


@pytest.mark.parametrize('action', ['new_question', 'pause', 'failure'])
def test_probe_cannot_publish_after_its_request_or_capture_is_revoked(tmp_path, offline, action):
    def revoke(owner):
        if action == 'new_question':
            owner._answer_query_generation = getattr(owner, '_answer_query_generation', 0) + 1
            owner._visual_query_waiters = 0
        elif action == 'pause':
            owner._observer_stop_requested = True
        else:
            owner._capture_failure_revision = getattr(owner, '_capture_failure_revision', 0) + 1
            owner._visual_query_waiters = 0
    owner, _, _, _, _, _ = offline.run(harness(tmp_path, pages=['character_detail'], invalidate=revoke, duration=.12))
    assert (getattr(owner, '_scene_preview', None) or {}).get('page', {}).get('page_type') != 'character_detail'


def test_probe_finished_after_original_deadline_cannot_retire_worker(tmp_path, offline):
    owner, _, _, retired, _, _ = offline.run(harness(tmp_path, pages=['character_detail'], duration=.025, slow_probe=.08))
    assert not retired
    assert (getattr(owner, '_scene_preview', None) or {}).get('page', {}).get('page_type') != 'character_detail'


def test_probe_limit_is_shared_across_producers_not_normal_commits(offline):
    from concurrent.futures import Future
    calls = []
    seq = [1]
    def capture(_):
        seq[0] += 1
        value = frame(picture('page'))
        value.frame_seq = seq[0]
        return value
    def submit(callback):
        future = Future()
        future.set_result(callback())
        return future
    owner = SimpleNamespace(_observer_status={'enabled': True}, _visual_query_waiters=1,
        _query_refresh_generation=1, _query_refresh_deadline=time.monotonic() + 4,
        _analysis_game_pid=1, _assistant_client=SimpleNamespace(fetch_frame=capture),
        _vision=SimpleNamespace(probe_scene=lambda *a, **kw: calls.append(1) or preview('unknown'), enqueue_review=submit))
    stop = threading.Event()
    for serial in range(page_handoff.MAX_PAGE_CHECKS + 3):
        owner._observation_serial = serial  # Normal same-request commits cannot reset the cap.
        budget = page_handoff.check_budget(owner)
        budget['next_at'] = 0  # Time acceleration, NOT a production budget extension.
        page_handoff.queue_page_check(owner, picture(), preview('exploration_hud'), stop)
    assert len(calls) == page_handoff.MAX_PAGE_CHECKS
    owner._answer_query_generation = 1
    owner._query_refresh_generation = 2
    assert page_handoff.check_budget(owner)['attempts'] == 0


def test_confirmation_or_followup_does_not_restart_probe_work_or_deadline():
    owner = SimpleNamespace(_observer_status={'enabled': True}, _visual_query_waiters=1,
        _query_refresh_generation=1, _query_refresh_deadline=time.monotonic() + .2,
        _answer_query_generation=1)
    direct = page_handoff.check_budget(owner)
    direct['attempts'] = 2
    owner._visual_query_waiters = 0
    expires = time.monotonic() + .4
    owner._pending_answer = SimpleNamespace(generation=1, expires_at=expires)
    resumed = page_handoff.check_budget(owner)
    assert resumed is direct and resumed['attempts'] == 2 and resumed['deadline'] == expires
    assert page_handoff.check_budget(owner)['deadline'] == expires  # Repeated acknowledgement changes nothing.
    owner._pending_answer = None
    assert page_handoff.check_budget(owner) is None


def test_idle_resident_observer_does_not_schedule_extra_native_work():
    owner = SimpleNamespace(_observer_status={'enabled': True})
    assert page_handoff.current_demand(owner) is None


@pytest.mark.parametrize('failure', ['stale', 'wrong_process', 'minimized', 'controls_changed', 'no_controls'])
def test_new_page_still_requires_fresh_same_process_and_stable_controls(failure):
    from concurrent.futures import Future
    calls = []
    def capture(_):
        value = frame(picture('page') if not calls or failure != 'controls_changed' else picture())
        if failure == 'stale':
            value.timestamp -= 10
        elif failure == 'wrong_process':
            value.game['pid'] = 2
        elif failure == 'minimized':
            value.game['minimized'] = True
        calls.append('capture')
        return value
    def read(*a, **kw):
        value = preview('character_detail')
        if failure == 'no_controls':
            value['recognition']['scene_regions'] = []
        return value
    def submit(callback):
        future = Future()
        future.set_result(callback())
        return future
    owner = SimpleNamespace(_observer_status={'enabled': True}, _visual_query_waiters=1,
        _query_refresh_generation=1, _query_refresh_deadline=time.monotonic() + 1, _analysis_game_pid=1,
        _assistant_client=SimpleNamespace(fetch_frame=capture),
        _vision=SimpleNamespace(probe_scene=read, enqueue_review=submit))
    result = page_handoff.queue_page_check(owner, picture(), preview('exploration_hud'), threading.Event()).result()
    assert result['reason'] in {'capture_unavailable', 'controls_changed', 'page_controls_missing'}
    assert 'preview' not in result


def test_busy_checkpoint_admission_preserves_remaining_opportunity():
    owner = SimpleNamespace(_observer_status={'enabled': True}, _visual_query_waiters=1,
        _query_refresh_generation=1, _query_refresh_deadline=time.monotonic() + 1,
        _vision=SimpleNamespace(probe_scene=lambda *a: None, enqueue_review=lambda callback: None))
    for _ in range(10):
        assert page_handoff.queue_page_check(owner, picture(), preview('exploration_hud'), threading.Event()) is None
    assert page_handoff.check_budget(owner)['attempts'] == 0


def test_health_sequence_avoids_duplicate_checkpoint_fetches_and_trace_spam():
    def forbidden(*a, **kw):
        raise AssertionError('Known same frame must not enqueue native work')
    owner = SimpleNamespace(_observer_status={'enabled': True}, _visual_query_waiters=1,
        _query_refresh_generation=1, _query_refresh_deadline=time.monotonic() + 1, _analysis_game_pid=1,
        _vision=SimpleNamespace(probe_scene=forbidden, enqueue_review=forbidden),
        _active_refresh_trace={'started': time.monotonic(), 'stages': []})
    budget = page_handoff.check_budget(owner)
    budget.update(last_frame=(1, 'seq', 10), attempts=1)
    for _ in range(20):
        assert page_handoff.queue_page_check(owner, picture(), preview('exploration_hud'), threading.Event(), frame_seq=10) is None
    assert budget['attempts'] == 1
    assert len(owner._active_refresh_trace['stages']) == 1


def test_completed_probe_must_recheck_request_when_consumed():
    owner = SimpleNamespace(_observer_status={'enabled': True}, _visual_query_waiters=1,
        _query_refresh_generation=1, _answer_query_generation=1, _query_refresh_deadline=time.monotonic() + 1)
    result = {'request_key': (1, 1), 'deadline': owner._query_refresh_deadline, 'revision': (0, 1, 0)}
    assert page_handoff.result_current(owner, result)
    owner._answer_query_generation = 2
    assert not page_handoff.result_current(owner, result)


def test_diagnostic_keeps_probe_failure_cause_without_game_content():
    import json

    from plugin.plugins.hsr_companion.runtime.diagnostics import build_diagnostic_report
    from plugin.plugins.hsr_companion.tests.test_diagnostics import inputs
    data = inputs()
    data['last_tool'] = {'refresh_diagnostics': {'stages': [
        {'stage': 'page_check_result', 'reason': 'unknown_page', 'attempt': 2, 'probe_seconds': .4,
         'raw': 'DO_NOT_EXPORT'}, {'stage': 'page_check_stopped', 'reason': 'attempt_limit'}]}}
    result = json.loads(build_diagnostic_report(**data)['copy_text'])
    assert result['last_query']['stages'][0]['reason'] == 'unknown_page'
    assert result['last_query']['stages'][0]['attempt'] == 2
    assert result['last_query']['stages'][1]['reason'] == 'attempt_limit'
    assert 'DO_NOT_EXPORT' not in json.dumps(result)


@pytest.mark.parametrize('field', ['attributes', 'equipment', 'page'])
def test_retired_producer_is_followed_by_new_facts_at_the_formal_entry(tmp_path, offline, field):
    """Synthetic native backend, real refresh/admission/commit/formal payload.

    No historical frame is granted permission and no admission is mocked.
    This is not live OCR accuracy or autonomous cat parameter selection.
    """
    from plugin.plugins.hsr_companion.tests.test_chain_audit_20261017 import raw_page, simple_raw

    async def run():
        owner, _, _, _ = await resident_owner(tmp_path)
        original = copy.deepcopy(owner._committed_observation)
        new = (simple_raw('attributes', 'stat', '防御力', '3335') if field == 'attributes' else
               raw_page() if field == 'equipment' else raw_page('challenge_setup'))
        destination = new['page']['page_type']
        new['scene']['evidence'] = ['synthetic current-page controls']
        entered, release = threading.Event(), threading.Event()
        calls = []
        active = {'changed': False, 'seq': 1}

        class Vision(LocalVisionAdapter):
            @serial_ocr(cooperative=True)
            def analyze(self, payload, *, cancelled=None, on_scene=None, wants_relic=None):
                if payload == picture():
                    on_scene(original)
                    entered.set()
                    while not release.wait(.002):
                        check_current(cancelled)
                    calls.append('old_completed')
                    return copy.deepcopy(original)
                calls.append('new_completed')
                on_scene(preview(destination))
                return copy.deepcopy(new)

            @serial_ocr
            def analyze_scene(self, payload, **kwargs):
                calls.append('new_completed')
                return copy.deepcopy(new)

            @serial_ocr
            def probe_scene(self, payload, **kwargs):
                calls.append('probe')
                return preview('unknown' if calls.count('probe') == 1 else destination)

            @serial_ocr
            def verify_text_regions(self, payload, proofs, **kwargs):
                # The synthetic backend must NOT confirm old controls against
                # the new frame while the destination is still unproduced.
                return {proof_key(p) for p in proofs} if 'new_completed' in calls else set()

        def capture(_):
            value = frame(picture('page') if active['changed'] else picture())
            value.frame_seq = active['seq']
            return value

        def health():
            if active['changed']:
                active['seq'] += 1
            return {'connected': True, 'frame_seq': active['seq'], 'game': {'running': True, 'pid': 1},
                    'capture': {'state': 'capturing', 'isolated_window': True}}

        owner._vision = Vision(None)
        owner._assistant_client = SimpleNamespace(fetch_frame=capture, health=health)

        async def resident():
            result = await live_refresh.guarded_analysis(owner, picture(), helper=True)
            assert result is None, 'Confirmed switch must retire, not commit, the old page'
            result = await live_refresh.guarded_analysis(owner, picture('page'), helper=True)
            assert result is not None
            await owner._apply_local_analysis(result, image_fingerprint='synthetic-c-new',
                source='assistant_local_ocr', deliver_event=False)

        producer = asyncio.create_task(resident())
        assert await asyncio.to_thread(entered.wait, 1)
        active['changed'] = True
        try:
            result = await owner.hsr_companion_prepare(query='读取当前画面', intent='read_screen', screen_fields=[field])
            await asyncio.wait_for(producer, 4)
            assert result['query_answer']['state'] == 'ready', result.get('query_timing')
            assert result['current_visual_evidence']['page_type'] == destination
            assert calls.count('probe') == 2 and 'old_completed' not in calls
            assert 'new_completed' in calls
            assert not result['followup']['pending']
            values = result['current_visual_evidence']['screen_facts']['facts']
            assert not any(f['kind'] in {'current_location', 'tracked_quest', 'quest_objective'} for f in values)
            if field == 'equipment':
                assert len(result['equipment_answer']['sub_stats']) == 4
                assert result['equipment_answer']['main_stat']['value'] == '64.8%'
            elif field == 'attributes':
                assert any(f['value'] == '3335' for f in values)
        finally:
            release.set()
            await asyncio.gather(producer, return_exceptions=True)
            await live_refresh.drain_analysis_worker(owner)

    offline.run(run())
