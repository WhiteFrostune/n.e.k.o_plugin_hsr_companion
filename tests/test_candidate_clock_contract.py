"""Sub-microsecond ISO rounding must not drop a valid live candidate.

This does not relax fresh-frame, literal-value, identity or object admission.
"""
import asyncio
import copy
import math
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from plugin.plugins.hsr_companion.runtime import fact_lifecycle, live_refresh, query_reuse, review_policy
from plugin.plugins.hsr_companion.runtime.delivery_scope import scoped_guard
from plugin.plugins.hsr_companion.runtime.query_coordinator import answer_status
from plugin.plugins.hsr_companion.runtime.query_plan import build_query_plan
from plugin.plugins.hsr_companion.tests.test_chain_audit_20261017 import audit_owner, commit_raw, raw_page
from plugin.plugins.hsr_companion.tests.test_current_observation import ROLE
from plugin.plugins.hsr_companion.tests.test_delivery_fix_20261012 import CONTROL, frame, picture
from plugin.plugins.hsr_companion.tests.test_review_policy_20261017 import QUERY, resident_owner, select


def same_tick(monkeypatch):
    # Keep standalone candidates in the real projection's past. Integration
    # callers must install this clock BEFORE producing any observations.
    recorded = (datetime.now(timezone.utc) - timedelta(seconds=2)).replace(microsecond=184040)
    precise_now = math.nextafter(recorded.timestamp(), -math.inf)
    assert precise_now < recorded.timestamp()
    assert datetime.fromtimestamp(precise_now, timezone.utc) == recorded
    monkeypatch.setattr(fact_lifecycle, "time", SimpleNamespace(time=lambda: precise_now))
    return recorded


async def clocked_resident(tmp_path, monkeypatch):
    """One timeline for capture, raw observation, candidate and receipt.

    Pinning only a receipt after real-time commits could rewind the clock by
    almost a second. The raw observation then correctly failed future checks,
    masking the sub-microsecond behavior this test is supposed to exercise.
    """
    recorded = same_tick(monkeypatch)
    clock = SimpleNamespace(time=fact_lifecycle.time.time, monotonic=time.monotonic)
    for module in (query_reuse, live_refresh, review_policy):
        monkeypatch.setattr(module, "time", clock)
    entry = __import__("plugin.plugins.hsr_companion", fromlist=["utc_now_iso"])
    monkeypatch.setattr(entry, "utc_now_iso", lambda: recorded.isoformat())
    owner, values, sent, calls = await resident_owner(tmp_path)
    captured = frame(picture())
    captured.timestamp = clock.time()
    owner._assistant_client.fetch_frame = lambda seq: captured
    assert owner._committed_observation["observed_at"] == recorded.isoformat()
    return recorded, owner, values, sent, calls


def candidate(recorded):
    return {**raw_page("team_setup"), "source": "assistant_local_ocr",
        "observed_at": recorded.isoformat(), "observation_serial": 2,
        "runtime": {"stable_scene": "menu", "stable_character_ids": [ROLE["entity_id"]]},
        "characters": [{**ROLE, **CONTROL, "identity_scope": "team_selection"}]}


def test_same_tick_iso_rounding_does_not_become_a_future_candidate(monkeypatch):
    recorded = same_tick(monkeypatch)
    raw = candidate(recorded)
    before = copy.deepcopy(raw)
    value = fact_lifecycle.candidate_evidence(raw)
    assert value["fresh"]
    assert [a["name"] for a in value["characters"]] == ["银狼"]
    assert value["observed_at"] == recorded.isoformat()
    assert raw == before  # Never rewrite recognition time to make it fresh.


@pytest.mark.parametrize("ahead", [1, 1000, 1_000_000])
def test_representably_future_timestamps_still_rejected(monkeypatch, ahead):
    recorded = same_tick(monkeypatch)
    value = fact_lifecycle.candidate_evidence(candidate(recorded + timedelta(microseconds=ahead)))
    assert not value["fresh"]
    assert not value["characters"]


def test_rounded_second_confirmation_replaces_first_pending_cache(tmp_path, monkeypatch):
    recorded = same_tick(monkeypatch)
    owner, _, _ = audit_owner(tmp_path)
    raw = raw_page("team_setup")
    raw["characters"] = [{**ROLE, **CONTROL, "identity_scope": "team_selection"}]
    raw["screen_facts"]["facts"] = []
    entry = __import__("plugin.plugins.hsr_companion", fromlist=["utc_now_iso"])
    monkeypatch.setattr(entry, "utc_now_iso", lambda: (recorded - timedelta(microseconds=1)).isoformat())

    async def run():
        await commit_raw(owner, raw)
        assert owner._reusable_observations["full"]["evidence"]["characters"] == []
        monkeypatch.setattr(entry, "utc_now_iso", lambda: recorded.isoformat())
        await commit_raw(owner, raw)
        cached = owner._reusable_observations["full"]
        assert cached["evidence"]["screen_facts"]["observation_serial"] == owner._observation_serial == 2
        assert [a["name"] for a in cached["evidence"]["characters"]] == ["银狼"]
        plan = build_query_plan("画面上是谁")
        selected = scoped_guard(owner, cached["evidence"], plan, [cached["guard"]], proof_source=cached)
        assert selected is not None
        assert answer_status(plan, selected[0], owner._observer_status)["state"] == "ready"
        damaged = copy.deepcopy(cached)
        damaged["epoch"] += 1
        assert scoped_guard(owner, damaged["evidence"], plan, [damaged["guard"]], proof_source=damaged) is None

    asyncio.run(run())


def test_control_only_guard_cannot_claim_an_identity_answer(tmp_path):
    owner, _, _ = audit_owner(tmp_path)
    raw = raw_page("team_setup")
    raw["screen_facts"]["facts"] = []

    async def run():
        await commit_raw(owner, raw)
        cached = owner._reusable_observations["full"]
        plan = build_query_plan("画面上是谁")
        selected = scoped_guard(owner, cached["evidence"], plan, [cached["guard"]], proof_source=cached)
        assert selected is not None  # Page geometry exists, not identity proof.
        result = answer_status(plan, selected[0], owner._observer_status)
        assert result["state"] == "unconfirmed"
        assert result["confirmed_fields"] == []
        assert result["missing_fields"] == ["characters"]
        assert not selected[0]["can_answer_character_identity"]

    asyncio.run(run())


@pytest.mark.parametrize("boundary", ["candidate_review", "receipt_consumption"])
def test_iso_precision_is_shared_through_review_and_receipt(tmp_path, monkeypatch, boundary):
    async def run():
        recorded, owner, _, _, calls = await clocked_resident(tmp_path, monkeypatch)
        plan = build_query_plan(QUERY)
        if boundary == "candidate_review":
            # Hold time within the same native tick after the ISO observation
            # was rounded up. The capture clock itself stays full precision.
            for cached in owner._reusable_observations.values():
                cached["evidence"]["observed_at"] = recorded.isoformat()
        result = await select(owner, "full")
        assert result is not None
        if boundary == "receipt_consumption":
            result["reuse"]["validated_at"] = recorded.isoformat()
        def unexpected(*args):
            raise AssertionError("same-tick receipt must not recapture or rerun OCR")
        owner._assistant_client.fetch_frame = unexpected
        delivered = await live_refresh.validated_delivery(owner, result, plan=plan)
        assert delivered is not None and not calls
        assert answer_status(plan, delivered, owner._observer_status)["state"] == "ready"

    asyncio.run(run())


@pytest.mark.parametrize("offset,accepted", [(-1, False), (0, True), (2_000_000, True), (2_000_001, False)])
def test_receipt_future_and_expired_boundaries_remain_closed(tmp_path, monkeypatch, offset, accepted):
    async def run():
        recorded, owner, _, _, _ = await clocked_resident(tmp_path, monkeypatch)
        result = await select(owner, "full")
        assert result is not None
        plan = build_query_plan(QUERY)
        assert review_policy.checked_receipt(owner, result, plan=plan)
        result["reuse"]["validated_at"] = (recorded - timedelta(microseconds=offset)).isoformat()
        assert review_policy.checked_receipt(owner, result, plan=plan) is accepted

    asyncio.run(run())
