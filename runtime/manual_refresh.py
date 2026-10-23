"""A bounded, silent request to the existing resident observer, not another OCR."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from .observation_store import ObservationSnapshot
from .query_coordinator import satisfied_fields
from .query_plan import build_query_plan

MANUAL_TIMEOUT_SECONDS = 20.0


def content_ready(value):
    """Completion means expected page content, not merely one recognized name."""
    page = value.get("page_type")
    fields = {"relic_detail": ["equipment"], "exploration_hud": ["location", "quest"],
              "character_detail": ["characters", "attributes"], "team_setup": ["characters"]}.get(page)
    if fields:
        plan = build_query_plan("主副词条" if page == "relic_detail" else "", screen_fields=fields)
        return plan.fields <= satisfied_fields(plan, value)
    return bool(value.get("fresh") and ((value.get("screen_facts") or {}).get("facts")
                                      or (value.get("screen_facts") or {}).get("visible_text")))


def public_manual_refresh(owner):
    value = getattr(owner, "_manual_refresh_status", {})
    return {k: v for k, v in value.items() if k in {"state", "requested_at", "finished_at", "revision", "field_count"}}


def can_observe(owner):
    observer = owner._observer_status
    task = getattr(owner, "_observer_task", None)
    return bool(observer.get("enabled") and observer.get("state") not in {"paused", "stopped", "waiting_for_game"}
                and not getattr(owner, "_observer_stop_requested", False) and task is not None and not task.done())


def request_manual_refresh(owner):
    task = getattr(owner, "_manual_refresh_task", None)
    if task is not None and not task.done():
        return public_manual_refresh(owner)
    if not can_observe(owner):
        owner._manual_refresh_status = {"state": "unavailable"}
        return public_manual_refresh(owner)
    owner._manual_refresh_status = {"state": "running", "requested_at": datetime.now(timezone.utc).isoformat()}
    baseline = getattr(owner, "_last_full_commit_serial", getattr(owner, "_observation_serial", 0))
    owner._observer_last_signature = None
    wake = getattr(owner, "_observer_wake_event", None)
    if wake is not None:
        wake.set()
    owner._manual_refresh_task = asyncio.create_task(wait_for_observation(owner, baseline), name="hsr-manual-observation")
    return public_manual_refresh(owner)


def cancel_manual_refresh(owner):
    task = getattr(owner, "_manual_refresh_task", None)
    if task is not None and not task.done():
        owner._manual_refresh_status = {**getattr(owner, "_manual_refresh_status", {}), "state": "cancelled",
                                        "finished_at": datetime.now(timezone.utc).isoformat()}
        task.cancel()


async def wait_for_observation(owner, baseline):
    deadline = time.monotonic() + MANUAL_TIMEOUT_SECONDS
    status = owner._manual_refresh_status
    try:
        while time.monotonic() < deadline and owner._manual_refresh_status is status:
            if not can_observe(owner):
                status.update(state="unavailable", finished_at=datetime.now(timezone.utc).isoformat())
                return
            try:
                current = await asyncio.wait_for(owner._current_visual_evidence(), timeout=min(2.0, max(.001, deadline - time.monotonic())))
            except TimeoutError:
                continue
            snapshot = ObservationSnapshot.select(owner, current)
            # A withdrawal increments observation_serial too. Only the resident
            # producer's new FULL commit counts as a fresh manual read.
            if (getattr(owner, "_last_full_commit_serial", baseline) > baseline
                    and current.get("fresh") and current.get("read_scope") == "full" and snapshot.matches(owner)):
                count = len((current.get("screen_facts") or {}).get("facts") or []) + len(current.get("characters") or [])
                if count or current.get("scene", {}).get("verified"):
                    status.update(state="completed" if content_ready(current) else "partial", revision=snapshot.serial, field_count=count,
                                  finished_at=datetime.now(timezone.utc).isoformat())
                    return
            event = getattr(owner, "_visual_update_event", None)
            if event is not None:
                event.clear()
                try:
                    await asyncio.wait_for(event.wait(), timeout=min(.25, max(.001, deadline - time.monotonic())))
                except TimeoutError:
                    pass
            else:
                await asyncio.sleep(min(.25, max(.001, deadline - time.monotonic())))
        if owner._manual_refresh_status is status:
            status.update(state="timeout", finished_at=datetime.now(timezone.utc).isoformat())
    except asyncio.CancelledError:
        if owner._manual_refresh_status is status and status.get("state") == "running":
            status.update(state="cancelled", finished_at=datetime.now(timezone.utc).isoformat())
        raise
    except Exception:
        if owner._manual_refresh_status is status:
            status.update(state="failed", finished_at=datetime.now(timezone.utc).isoformat())
