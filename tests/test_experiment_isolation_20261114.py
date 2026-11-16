from __future__ import annotations

import asyncio
import importlib
import time
from types import SimpleNamespace

import pytest
from plugin.plugins.hsr_companion.runtime.engine import CompanionRuntime
from plugin.plugins.hsr_companion.runtime.speech import SpeechArbiter
from plugin.plugins.hsr_companion.tests.test_current_observation import analysis, plugin_fixture


@pytest.mark.parametrize("stored,enabled", [({}, False), ({"proactive_enabled": False}, False),
                                            ({"proactive_enabled": True}, True)])
def test_observation_start_uses_saved_choice_or_quiet_default(tmp_path, stored, enabled):
    owner, values, _ = plugin_fixture(tmp_path)
    values["observer_settings"] = stored.copy()
    owner._start_data_pack_task = lambda: None
    owner._start_observer_task = lambda: None

    async def no_context(*args, **kwargs):
        return {}

    owner._start_companion_session = owner._set_user_operation = no_context
    asyncio.run(owner.start_auto_observation())
    assert owner._observer_status["proactive_enabled"] is enabled
    assert not values["observer_settings"]["idle_chatter_enabled"]
    assert not values["observer_settings"]["extended_guidance_enabled"]


@pytest.mark.parametrize("proactive,idle,expected", [(False, True, False), (True, False, False), (True, True, True)])
def test_idle_candidate_requires_both_switches(proactive, idle, expected):
    runtime = CompanionRuntime(global_rate_seconds=0, idle_chatter_seconds=30)
    frame = analysis(roles=False, scene="exploration")
    runtime.ingest(frame, now=10, proactive_enabled=proactive, idle_chatter_enabled=idle)
    event, decisions = runtime.ingest(frame, now=41, proactive_enabled=proactive, idle_chatter_enabled=idle)
    assert runtime.stable_scene == "exploration"
    if expected:
        assert event.kind == "exploration_idle" and event.proactive
    else:
        assert event is None and decisions == []


def test_silence_skips_proactive_delivery_and_reenabling_restores_scene_cue(tmp_path, monkeypatch):
    owner, _, sent = plugin_fixture(tmp_path)
    owner._speech_arbiter = SpeechArbiter(hold_seconds=0)
    checked = []

    async def validate(_):
        checked.append("frame")
        return True

    async def probe():
        checked.append("transport")
        return {"available": True}

    async def activity():
        return True

    owner._check_message_path, owner._refresh_user_activity = probe, activity
    monkeypatch.setattr(importlib.import_module(type(owner).__module__), "validate_before_delivery", validate)

    async def apply(scene):
        return await owner._apply_local_analysis(
            analysis(roles=False, scene=scene), image_fingerprint=scene, source="test")

    async def run():
        await owner.set_proactive_companionship(False)
        assert not (await apply("combat"))["delivery"]["submitted"]
        assert owner._runtime.stable_scene == "combat"
        assert not getattr(owner, "_speech_task", None) and not checked
        assert sent and all(item["ai_behavior"] == "read" for item in sent)
        await owner.set_proactive_companionship(True)
        await apply("warp")
        assert (await apply("warp"))["delivery"]["queued"]
        await owner._speech_task
        assert checked == ["transport", "frame"]
        assert sum(item["ai_behavior"] == "respond" for item in sent) == 1

    asyncio.run(run())


def test_switching_off_idle_cancels_only_idle_speech(tmp_path):
    owner, values, sent = plugin_fixture(tmp_path)
    owner._runtime = CompanionRuntime(global_rate_seconds=0, idle_chatter_seconds=30)

    async def run():
        await owner.set_proactive_companionship(True)
        await owner.set_experimental_features(idle_chatter_enabled=True)
        await owner._apply_local_analysis(analysis(roles=False, scene="exploration"),
                                          image_fingerprint="exploration", source="test")
        owner._runtime.stable_scene_since = time.monotonic() - 31
        owner._runtime.last_meaningful_event_at = time.monotonic() - 31
        result = await owner._apply_local_analysis(analysis(roles=False, scene="exploration"),
                                                   image_fingerprint="idle", source="test")
        assert result["delivery"]["event_kind"] == "exploration_idle" and result["delivery"]["queued"]
        speech_task = owner._speech_task
        request = owner._pending_answer = SimpleNamespace(guidance_plan=None)
        await owner.set_experimental_features(idle_chatter_enabled=False, extended_guidance_enabled=False)
        assert owner._pending_answer is request
        assert owner._get_speech_arbiter().pending is None
        await asyncio.gather(speech_task, return_exceptions=True)
        assert values["observer_settings"]["proactive_enabled"] and not values["observer_settings"]["idle_chatter_enabled"]
        assert all(item["ai_behavior"] == "read" for item in sent)

    asyncio.run(run())


@pytest.mark.parametrize("query,intent", [("眼前这个关卡怎么配队？", "challenge_guide"),
                                          ("本期混沌普通12层怎么配队？", "conversation")])
def test_specialty_guidance_off_stops_before_capture_and_does_not_arm_followup(tmp_path, monkeypatch, query, intent):
    owner, _, sent = plugin_fixture(tmp_path)

    def forbidden(*args, **kwargs):
        pytest.fail("A disabled experiment must not read pixels, wait for data, or evaluate candidates")

    module = importlib.import_module(type(owner).__module__)
    monkeypatch.setattr(module, "resolve_query", forbidden)
    monkeypatch.setattr(module, "build_guidance", forbidden)
    owner._wait_for_data_pack = forbidden
    result = asyncio.run(owner.hsr_companion_prepare(query=query, intent=intent))
    assert result["guidance_answer"]["state"] == "experiment_disabled"
    assert result["request_completion"]["service_ready"] is False
    assert not result["followup"]["pending"] and not sent
