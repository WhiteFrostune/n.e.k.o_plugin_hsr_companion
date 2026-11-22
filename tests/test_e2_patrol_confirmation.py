"""E2 scheduling, identity failure, and diagnostic-only contracts."""
import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from plugin.plugins import hsr_companion as entry
from plugin.plugins.hsr_companion.runtime import engine, live_refresh, patrol, recognition_metrics
from plugin.plugins.hsr_companion.runtime.engine import CompanionRuntime
from plugin.plugins.hsr_companion.runtime.manual_refresh import cancel_manual_refresh, request_manual_refresh
from plugin.plugins.hsr_companion.tests.controlled_time import ManualClock, deadlock_guard
from plugin.plugins.hsr_companion.tests.test_chain_audit_20261017 import audit_owner, commit_raw, raw_page
from plugin.plugins.hsr_companion.tests.test_current_observation import ROLE
from plugin.plugins.hsr_companion.tests.test_delivery_fix_20261012 import CONTROL
from plugin.plugins.hsr_companion.tests.test_static_patrol import static_owner


def test_same_static_commit_gets_a_bounded_age_refresh(tmp_path, monkeypatch):
    owner, image = static_owner(tmp_path)
    epoch = time.time()
    elapsed = [0.0]
    monkeypatch.setattr(patrol, "time", SimpleNamespace(
        time=lambda: epoch + elapsed[0], monotonic=lambda: elapsed[0],
    ))
    observed = datetime.fromtimestamp(epoch - 29, timezone.utc).isoformat()
    owner._committed_observation["observed_at"] = observed

    async def check():
        await patrol.remember_capture(owner, image)

        async def scan(seq, offset):
            elapsed[0] = offset
            return await patrol.should_scan(owner, image, requested=False, changed=False,
                                            frame_seq=seq, captured_at=epoch + offset, pid=1)

        assert not await scan(1, 1)
        assert not await scan(2, 2)
        owner._region_query_waiters = 1
        assert not await scan(3, 4)
        owner._region_query_waiters = 0
        assert await scan(4, 5)
        assert owner._patrol_status["reason"] == "age_budget"
        assert not await scan(5, 32)
        assert not await scan(6, 44)
        assert await scan(7, 45)
        assert owner._patrol_status["age_refreshes"] == 2
        assert owner._committed_observation["observed_at"] == observed
        assert owner._last_full_commit_serial == 4

    asyncio.run(check())


def identity_page(*actors):
    raw = raw_page("character_detail")
    raw["characters"] = list(actors)
    raw["screen_facts"]["facts"] = []
    return raw


@pytest.mark.parametrize("reason", ["insufficient_confirmations", "confirmation_reset", "ocr_timeout", "yielded_for_query"])
def test_identity_failure_records_the_blocker_and_preserves_evidence_gate(reason, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(engine, "time", SimpleNamespace(monotonic=lambda: now[0]))
    runtime = CompanionRuntime()
    actor = {**ROLE, "identity_scope": "detail_subject", "slot": 1}
    runtime.ingest(identity_page(actor))
    current = runtime.identity_confirmation()
    assert current["state"] == "pending"
    assert not runtime.stable_character_ids
    now[0] = 120.0
    if reason == "confirmation_reset":
        runtime.ingest(identity_page())
    elif reason == "ocr_timeout":
        different = identity_page()
        different["recognition"]["text_reader"] = {"errors": ["ocr_time_budget"]}
        runtime.ingest(different)
    elif reason == "yielded_for_query":
        live_refresh._full_yielded(SimpleNamespace(_runtime=runtime, _vision_job_id=1), "ocr")
    now[0] = 169.9
    assert runtime.identity_confirmation()["state"] == "pending"
    now[0] = 170.0
    assert runtime.identity_confirmation() == {
        "state": "failed", "reason": reason, "elapsed_seconds": 70.0, "limit_seconds": 70.0,
    }
    # Neither elapsed time nor retry increases the number of matching reads.
    assert not runtime.stable_character_ids
    runtime.retry_identity_confirmation()
    assert runtime.identity_confirmation()["state"] == "pending"
    runtime.ingest(identity_page(actor))
    runtime.ingest(identity_page(actor))
    assert runtime.stable_character_ids == (actor["entity_id"],)
    assert runtime.identity_confirmation() is None
    runtime.observe_identity_page("character_detail")
    runtime.invalidate_observation(reason="new_page_title")
    assert runtime.identity_confirmation() is None


def test_failed_confirmation_is_visible_in_panel_and_official_tool(tmp_path, monkeypatch):
    owner, _, _ = audit_owner(tmp_path)
    now = [100.0]
    monkeypatch.setattr(engine, "time", SimpleNamespace(monotonic=lambda: now[0]))

    async def no_current_identity(*args, **kwargs):
        return None

    monkeypatch.setattr(entry, "refresh_for_query", no_current_identity)

    async def check():
        await commit_raw(owner, identity_page({**ROLE, **CONTROL, "identity_scope": "detail_subject"}))
        now[0] += 71
        panel = await owner._current_visual_evidence()
        assert panel["identity_confirmation"]["state"] == "failed"
        assert not panel["characters"]
        result = await owner.hsr_companion_prepare(query="你看到的是谁哇", intent="read_screen",
                                                  screen_fields=["characters"])
        answer = result["screen_identity_answer"]
        assert answer["confirmation"]["reason"] == "insufficient_confirmations"
        assert answer["names"] == [] and not answer["allowed"]
        assert not result["followup"]["pending"]
        assert not result["current_visual_evidence"]["page_characters"]
        manual = request_manual_refresh(owner)
        assert manual["state"] == "running"
        try:
            assert owner._runtime.identity_confirmation()["state"] == "pending"
        finally:
            cancel_manual_refresh(owner)
            await asyncio.gather(owner._manual_refresh_task, return_exceptions=True)

    asyncio.run(deadlock_guard(check()))


def test_page_first_short_window_counts_only_missing_identity(tmp_path, monkeypatch):
    owner, _, _ = audit_owner(tmp_path)
    clock = ManualClock()
    clock.install(monkeypatch, live_refresh)
    real_reuse = live_refresh.try_reuse

    async def delayed_reuse(*args, **kwargs):
        value = await real_reuse(*args, **kwargs)
        if kwargs.get("pixel_only") and "characters" in kwargs["plan"].fields:
            clock.advance(.351)
        return value

    monkeypatch.setattr(live_refresh, "try_reuse", delayed_reuse)

    async def check():
        raw = raw_page("team_setup")
        raw["characters"] = [{**ROLE, **CONTROL, "identity_scope": "team_selection"}]
        raw["screen_facts"]["facts"] = []
        await commit_raw(owner, raw)
        await commit_raw(owner, raw)
        result = await owner.hsr_companion_prepare(query="现在是什么场景？都有谁？")
        assert result["query_answer"]["state"] == "partial"
        assert result["query_answer"]["missing_fields"] == ["characters"]
        health = recognition_metrics.observation_health(owner, result["current_visual_evidence"])
        assert health["page_only_reuse_timeouts"] == 1

    asyncio.run(deadlock_guard(check()))


def test_recorders_are_best_effort_even_if_their_storage_raises(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("No diagnostic storage")

    owner = SimpleNamespace()
    monkeypatch.setattr(recognition_metrics, "_metrics", broken)
    recognition_metrics.record_full(owner, job_id=1, state="parsed")
    recognition_metrics.record_late_discard(owner)
    recognition_metrics.record_page_only_timeout(owner)
    monkeypatch.setattr(recognition_metrics, "_utc", broken)
    events = []
    recognition_metrics.count_reset(events, "slot_replaced", slots=1)
    assert events == []
