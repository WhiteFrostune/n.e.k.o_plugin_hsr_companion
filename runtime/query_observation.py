"""Coordinate existing observation workers with a bounded query wait (L2b)."""
from __future__ import annotations

import asyncio
import copy
import time

from .contracts import QueryObservation, QueryPlan
from .observation_store import ObservationSnapshot


async def resolve_query(owner, plan: QueryPlan, *, refresh, timeout: float, generation: int | None = None) -> QueryObservation:
    """Bound coordination and copy its trace before another request can mutate it."""
    started = time.monotonic()
    generation = getattr(owner, "_answer_query_generation", 0) if generation is None else generation
    deadline = started + timeout
    diagnostics = {}
    selected = None
    if plan.archived_guidance:
        from .observation_evidence import public_visual_evidence
        selected = public_visual_evidence({"invalidated": True, "source": "assistant_local_ocr"}, owner._observer_status)
        return QueryObservation(ObservationSnapshot.select(owner, selected), {"outcome": "not_requested", "stages": []}, 0.0, generation)
    if plan.screen and generation == getattr(owner, "_answer_query_generation", 0):
        # Legacy refresh state has one trace/one query gate. Serialize only
        # coordination, not OCR, and let newer questions supersede old ones.
        lock = getattr(owner, "_query_coordinator_lock", None)
        if lock is None:
            owner._query_coordinator_lock = lock = asyncio.Lock()
        active = getattr(owner, "_active_coordinated_refresh", None)
        if active is not None and active[0] != generation and not active[1].done():
            # Cancel only the obsolete query waiter, not the shared OCR worker.
            active[1].cancel()
        acquired = False
        refresh_task = None
        try:
            await asyncio.wait_for(lock.acquire(), timeout=timeout)
            acquired = True
            if generation == getattr(owner, "_answer_query_generation", 0):
                settings = await asyncio.wait_for(owner._read_observer_settings(), timeout=max(.001, deadline - time.monotonic()))
                task = getattr(owner, "_observer_task", None)
                if settings.get("enabled") and (task is None or task.done()):
                    owner._start_observer_task(restored=True)
                remaining = timeout - (time.monotonic() - started)
                if remaining > 0:
                    owner._last_refresh_diagnostics = {"outcome": "observer_unavailable", "stages": []}
                    refresh_task = asyncio.create_task(refresh(owner, timeout=remaining, query_plan=plan, **plan.refresh_args()))
                    owner._active_coordinated_refresh = (generation, refresh_task)
                    selected = await asyncio.wait_for(refresh_task, timeout=remaining)
                    diagnostics = copy.deepcopy(getattr(owner, "_last_refresh_diagnostics", {}))
                else:
                    diagnostics = {"outcome": "timeout", "stages": []}
            else:
                diagnostics = {"outcome": "cancelled", "stages": []}
        except TimeoutError:
            diagnostics = copy.deepcopy(getattr(owner, "_last_refresh_diagnostics", {})) if refresh_task else {}
            diagnostics.update(outcome="timeout", stages=diagnostics.get("stages", []))
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling() or generation == getattr(owner, "_answer_query_generation", 0):
                raise
            diagnostics = {"outcome": "cancelled", "stages": []}
        finally:
            if refresh_task is not None and getattr(owner, "_active_coordinated_refresh", None) == (generation, refresh_task):
                owner._active_coordinated_refresh = None
            if acquired:
                lock.release()
    elif plan.screen:
        diagnostics = {"outcome": "cancelled", "stages": []}
    if not isinstance(selected, dict):
        try:
            from .review_policy import offline_image_context
            if plan.screen and not offline_image_context(owner):
                # Panel/candidate availability is not a live review receipt.
                # A timeout must not turn the preserved display into an answer.
                from .observation_evidence import public_visual_evidence
                selected = public_visual_evidence({"invalidated": True, "source": "assistant_local_ocr"}, owner._observer_status)
            else:
                # Do not add an unbounded state read after the recognition deadline.
                selected = await asyncio.wait_for(owner._current_visual_evidence(),
                                                  timeout=max(.001, deadline - time.monotonic()))
        except TimeoutError:
            from .observation_evidence import public_visual_evidence
            selected = public_visual_evidence({"invalidated": True, "source": "assistant_local_ocr"}, owner._observer_status)
            if plan.screen and diagnostics.get("outcome") != "cancelled":
                diagnostics = {"outcome": "timeout", "stages": diagnostics.get("stages", [])}
    snapshot = ObservationSnapshot.select(owner, selected, fields=plan.fields)
    if plan.screen:
        selected = snapshot.delivery(owner, generation=generation)
        snapshot = ObservationSnapshot.select(owner, selected, fields=plan.fields)
    return QueryObservation(snapshot, diagnostics, round(time.monotonic() - started, 3), generation)
