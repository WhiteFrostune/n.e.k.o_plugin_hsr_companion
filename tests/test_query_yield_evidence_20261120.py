"""0.9.7: a query-preempted frame must not erase the last complete observation."""
from __future__ import annotations

import asyncio
import copy
import threading
import time
from types import SimpleNamespace

from plugin.plugins.hsr_companion.adapters.text_reader import QueryPreempted
from plugin.plugins.hsr_companion.runtime.live_refresh import guarded_analysis
from plugin.plugins.hsr_companion.runtime.observation_store import ObservationStore
from plugin.plugins.hsr_companion.tests.test_current_observation import analysis, plugin_fixture
from plugin.plugins.hsr_companion.tests.test_delivery_fix_20261012 import picture


def test_yielded_frame_does_not_replace_the_last_complete_observation(tmp_path):
    owner, _, _ = plugin_fixture(tmp_path)

    async def run():
        complete = analysis(scene="menu")
        await owner._apply_local_analysis(complete, image_fingerprint="complete", source="assistant_local_ocr")
        await owner._apply_local_analysis(complete, image_fingerprint="complete", source="assistant_local_ocr")
        before = copy.deepcopy(owner._committed_observation)
        yielded = analysis(roles=False, scene="menu")
        yielded["recognition"] = {
            "yielded_for_query": True,
            "portrait_templates": {"available": False, "reason": "yielded_for_query"},
        }
        result = await owner._apply_local_analysis(
            yielded, image_fingerprint="yielded", source="assistant_local_ocr")
        assert result["delivery"]["reason"] == "analysis_yielded_incomplete"
        assert owner._committed_observation == before
        assert owner._last_successful_observation == before
        current = await ObservationStore(owner).current()
        assert current["observed_at"] == before["observed_at"]
        assert current["characters"] == before["characters"]

    asyncio.run(run())


def test_query_preempting_an_in_progress_analysis_keeps_the_last_complete_observation(tmp_path):
    owner, _, _ = plugin_fixture(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def analyze_live(_payload, **_kwargs):
        entered.set()
        release.wait(2)
        raise QueryPreempted()

    owner._vision = SimpleNamespace(analyze_live=analyze_live)
    owner._visual_query_waiters = 0
    owner._query_refresh_generation = 0
    owner._query_refresh_deadline = 0

    async def run():
        complete = analysis(scene="menu")
        await owner._apply_local_analysis(complete, image_fingerprint="complete",
                                          source="assistant_local_ocr")
        await owner._apply_local_analysis(complete, image_fingerprint="complete",
                                          source="assistant_local_ocr")
        before = copy.deepcopy(owner._committed_observation)
        task = asyncio.create_task(guarded_analysis(owner, picture(), helper=False))
        assert await asyncio.to_thread(entered.wait, 2)
        # The question starts while the producer is still running; the next
        # native boundary raises QueryPreempted and no result is committed.
        owner._visual_query_waiters = 1
        owner._query_refresh_generation += 1
        owner._query_refresh_deadline = time.monotonic() + 1
        owner._region_query_waiters = 1
        release.set()
        assert await asyncio.wait_for(task, 5) is None
        assert owner._committed_observation == before
        current = await ObservationStore(owner).current()
        assert current["observed_at"] == before["observed_at"]
        assert current["characters"] == before["characters"]

    asyncio.run(run())
