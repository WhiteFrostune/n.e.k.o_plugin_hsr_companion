"""A new question may retire an old-page producer at its native checkpoint.

Uses the existing page reader on the SAME OCR thread. Pixels only request a
check; only a fresh, independently classified different page retires work.
"""
import time
from datetime import datetime, timezone

PAGE_CHECK_INTERVAL_SECONDS = .75
MAX_PAGE_CHECKS = 6


def current_demand(owner):
    """Only a live query/registered followup owns extra checkpoint work.

    These are the EXISTING caller deadlines, never a new handoff timeout.
    Metadata survives producer replacement, not question replacement.
    """
    if (getattr(owner, '_observer_stop_requested', False)
            or not getattr(owner, '_observer_status', {}).get('enabled')
            or getattr(owner, '_observer_status', {}).get('state') in {'paused', 'stopped', 'waiting_for_game'}):
        return None
    key = (getattr(owner, '_query_refresh_generation', 0), getattr(owner, '_answer_query_generation', 0))
    deadline = 0
    if getattr(owner, '_visual_query_waiters', 0):
        deadline = getattr(owner, '_query_refresh_deadline', 0)
    else:
        pending = getattr(owner, '_pending_answer', None)
        if pending is not None and getattr(pending, 'generation', None) == key[1]:
            deadline = pending.expires_at
    return (key, deadline) if deadline > time.monotonic() else None


def check_budget(owner):
    demand = current_demand(owner)
    if demand is None:
        return None
    key, deadline = demand
    budget = getattr(owner, '_page_check_budget', None)
    if budget is None or budget['key'] != key:
        owner._page_check_budget = budget = {
            'key': key, 'attempts': 0, 'next_at': 0, 'last_frame': None,
            'limit_traced': False, 'last_skipped_frame': None,
            'native_calls': 0, 'native_seconds': 0., 'baseline': None,
        }
    budget['deadline'] = deadline  # Acknowledgements cannot renew either caller deadline.
    return budget


def result_current(owner, result):
    demand = current_demand(owner)
    return bool(result and demand is not None and result.get('request_key') == demand[0]
                and time.monotonic() < result.get('deadline', 0)
                and result.get('revision') == (
                    getattr(owner, '_observation_epoch', 0), getattr(owner, '_answer_query_generation', 0),
                    getattr(owner, '_capture_failure_revision', 0)))


def queue_page_check(owner, original, preview, stop, *, frame_seq=None):
    reader = getattr(owner._vision, 'probe_scene', None)
    submit = getattr(owner._vision, 'enqueue_review', None)
    if not callable(reader) or not callable(submit):
        return None
    controls = (preview.get('recognition') or {}).get('scene_regions') or []
    expected = (preview.get('page') or {}).get('page_type')
    if not controls or expected in (None, 'unknown'):
        return None
    budget = check_budget(owner)
    if budget is None or time.monotonic() < budget['next_at']:
        return None
    if budget['attempts'] >= MAX_PAGE_CHECKS:
        if not budget['limit_traced']:
            from .live_refresh import trace_refresh
            trace_refresh(owner, 'page_check_stopped', reason='attempt_limit', attempt=budget['attempts'])
            budget['limit_traced'] = True
        return None
    frame_id = (getattr(owner, '_analysis_game_pid', 0), 'seq', frame_seq)
    if isinstance(frame_seq, int) and frame_seq > 0 and frame_id == budget['last_frame']:
        # Health already tells us that the helper has no new acquisition.
        # Do not enqueue another fetch/checkpoint or fill the bounded trace
        # with the same pending reason on every resident poll.
        if budget['last_skipped_frame'] != frame_id:
            from .live_refresh import trace_refresh
            trace_refresh(owner, 'page_check_result', reason='same_frame', attempt=budget['attempts'])
            budget['last_skipped_frame'] = frame_id
        return None
    request_key, deadline = budget['key'], budget['deadline']
    revision = (getattr(owner, '_observation_epoch', 0), getattr(owner, '_answer_query_generation', 0),
                getattr(owner, '_capture_failure_revision', 0))

    def obsolete():
        demand = current_demand(owner)
        return (stop.is_set() or getattr(owner, '_observer_stop_requested', False)
                or demand is None or demand[0] != request_key or time.monotonic() >= deadline
                or revision != (getattr(owner, '_observation_epoch', 0),
                                getattr(owner, '_answer_query_generation', 0),
                                getattr(owner, '_capture_failure_revision', 0)))

    def capture():
        from .query_reuse import LIVE_FRAME_MAX_AGE_SECONDS
        value = owner._assistant_client.fetch_frame(0)
        return value if (value is not None and 0 <= time.time() - value.timestamp <= LIVE_FRAME_MAX_AGE_SECONDS
            and value.game.get('pid') == getattr(owner, '_analysis_game_pid', 0)
            and value.game.get('running') and not value.game.get('minimized')
            and value.capture.get('state') == 'capturing' and value.capture.get('isolated_window') is True) else None

    def check():
        from .live_refresh import frame_stamp
        from .query_reuse import region_stamp, regions_changed
        started = time.monotonic()
        def outcome(reason, **data):
            budget['next_at'] = time.monotonic() + PAGE_CHECK_INTERVAL_SECONDS
            return {'reason': reason, 'request_key': request_key, 'deadline': deadline, 'revision': revision,
                    'attempt': budget['attempts'], 'probe_seconds': round(time.monotonic() - started, 3),
                    'native_calls': budget['native_calls'], 'native_seconds': round(budget['native_seconds'], 3), **data}
        if obsolete():
            return outcome('request_obsolete')
        first = capture()
        if first is None:
            return outcome('capture_unavailable')
        if obsolete():
            return outcome('request_obsolete')
        # Helpers can return the same cached frame with a newly fetched HTTP
        # response. Do not pay for another page OCR on that same acquisition.
        seq = getattr(first, 'frame_seq', 0)
        frame_id = (first.game.get('pid'), 'seq', seq) if isinstance(seq, int) and seq > 0 else (
            first.game.get('pid'), 'time', first.timestamp)
        if frame_id == budget['last_frame']:
            return outcome('same_frame')
        budget['last_frame'] = frame_id
        # Scheduling evidence ONLY. Never publish or amend the observation,
        # identity or field guards using this baseline. A new producer owns a
        # new source frame; the original request still owns the shared cap.
        baseline = budget['baseline']
        if baseline is None or baseline['producer'] is not stop or baseline['page'] != expected:
            budget['baseline'] = baseline = {'producer': stop, 'page': expected,
                'controls': controls, 'patches': region_stamp(original, controls)}
        checked_controls, before = baseline['controls'], baseline['patches']
        now = region_stamp(first.image_bytes, checked_controls)
        if len(now) != len(checked_controls) or len(before) != len(checked_controls):
            return outcome('page_controls_missing')
        if not regions_changed(before, now):
            return outcome('controls_unchanged')  # Camera/background is not a semantic switch.
        budget['attempts'] += 1  # Only an ACTUAL native page read spends this cap.
        budget['native_calls'] += 1
        native_started = time.monotonic()
        from ..adapters.native_text import ObservationSuperseded
        failure = None
        try:
            candidate = reader(first.image_bytes, cancelled=obsolete)
        except ObservationSuperseded:
            failure = 'request_obsolete' if obsolete() else 'probe_failed'
        except Exception:
            failure = 'probe_failed'
        finally:
            budget['native_seconds'] += time.monotonic() - native_started
        if failure is not None:
            return outcome(failure)
        page = (candidate.get('page') or {}).get('page_type')
        if obsolete():
            return outcome('request_obsolete')
        if not candidate.get('scene', {}).get('verified') or page in (None, 'unknown'):
            return outcome('unknown_page')
        proved = (candidate.get('recognition') or {}).get('scene_regions') or []
        latest = capture()
        if not proved:
            return outcome('page_controls_missing')
        if latest is None:
            return outcome('capture_unavailable')
        if obsolete():
            return outcome('request_obsolete')
        a, b = region_stamp(first.image_bytes, proved), region_stamp(latest.image_bytes, proved)
        if len(a) != len(proved) or regions_changed(a, b):
            return outcome('controls_changed')  # Never approve a second switch using the first page.
        if page == expected:
            # Remember what THIS check actually saw, instead of endlessly
            # comparing every acquisition to a superseded source rendering.
            # Unknown/unstable results do not learn a baseline. Another real
            # control change can still enqueue a later bounded page check.
            baseline.update(controls=proved, patches=b)
            return outcome('same_page', baseline_updated=True)
        return outcome('confirmed_transition', preview={**candidate, 'characters': [], 'screen_facts': {'facts': [], 'visible_text': []},
                            'observed_at': datetime.fromtimestamp(latest.timestamp, timezone.utc).isoformat()},
                       stamp=frame_stamp(latest.image_bytes), from_page=expected, to_page=page)

    # Reserve the interval BEFORE submit: an eager native checkpoint can finish
    # the callback immediately, and its completion-based cooldown must win.
    next_at = budget['next_at']
    budget['next_at'] = time.monotonic() + PAGE_CHECK_INTERVAL_SECONDS
    future = submit(check)
    if future is None:
        budget['next_at'] = next_at  # Busy admission neither spends nor resets the cap.
    return future
