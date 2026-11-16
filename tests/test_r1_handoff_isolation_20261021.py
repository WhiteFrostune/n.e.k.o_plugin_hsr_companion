"""Diagnose the retained C timing failure, not a replacement acceptance gate.

The old one-second synthetic harness is unchanged. Record native checkpoints
and prove whether R1's completion code executes on this particular route.
Passing samples do not erase the original combined-run failure.
"""
import json

import pytest

from plugin.plugins.hsr_companion.runtime import followup, request_completion
from plugin.plugins.hsr_companion.tests import test_bounded_page_handoff_20261021 as original

offline = original.offline


@pytest.mark.parametrize("sample", range(5))
def test_original_page_handoff_does_not_execute_r1_completion(
        tmp_path, monkeypatch, offline, record_property, sample):
    touched = []

    def forbidden(*a, **kw):
        touched.append(True)
        raise AssertionError("This native-handoff-only harness must not execute R1 service completion")

    monkeypatch.setattr(followup, "request_completion", forbidden)
    monkeypatch.setattr(request_completion, "evaluate_request", forbidden)
    owner, calls, threads, retired, epoch, stages = offline.run(original.harness(
        tmp_path, pages=["unknown", "exploration_hud"], start_page="character_detail", preexisting=True))
    context = {"sample": sample, "calls": calls, "stages": stages, "retired": retired,
               "epoch": epoch, "native_threads": len(set(threads)), "r1_executions": len(touched)}
    record_property("handoff_context", json.dumps(context))
    assert not touched
    assert retired and calls.count("probe") == 2 and "old_completed" not in calls, context
    assert owner._scene_preview["page"]["page_type"] == "exploration_hud"
