"""Isolated completion regressions; no live OCR, game or host submissions.

Each supplied packet owns its fields. In particular, the later slot is not
merged into earlier numbers: the final packet must independently be usable.
"""
import asyncio
import copy
import time
from dataclasses import replace

import pytest
from plugin.plugins.hsr_companion.runtime import followup_delivery as followup
from plugin.plugins.hsr_companion.runtime.query_plan import build_query_plan
from plugin.plugins.hsr_companion.tests import test_guidance as cases
from plugin.plugins.hsr_companion.tests.controlled_time import ManualClock, deadlock_guard
from plugin.plugins.hsr_companion.tests.test_page_followup import followup_owner
from plugin.plugins.hsr_companion.tests.test_stage1_repairs import fact, visual

book, catalog = cases.book, cases.catalog
QUERY = "这件遗器适合真珠吗？"


def numbers(*, slot=False):
    value = visual(fact("main_stat", "主词条·速度", "25"),
                   fact("sub_stat", "副词条·防御力", "12.9%"),
                   fact("sub_stat", "副词条·暴击率", "3%"),
                   fact("sub_stat", "副词条·生命值", "80"),
                   fact("sub_stat", "副词条·效果抵抗", "5%"))
    if slot:
        value["screen_facts"]["facts"].append(fact("equipment_slot", "遗器部位", "脚部"))
    return value


def pending(book, catalog, *, duration=1, clock=None):
    selected = cases.plan(QUERY, book, catalog)
    plan = selected.observation_plan(build_query_plan(QUERY))
    start = (clock.monotonic if clock is not None else time.monotonic)()
    return followup.AnswerRequest("slot-late", QUERY, "猫娘", time.time(), start,
        start + duration, "unknown", False, False, True, False,
        plan=plan, guidance_plan=selected, guidance_profile={})


def test_empty_advice_waits_for_later_self_contained_equipment_packet(book, catalog, monkeypatch):
    clock = ManualClock()
    clock.install(monkeypatch, followup, drive_sleep=True, expire_events=True)
    first, final = numbers(), numbers(slot=True)
    first["screen_facts"]["facts"][0]["value"] = "19"
    owner, sent = followup_owner(monkeypatch, value=first)
    owner._guidance_book, owner._catalog = book, catalog
    owner._visual_update_event = asyncio.Event()
    reads, stages = [], []
    request = pending(book, catalog, clock=clock)
    before = copy.deepcopy(first)

    async def current():
        reads.append(1)
        # No usable advice on the first read; the next whole packet includes
        # both slot AND its own numbers. Never borrow the first packet's slot.
        return copy.deepcopy(first if len(reads) == 1 else final)

    original = followup.request_completion

    def traced(*args, **kwargs):
        state, guidance = original(*args, **kwargs)
        stages.append(state.public())
        return state, guidance

    owner._current_visual_evidence = current
    monkeypatch.setattr(followup, "request_completion", traced)

    async def run():
        owner._pending_answer = request
        await followup.deliver_followup(owner, request)

    asyncio.run(deadlock_guard(run()))
    assert stages[0]["facts_ready"] and not stages[0]["service_ready"]
    assert len(reads) >= 2 and len(sent) == 1, owner._followup_status
    assert owner._followup_status["answer_complete"]
    assert request.expires_at - request.armed_at == pytest.approx(1, abs=1e-9)
    data = owner._test_answer_payloads[-1]
    assert data["guidance_answer"]["completion"]["evaluation_complete"]
    assert data["equipment_answer"]["slot"] == "脚部"
    assert data["equipment_answer"]["main_stat"]["value"] == "25"
    assert first == before
    assert "速度25" not in sent[0]["parts"][0]["text"]  # Ordinary advice is not a readout.


@pytest.mark.parametrize("end", ["deadline", "cancel", "epoch", "slot_only"])
def test_empty_advice_cannot_send_or_complete_without_valid_evidence(book, catalog, monkeypatch, end):
    clock = ManualClock()
    clock.install(monkeypatch, followup, drive_sleep=True, expire_events=True)
    owner, sent = followup_owner(monkeypatch, value=numbers())
    owner._guidance_book, owner._catalog = book, catalog
    request = pending(book, catalog, duration=.06 if end == "deadline" else .35 if end == "slot_only" else 1, clock=clock)
    reads = []

    async def current():
        reads.append(1)
        if len(reads) == 2 and end == "cancel":
            followup.cancel_followup(owner, "user_cancelled")
        if len(reads) == 2 and end == "epoch":
            owner._observation_epoch += 1
            owner._observation_invalidated = True
            return numbers(slot=True)
        if len(reads) >= 2 and end == "slot_only":
            return visual(fact("equipment_slot", "遗器部位", "脚部"))
        return numbers()

    owner._current_visual_evidence = current

    async def run():
        owner._pending_answer = request
        await followup.deliver_followup(owner, request)

    asyncio.run(deadlock_guard(run()))
    assert not sent and not owner._test_answer_payloads
    assert owner._pending_answer is None
    assert not owner._followup_status.get("answer_complete")
    if end != "epoch":
        assert owner._followup_status["reason"] == ("user_cancelled" if end == "cancel" else "no_new_verified_facts")


def test_deadline_keeps_ready_result_distinct_from_failed_speech_transport(monkeypatch):
    from plugin.plugins.hsr_companion.tests.test_page_followup import request
    clock = ManualClock()
    clock.install(monkeypatch, followup, drive_sleep=True, expire_events=True)
    owner, sent = followup_owner(monkeypatch)
    pending_request = replace(request(), armed_at=clock.monotonic(), expires_at=clock.monotonic() + .035)

    async def unavailable(*args, **kwargs):
        return "unavailable", None

    monkeypatch.setattr(followup, "latest_user_activity", unavailable)

    async def run():
        owner._pending_answer = pending_request
        await followup.deliver_followup(owner, pending_request)

    asyncio.run(deadlock_guard(run()))
    assert not sent and owner._followup_status["result_ready"]
    assert owner._followup_status["reason"] == "activity_unavailable_after_retries"

