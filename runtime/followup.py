"""One explicit pending answer, independent of unsolicited idle chatter.

Only plugin tool calls create obligations. Never parse an LLM's '稍等' prose.
Requests and text live in memory only; SDK submission is not a playback receipt.
"""

from __future__ import annotations

import asyncio
import time
from copy import deepcopy
from dataclasses import dataclass, replace
from uuid import uuid4

from .chat_activity import latest_user_activity
from .live_refresh import validated_delivery
from .navigation import navigation_answer
from .observation_store import ObservationSnapshot
from .pending_reply import message_allows, relation_contract
from .query_coordinator import answer_status, has_answerable_content, satisfied_fields
from .query_plan import QueryPlan, build_query_plan
from .request_completion import evaluate_request
from .service_contract import record_answer_delivery, response_contract

DIRECT_WAIT_SECONDS = 8.0
FOLLOWUP_SECONDS = 35.0
FOLLOWUP_GRACE_SECONDS = 5.0
ACTIVITY_ATTEMPTS = 3
ACTIVITY_TRANSPORT_SECONDS = 1.0


@dataclass(frozen=True)
class AnswerRequest:
    request_id: str
    query: str
    target: str
    asked_at: float
    armed_at: float
    expires_at: float
    page_type: str
    scene_only: bool
    navigation: bool
    content: bool
    controlled: bool
    plan: QueryPlan | None = None
    guidance_plan: object | None = None
    guidance_profile: dict | None = None
    generation: int = 0
    discussion_generation: int | None = None
    delivered_fields: frozenset[str] = frozenset()
    delivered_key: str = ""
    delivered_reply: dict | None = None


def request_plan(request: AnswerRequest) -> QueryPlan:
    if request.plan is not None:
        return request.plan
    # Compatibility for older internal callers/tests. Production requests carry
    # the entry's actual plan; they never reclassify the prompt or its truncation.
    plan = build_query_plan(request.query)
    if request.navigation:
        fields = frozenset({"location", "quest"})
    elif request.scene_only:
        fields = frozenset({"location" if any(w in request.query for w in ("在哪", "哪里", "哪儿")) else "page"})
    elif request.content:
        fields = plan.topics
    else:
        fields = frozenset({"controlled_character" if request.controlled else "characters"})
    return replace(plan, screen=True, scene_only=request.scene_only, navigation=request.navigation,
                   content=request.content, controlled=request.controlled, fields=fields)


def answer_ready(visual: dict, request: AnswerRequest) -> bool:
    """Field readiness only; a role assessment has a separate completion gate."""
    plan = request_plan(request)
    return bool(plan.fields and plan.fields <= satisfied_fields(plan, visual))


def request_completion(owner, visual, request, *, guidance=None):
    """Evaluate the service on this packet BEFORE selecting speech/progress."""
    if guidance is None and request.guidance_plan is not None:
        from .guidance import build_guidance
        guidance = build_guidance(request.guidance_plan, request.query, book=owner._guidance_book,
                                  catalog=owner._catalog, profile=request.guidance_profile or {}, visual=visual)
    return evaluate_request(request_plan(request), visual,
                            completion=(guidance or {}).get("completion")), guidance


def reply_data(visual: dict, request: AnswerRequest, *, catalog=None) -> dict:
    """Only requested fields, not a second unsolicited roster announcement."""
    result = {"observed_at": visual.get("observed_at"), "page_type": visual.get("page_type"),
              "scene": visual.get("scene"), "instruction": visual.get("instruction")}
    plan = request_plan(request)
    if plan.fields & {"location", "quest"}:
        result["navigation"] = navigation_answer(visual, requested=True)
    if plan.fields & {"characters", "controlled_character"}:
        result["characters"] = ([visual["controlled_character"]] if visual.get("controlled_character") else []) if request.controlled else visual.get("characters", [])
    if plan.content and not plan.navigation:
        screen = visual.get("screen_facts") or {}
        result["screen_facts"] = {"facts": plan.relevant_facts(screen.get("facts") or []),
                                  "visible_text": screen.get("visible_text", []) if "visible_text" in plan.fields else []}
        if "equipment" in plan.fields:
            from .equipment import equipment_card
            result["equipment_answer"] = equipment_card(visual)
            if catalog is not None:
                from .grounding import build_grounding
                result["grounding"] = build_grounding(request.query, visual, catalog, plan=plan)
    return result


def cancel_followup(owner, reason: str) -> None:
    request = getattr(owner, "_pending_answer", None)
    owner._pending_answer = None
    owner._followup_confirmation = None
    owner._followup_relation_hold = None
    owner._followup_message_wait = None
    if request is not None:
        status = getattr(owner, "_followup_status", {})
        owner._followup_status = {"request_id": request.request_id, "state": "cancelled", "reason": reason,
                                  "result_ready": status.get("request_id") == request.request_id and status.get("result_ready") is True}
    task = getattr(owner, "_followup_task", None)
    if task and not task.done() and task is not asyncio.current_task():
        task.cancel()


def _cancel_current(owner, request: AnswerRequest, reason: str) -> None:
    # An await may have allowed a NEW question to replace this obligation.
    # The obsolete worker must never cancel the new worker's request.
    if getattr(owner, "_pending_answer", None) is request:
        cancel_followup(owner, reason)


def _can_observe(owner) -> bool:
    return bool(owner._observer_status.get("enabled")
                and not getattr(owner, "_observer_stop_requested", False)
                and owner._observer_status.get("state") not in {"stopped", "paused", "waiting_for_game"})


async def checked_activity(owner, request: AnswerRequest, bus) -> tuple[str, float | None]:
    """Retry transient read failures, never treat failed reads as silence."""
    for attempt in range(ACTIVITY_ATTEMPTS):
        if (getattr(owner, "_pending_answer", None) is not request or not _can_observe(owner)
                or owner._target_character() != request.target):
            return "cancelled", None
        remaining = request.expires_at - time.monotonic()
        if remaining <= 0:
            return "unavailable", None
        if getattr(owner, "_followup_relation_hold", None) == request.request_id:
            return "awaiting_relation", None
        # This is the speech guard's IPC budget, not the OCR/query deadline.
        # Measured healthy reads can exceed .25s. One worker/no backlog remains.
        activity, stamp = await latest_user_activity(bus, target=request.target, limit=8,
            include_identity=True,
            transport_timeout=min(ACTIVITY_TRANSPORT_SECONDS, remaining),
            wait_timeout=min(ACTIVITY_TRANSPORT_SECONDS + .1, remaining))
        if getattr(owner, "_pending_answer", None) is not request:
            return "cancelled", None
        if activity != "unavailable":
            return activity, stamp
        owner._followup_status = {**getattr(owner, "_followup_status", {}), "request_id": request.request_id, "state": "pending",
                                  "reason": "activity_retry", "activity_attempt": attempt + 1}
        if attempt + 1 == ACTIVITY_ATTEMPTS or time.monotonic() >= request.expires_at:
            break
        await asyncio.sleep(min(.25 * (attempt + 1), max(0, request.expires_at - time.monotonic())))
    return "unavailable", None


async def retry_activity_within_request(owner, request: AnswerRequest) -> bool:
    """A transient read failure suspends sending, not the original obligation.

    No deadline renewal or blind submission. A replacement question/pause can
    cancel while asleep; only the same request may try again inside its budget.
    """
    if getattr(owner, "_pending_answer", None) is not request:
        return False
    remaining = request.expires_at - time.monotonic()
    if remaining <= 0:
        reason = "message_relation_unresolved" if getattr(owner, "_followup_message_wait", None) == request.request_id else "activity_unavailable_after_retries"
        _cancel_current(owner, request, reason)
        return False
    owner._followup_status = {**getattr(owner, "_followup_status", {}), "request_id": request.request_id, "state": "pending",
                              "reason": "activity_retry"}
    await asyncio.sleep(min(.5, remaining))
    return getattr(owner, "_pending_answer", None) is request


async def wait_message_relation(owner, request):
    """Unknown relation suspends speech, not recognition or the obligation."""
    if getattr(owner, "_pending_answer", None) is not request:
        return False
    remaining = request.expires_at - time.monotonic()
    if remaining <= 0:
        _cancel_current(owner, request, "message_relation_unresolved")
        return False
    owner._followup_status = {**getattr(owner, "_followup_status", {}),
        "request_id": request.request_id, "state": "pending", "reason": "awaiting_message_relation"}
    owner._followup_message_wait = request.request_id
    await asyncio.sleep(min(.25, remaining))
    if time.monotonic() >= request.expires_at:
        _cancel_current(owner, request, "message_relation_unresolved")
        return False
    return getattr(owner, "_pending_answer", None) is request


async def _wait_for_update(owner, request):
    """Yield inside the original deadline, including silent evidence progress."""
    remaining = request.expires_at - time.monotonic()
    if remaining <= 0:
        return
    event = getattr(owner, "_visual_update_event", None)
    if event is None:
        await asyncio.sleep(min(.25, remaining))
    else:
        event.clear()
        try:
            await asyncio.wait_for(event.wait(), timeout=min(.5, remaining))
        except TimeoutError:
            pass


def arm_followup(owner, *, query: str, visual: dict, asked_at: float, scene_only: bool,
                 navigation: bool, content: bool, controlled: bool, plan: QueryPlan | None = None,
                 guidance_plan=None, guidance_profile=None, guidance_answer=None) -> dict:
    target = owner._target_character()
    unavailable = {"pending": False, "instruction": "本次没有后台补答；说明尚未确认，不要承诺稍后回复。"}
    if not target or not _can_observe(owner):
        return {**unavailable, "reason": "observer_or_target_unavailable"}
    try:
        bus = owner.bus
    except AttributeError:
        bus = None
    if not callable(getattr(getattr(bus, "memory", None), "get", None)):
        return {**unavailable, "reason": "activity_guard_unsupported"}
    now = time.monotonic()
    generation = getattr(owner, "_answer_query_generation", 0)
    service = getattr(owner, "_service_request", {})
    request_id = service.get("request_id") if service.get("generation") == generation else None
    request = AnswerRequest(request_id or uuid4().hex, str(query)[:240], target, asked_at, now, now + FOLLOWUP_SECONDS,
                            str(visual.get("page_type") or "unknown"), scene_only, navigation, content, controlled, plan,
                            guidance_plan, deepcopy(guidance_profile), generation, getattr(owner, "_discussion_generation", 0))
    progress, selected_guidance = request_completion(owner, visual, request, guidance=guidance_answer)
    if progress.service_ready:
        return {"pending": False, "reason": "answer_available"}
    if not progress.can_wait:
        return {**unavailable, "reason": progress.state}
    request = replace(request, delivered_fields=progress.confirmed, delivered_key=progress.progress_key,
                      delivered_reply=deepcopy((selected_guidance or {}).get("reply_plan")))
    cancel_followup(owner, "replaced")
    owner._cancel_pending_speech("explicit_followup")
    owner._pending_answer = request
    owner._followup_status = {"request_id": request.request_id, "state": "pending"}
    owner._followup_task = asyncio.create_task(deliver_followup(owner, request), name="hsr-explicit-answer")
    wake = getattr(owner, "_observer_wake_event", None)
    if wake is not None:
        wake.set()
    return {"pending": True, "request_id": request.request_id, "expires_in_seconds": FOLLOWUP_SECONDS,
            "pending_reply_contract": relation_contract(request),
            "instruction": "已登记本次问题的后台补答；只需简短说明未确认部分，不要说游戏加载、保证马上认出或要求玩家重复提问。"
                           "仅获得新增已确认信息时补答；到期仍无新信息只在插件状态结束等待，不另发失败消息，不承诺一定再回复。"}


async def deliver_followup(owner, request: AnswerRequest) -> None:
    try:
        delivered = set(request.delivered_fields)
        delivered_key = request.delivered_key
        delivered_reply = request.delivered_reply
        partial_sent = False

        def selected_reply(guidance):
            from .consultation import for_followup
            return for_followup((guidance or {}).get("reply_plan") or {}, previous=delivered_reply)

        def advances(state, guidance, visual):
            if state.service_ready:
                return True
            if selected_reply(guidance).get("answer_increment") is False:
                return False
            changed = state.progress_key != delivered_key
            if not state.can_wait:
                return changed and bool(state.confirmed)
            return not partial_sent and (bool(state.confirmed - delivered)
                # The same readiness policy used by direct/cache selection
                # exposes useful PARTIAL equipment too. A missing fourth row
                # must not prevent its existing reply_plan from reaching the
                # normal proof gate; completion/one-progress limits stay intact.
                or bool(request.guidance_plan is not None and changed
                        and has_answerable_content(request_plan(request), visual)))

        def expire():
            if getattr(owner, "_pending_answer", None) is request:
                previous = getattr(owner, "_followup_status", {})
                owner._pending_answer = None
                blocked = {"activity_retry": "activity_unavailable_after_retries",
                           "awaiting_message_relation": "message_relation_unresolved"}.get(previous.get("reason"))
                reason = blocked or ("service_incomplete" if request.guidance_plan is not None and delivered >= request_plan(request).fields
                                     else "partial_delivered_missing_fields" if partial_sent else "no_new_verified_facts")
                owner._followup_status = {"request_id": request.request_id, "state": "expired",
                    "outcome": "partial" if partial_sent else "timeout",
                    "reason": reason, "confirmed_fields": sorted(delivered), "result_ready": previous.get("result_ready") is True,
                    "answer_complete": False, "request_completion": previous.get("request_completion")}
        # Do not race the original tool-result reply. This is a grace period,
        # not a guarantee of host speech mutual exclusion.
        await asyncio.sleep(FOLLOWUP_GRACE_SECONDS)
        while getattr(owner, "_pending_answer", None) is request:
            if request.generation != getattr(owner, "_answer_query_generation", request.generation):
                _cancel_current(owner, request, "new_question")
                return
            if time.monotonic() >= request.expires_at:
                expire()
                return
            if (request.discussion_generation is not None
                    and request.discussion_generation != getattr(owner, "_discussion_generation", 0)):
                _cancel_current(owner, request, "discussion_changed")
                return
            if not _can_observe(owner) or owner._target_character() != request.target:
                _cancel_current(owner, request, "observer_or_target_changed")
                return
            try:
                bus = owner.bus
            except AttributeError:
                bus = None
            # Result progress is independent of the optional speech-activity
            # transport. Never skip reading/recovering data because IPC failed.
            visual = await owner._current_visual_evidence()
            if getattr(owner, "_pending_answer", None) is not request:
                return
            if (request.page_type != "unknown" and visual.get("scene", {}).get("verified")
                    and visual.get("page_type") != request.page_type):
                _cancel_current(owner, request, "page_changed")
                return
            state, guidance = request_completion(owner, visual, request)
            ready = state.service_ready
            recovered_snapshot = None
            if (state.can_wait and getattr(owner, "_reusable_observations", None)
                    and not getattr(owner, "_observation_invalidated", False)
                    and time.monotonic() < request.expires_at):
                # A busy page-control verifier masks current(), but the same
                # scoped snapshot can become provable when its OCR gate frees.
                # Recheck the cache's real guards; do not require another full
                # commit or resurrect unvalidated fields from panel history.
                from .query_reuse import try_reuse
                plan = request_plan(request)
                try:
                    recovered = await asyncio.wait_for(try_reuse(owner, navigation=plan.navigation,
                        full=not (plan.navigation or plan.scene_only), plan=plan),
                        timeout=min(2.5, max(.001, request.expires_at - time.monotonic())))
                except TimeoutError:
                    recovered = None
                if recovered is not None:
                    from .query_coordinator import answer_coverage
                    # A degraded recovery must not replace a richer candidate.
                    # Keeping current is NOT admission: validated_delivery below
                    # still checks that whole packet's own page/object proofs.
                    recovered_state, recovered_guidance = request_completion(owner, recovered, request)
                    if (recovered_state.service_ready or answer_coverage(plan, recovered) >= answer_coverage(plan, visual)):
                        visual = recovered
                        state, guidance = recovered_state, recovered_guidance
                        ready = state.service_ready
                        recovered_snapshot = ObservationSnapshot.select(owner, recovered, fields=plan.fields)
            progress = advances(state, guidance, visual) and not ready
            validated = None
            transport = None
            if ready or progress:
                epoch = getattr(owner, "_observation_epoch", 0)
                check_path = getattr(owner, "_check_message_path", None)
                if callable(check_path):
                    # Keep this exact candidate across the new await. A
                    # revision can change without invalidating the whole page.
                    before_probe = recovered_snapshot or ObservationSnapshot.select(
                        owner, visual, fields=request_plan(request).fields)
                    transport = await check_path()
                    if getattr(owner, "_pending_answer", None) is not request:
                        return
                    if time.monotonic() >= request.expires_at:
                        expire()
                        return
                    # Only use the snapshot comparison for a new commit.
                    # The normal live review still owns admission, including
                    # recovery from a transient capture or control failure.
                    if (epoch != getattr(owner, "_observation_epoch", 0)
                            or (before_probe.serial != getattr(owner, "_observation_serial", 0)
                                and not before_probe.matches(owner))):
                        await _wait_for_update(owner, request)
                        continue
                if recovered_snapshot is not None:
                    # try_reuse checked THIS detached packet's own pixels. A
                    # second check against a newer partial commit's guard would
                    # reject the good packet or certify a different object.
                    validated = recovered_snapshot.matches(owner)
                else:
                    try:
                        checked = await asyncio.wait_for(validated_delivery(owner, visual,
                            plan=request.plan), timeout=2.5)
                        validated = checked is not None
                        if validated:
                            visual = checked
                            state, guidance = request_completion(owner, visual, request)
                            ready = state.service_ready
                            recovered_snapshot = ObservationSnapshot.select(owner, visual, fields=request_plan(request).fields)
                    except TimeoutError:
                        validated = False
                if not validated:
                    ready = False
                    progress = False
                elif (epoch != getattr(owner, "_observation_epoch", 0)
                      or recovered_snapshot is not None and not recovered_snapshot.matches(owner)):
                    ready = False
                    progress = False
                else:
                    progress = advances(state, guidance, visual) and not ready
            if getattr(owner, "_pending_answer", None) is not request:
                return
            status = state.public() if validated is not False else {
                "facts_ready": False, "service_ready": False, "state": "awaiting_validation"}
            owner._followup_status = {"request_id": request.request_id, "state": "pending", "result_ready": ready,
                                      "request_completion": status}
            if time.monotonic() >= request.expires_at:
                expire()
                return
            if not state.can_wait and not ready and not progress and validated is not False:
                # Missing rules or a known different subject cannot be fixed
                # by requesting more pixels. Do not manufacture an empty push.
                owner._pending_answer = None
                owner._followup_status.update(state="ended_incomplete", reason=state.state, answer_complete=False)
                return
            activity, stamp = await checked_activity(owner, request, bus)
            if activity == "awaiting_relation":
                if await wait_message_relation(owner, request):
                    continue
                return
            if activity == "unavailable":
                if await retry_activity_within_request(owner, request):
                    continue
                return
            if activity != "available":
                _cancel_current(owner, request, "activity_unavailable_after_retries")
                return
            if not message_allows(owner, request, stamp):
                if not await wait_message_relation(owner, request):
                    return
                continue
            if time.monotonic() >= request.expires_at:
                # The SDK cannot retract a submitted notice. Do not queue a
                # redundant failure that may play after the next good answer.
                # Closing locally leaves successful scoped followups intact.
                expire()
                return
            if ready or progress:
                # Compile the model-facing notification BEFORE the final async
                # guards. A cold host formatter can do IO; never block here or
                # claim a request before that await can be preempted/cancelled.
                data = reply_data(visual, request, catalog=getattr(owner, "_catalog", None))
                confirmed = state.confirmed
                data["answer_progress"] = {"complete": state.facts_ready, "confirmed_fields": sorted(confirmed),
                    "missing_fields": sorted(request_plan(request).fields - confirmed)}
                data["request_completion"] = state.public()
                guidance_context = None
                if guidance is not None:
                    guidance = deepcopy(guidance)
                    guidance["reply_plan"] = selected_reply(guidance)
                    guidance_context = guidance.pop("context", None)
                    if guidance_context is not None:
                        guidance_context["question_offered"] = False
                    data["guidance_answer"] = guidance
                data["response_contract"] = response_contract(owner, request.generation, channel="followup",
                                                              request_id=request.request_id)
                from .answer_message import render_followup
                silent = bool((guidance or {}).get("reply_plan", {}).get("answer_increment") is False)
                text, message_projection = (None, {}) if silent else await asyncio.to_thread(
                    render_followup, data, plan=request_plan(request), ready=True)
                if not message_projection.get("has_content", True):
                    if state.can_wait:
                        # Reading progress is not necessarily useful speech.
                        # Keep the SAME obligation for later self-contained
                        # evidence; an empty brief answer cannot complete it.
                        owner._followup_status.update(reason="awaiting_answer_content")
                        await _wait_for_update(owner, request)
                        continue
                    _cancel_current(owner, request, "empty_projection")
                    return
                # Final async guard, then synchronous claim/submission.
                activity, stamp = await checked_activity(owner, request, bus)
                if activity == "awaiting_relation":
                    if await wait_message_relation(owner, request):
                        continue
                    return
                if activity == "unavailable":
                    if await retry_activity_within_request(owner, request):
                        continue
                    return
                if (getattr(owner, "_pending_answer", None) is not request or activity != "available"
                        or not _can_observe(owner)
                        or owner._target_character() != request.target):
                    _cancel_current(owner, request, "final_guard_rejected")
                    return
                if request.generation != getattr(owner, "_answer_query_generation", request.generation):
                    _cancel_current(owner, request, "new_question")
                    return
                if time.monotonic() >= request.expires_at:
                    expire()
                    return
                if not message_allows(owner, request, stamp):
                    if not await wait_message_relation(owner, request):
                        return
                    continue
                if (request.discussion_generation is not None
                        and request.discussion_generation != getattr(owner, "_discussion_generation", 0)):
                    _cancel_current(owner, request, "discussion_changed")
                    return
                if (epoch != getattr(owner, "_observation_epoch", 0)
                              or getattr(owner, "_observation_invalidated", False)
                              or recovered_snapshot is not None and not recovered_snapshot.matches(owner)):
                    continue
                if silent:
                    # Same validated answer, now service-complete. End locally
                    # without claiming another submission/host utterance.
                    if ready:
                        owner._pending_answer = None
                        owner._followup_status.update(state="completed_silent", reason="no_answer_increment",
                            result_ready=True, answer_complete=False)
                    if ready:
                        return
                    continue
                owner._cancel_pending_speech("covered_by_followup")
                # One partial progress notice plus one eventual full result at
                # most. No deadline extension or repeated per-frame notices.
                terminal = ready or not state.can_wait or not message_projection["complete"]
                if terminal:
                    owner._pending_answer = None
                if guidance_context is not None:
                    owner._guidance_context = guidance_context
                answer_complete = bool(ready and message_projection["complete"]
                                       and message_projection.get("answer_complete", True))
                outcome = ("ready" if answer_complete else "partial") if message_projection["complete"] else "transport_limited"
                pushed = owner.push_message(**({"_message_transport": transport} if transport is not None else {}),
                                            visibility=[], ai_behavior="respond", source="hsr_companion",
                                            target_lanlan=request.target, priority=5,
                                            coalesce_key="hsr_explicit_answer",
                                            parts=[{"type": "text", "text": text}],
                                            metadata={"kind": "query_followup", "request_id": request.request_id,
                                                      "origin": "hsr_explicit_followup", "generation": request.generation,
                                                      "outcome": outcome,
                                                      "message_projection": message_projection})
                owner._followup_status = {"request_id": request.request_id,
                                          "state": "submitted" if isinstance(pushed, dict) and pushed.get("submitted") else "submission_failed",
                                          "result_ready": ready,
                                          "answer_complete": answer_complete,
                                          "outcome": outcome}
                submitted = owner._followup_status["state"] == "submitted"
                data["request_completion"] = state.public(body_complete=message_projection["complete"],
                    submitted=submitted, answer_complete=answer_complete)
                owner._followup_status["request_completion"] = data["request_completion"]
                if terminal and not answer_complete:
                    owner._followup_status["reason"] = ("body_not_delivered" if not message_projection["complete"] else
                        state.state if not state.service_ready else "service_projection_incomplete")
                record_answer_delivery(owner, generation=request.generation, channel="followup", payload=data,
                    visual=visual,
                    answer=answer_status(request_plan(request), visual, owner._observer_status),
                    selection_serial=(visual.get("screen_facts") or {}).get("observation_serial"),
                    request_id=request.request_id, submitted=submitted,
                    message_projection=message_projection)
                if ready and "equipment_answer" in data and owner._followup_status["state"] == "submitted":
                    from .equipment import record_handoff
                    record_handoff(owner, data["equipment_answer"], channel="followup")
                owner._get_speech_arbiter().query_started(now=time.monotonic())
                if terminal or not submitted:
                    if getattr(owner, "_pending_answer", None) is request:
                        owner._pending_answer = None
                    return
                delivered.update(confirmed)
                delivered_key = state.progress_key
                delivered_reply = deepcopy((guidance or {}).get("reply_plan"))
                partial_sent = True
                owner._followup_status.update(state="pending", partial_submitted=True,
                                               confirmed_fields=sorted(delivered))
            await _wait_for_update(owner, request)
    except asyncio.CancelledError:
        raise
    except Exception:
        _cancel_current(owner, request, "followup_failed")
