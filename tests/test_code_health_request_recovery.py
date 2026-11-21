"""Page-first is useful only if the SAME request can finish its missing field.

Synthetic images, public bus substitute, actual prepare/follow-up/formatter.
No host model, live game, real message submission or changed wait budgets.
"""
import asyncio
from types import SimpleNamespace

import pytest
from plugin.plugins.hsr_companion.runtime import followup_delivery as followup
from plugin.plugins.hsr_companion.runtime import live_refresh, query_reuse
from plugin.plugins.hsr_companion.runtime.delivery_scope import coordinates
from plugin.plugins.hsr_companion.runtime.observation_store import ObservationStore
from plugin.plugins.hsr_companion.tests.test_chain_audit_20261017 import audit_owner, commit_raw, raw_page
from plugin.plugins.hsr_companion.tests.test_current_observation import ROLE
from plugin.plugins.hsr_companion.tests.test_delivery_fix_20261012 import CONTROL
from plugin.sdk.plugin import NekoPluginBase


@pytest.mark.parametrize("finish", ["recover", "cancel", "invalidate"])
def test_partial_page_request_recovers_identity_without_new_commit(tmp_path, monkeypatch, finish):
    owner, _, sent = audit_owner(tmp_path)
    owner._target_character = lambda: "isolated-test-cat"
    owner.ctx = SimpleNamespace(bus=NekoPluginBase(SimpleNamespace(metadata={}, plugin_id="isolated-health-recovery",
        bus=SimpleNamespace(memory=SimpleNamespace(get=lambda **kw: [])))).bus)
    monkeypatch.setattr(followup, "FOLLOWUP_GRACE_SECONDS", .01)
    monkeypatch.setattr(followup, "FOLLOWUP_SECONDS", 1.5)
    available = asyncio.Event()
    original = query_reuse.try_reuse
    attempts = []

    async def unavailable_identity(target, **kwargs):
        fields = kwargs["plan"].fields
        attempts.append((fields, available.is_set()))
        if "characters" in fields and not available.is_set():
            return None
        return await original(target, **kwargs)

    monkeypatch.setattr(live_refresh, "try_reuse", unavailable_identity)
    monkeypatch.setattr(query_reuse, "try_reuse", unavailable_identity)

    async def run():
        raw = raw_page("team_setup")
        identity = {**ROLE, **CONTROL, "identity_scope": "team_selection",
                    "region": {"left": .6, "top": .7, "right": .8, "bottom": .75}}
        raw["characters"] = [identity]
        raw["screen_facts"]["facts"] = []
        await commit_raw(owner, raw)
        await commit_raw(owner, raw)
        serial = owner._observation_serial
        # Model a resident reviewer withholding ONLY the challenged name.
        # The complete candidate and its original guard remain in the cache.
        ObservationStore(owner).defer_regions({coordinates(identity)})
        first = await owner.hsr_companion_prepare(query="这是什么页面，都有谁？", intent="read_screen",
            screen_fields=["page", "characters"])
        assert first["query_answer"]["confirmed_fields"] == ["page"]
        assert first["query_answer"]["missing_fields"] == ["characters"]
        assert not first["current_visual_evidence"]["characters"]
        request, task = owner._pending_answer, owner._followup_task
        assert request is not None and first["followup"]["pending"]
        deadline = request.expires_at
        if finish == "cancel":
            followup.cancel_followup(owner, "user_cancelled")
        elif finish == "invalidate":
            owner._observation_epoch = getattr(owner, "_observation_epoch", 0) + 1
            owner._observation_invalidated = True
        else:
            available.set()
        try:
            outcomes = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=2.5)
            assert outcomes == [None] or finish == "cancel" and isinstance(outcomes[0], asyncio.CancelledError)
            pushes = [message for message in sent if message.get("metadata", {}).get("kind") == "query_followup"]
            assert owner._observation_serial == serial
            assert request.expires_at == deadline
            if finish == "recover":
                assert len(pushes) == 1
                push = pushes[0]
                assert push["metadata"]["request_id"] == request.request_id
                assert push["metadata"]["outcome"] == "ready"
                assert ROLE["name"] in str(push["parts"])
                assert owner._pending_answer is None
                assert any("characters" in fields and ready for fields, ready in attempts)
            else:
                assert not pushes
        finally:
            followup.cancel_followup(owner, "test_cleanup")
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
