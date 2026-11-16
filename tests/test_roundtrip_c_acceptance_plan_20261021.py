"""Checks the diagnostic scope, not real frame recognition or cat behavior."""
import asyncio
import socket
import time
from io import BytesIO
from types import SimpleNamespace

import pytest
from plugin.plugins.hsr_companion.scripts.verify_roundtrip_c_acceptance import (
    FROZEN_C,
    first_change,
    loopback_only,
    steps,
)


def test_D_has_only_three_transitions_and_preserves_real_requests():
    plan = steps()
    assert [r[0] for r in plan] == ['attributes', 'challenge', 'exploration_return']
    assert [r[-1] for r in plan] == ['character_detail', 'challenge_setup', 'exploration_hud']
    assert plan[0][2:5] == ('assess_panel', None,
        {'scope': 'current_panel', 'subject_state': 'specified', 'character': '真珠'})
    assert plan[1][3] == ['page', 'visible_text']
    assert plan[2][3] == ['location', 'quest']
    assert len(FROZEN_C) == 4


@pytest.mark.parametrize('host', ['127.0.0.1', '::1', 'localhost'])
def test_loopback_gate_keeps_local_host_helper_and_internal_loop_communication(host):
    called = []
    result = loopback_only(lambda sock, address: called.append(address) or 'ok')(socket.socket, (host, 8481))
    assert result == 'ok' and called == [(host, 8481)]


@pytest.mark.parametrize('address', [('example.com', 443), ('8.8.8.8', 443), 'external_socket'])
def test_D_never_uses_external_provider_or_real_cat_network(address):
    with pytest.raises(PermissionError):
        loopback_only(lambda *args: pytest.fail('must not connect'))(None, address)


def test_small_background_drift_does_not_trigger_question_before_substantial_page_change(tmp_path):
    from PIL import Image

    def sample(level, seq):
        stream = BytesIO()
        Image.new('RGB', (640, 400), (level, level, level)).save(stream, format='PNG')
        return SimpleNamespace(image_bytes=stream.getvalue(), frame_seq=seq, timestamp=time.time())

    frames = iter([sample(80, 1), sample(83, 2), sample(180, 3)])
    owner = SimpleNamespace(_assistant_client=SimpleNamespace(fetch_frame=lambda *args: next(frames)),
        _D_abort_marker=tmp_path / 'not-created')
    emitted = []
    with asyncio.Runner() as runner:
        result = runner.run(first_change(owner, lambda kind, **data: emitted.append((kind, data)),
            phase='attributes', until=time.monotonic() + 3))
    assert result['frame_seq'] == 3
    assert result['pixel_trigger_only'] and not result['page_assertion']
    assert [event[0] for event in emitted] == ['awaiting_transition', 'transition_question_trigger']
