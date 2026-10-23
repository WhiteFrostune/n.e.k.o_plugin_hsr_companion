"""One bounded UID import action; panel polling and chat never fetch online."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import timedelta

from plugin.sdk.plugin import Err, unwrap_or

from ..adapters.showcase import ShowcaseError, fetch_showcase, validate_uid
from .uid_import import STORE_KEY, confirm_preview, normalize_showcase, remaining, roster_view, utcnow


class UidImportService:
    def __init__(self, owner, *, fetch=None, clock=None):
        self.owner = owner
        self.fetch = fetch
        self.clock = clock or utcnow
        self.lock = asyncio.Lock()
        self.in_flight = False

    async def read(self):
        value = await self.owner._get_store(STORE_KEY)
        if isinstance(value, Err):
            raise RuntimeError("UID_STORE_READ_FAILED")
        state = unwrap_or(value, {})
        if state is None:  # Public SDK returns Ok(None) for a missing KV key.
            state = {}
        if not isinstance(state, dict):
            raise RuntimeError("UID_STORE_INVALID")
        return deepcopy(state)

    def view(self, state):
        now = self.clock()
        preview = deepcopy(state.get("preview"))
        if preview:
            preview["can_confirm"] = bool(remaining(preview.get("expires_at"), now))
            previous = state.get("characters", {})
            incoming = preview["characters"]
            summary = {
                "added": 0,
                "updated": 0,
                "unchanged": 0,
                "retained": len(set(previous) - set(incoming)),
                "total_after": len(set(previous) | set(incoming)),
            }
            rows = []
            for key, row in incoming.items():
                old = previous.get(key)
                change = (
                    "added"
                    if old is None
                    else (
                        "unchanged"
                        if row.get("content_digest") and row["content_digest"] == old.get("content_digest")
                        else "updated"
                    )
                )
                summary[change] += 1
                rows.append({**row, "change": change})
            preview["characters"], preview["summary"] = rows, summary
            # Keep the preview's own UID visible. If an unbound user tries a
            # different UID and that request fails, the retained preview still
            # belongs to the previous account, not to query_uid.
        return {
            "uid": state.get("uid") or state.get("query_uid") or "",
            "source": "Enka.Network",
            "source_url": "https://enka.network",
            "status": "reading" if self.in_flight else state.get("status", "empty"),
            "error_code": state.get("error_code"),
            "error_info": deepcopy(state.get("error_info")),
            "use_system_proxy": state.get("use_system_proxy") is True,
            "retry_after_seconds": remaining(state.get("next_query_at"), now),
            "next_query_at": state.get("next_query_at"),
            "last_query_at": state.get("last_query_at"),
            "preview": preview,
            "roster": roster_view(state, now=now),
            "network_on_poll": False,
        }

    async def stage(self, uid, consent=False, *, use_system_proxy=False):
        # Consent is checked before even looking up another account's data.
        try:
            uid = validate_uid(uid)
            if consent is not True:
                raise ShowcaseError("consent_required")
            if not isinstance(use_system_proxy, bool):
                raise ShowcaseError("invalid_network_mode")
        except ShowcaseError as exc:
            state = await self.read()
            return {**self.view(state), "status": "error", "error_code": exc.code}
        if self.lock.locked():
            return {**self.view(await self.read()), "status": "reading"}
        async with self.lock:
            state = await self.read()
            if state.get("uid") and state["uid"] != uid:
                return {**self.view(state), "status": "error", "error_code": "different_account"}
            # Global throttle also bounds pre-confirm UID changes and failures.
            if remaining(state.get("next_query_at"), self.clock()):
                return {**self.view(state), "status": "cooldown", "error_code": None}
            generation = getattr(self.owner, "_uid_import_generation", 0)
            attempted_at = self.clock()
            # Persist the cooldown BEFORE I/O. A cancelled panel call or reload
            # must not immediately open another request while its thread drains.
            state = {
                **state,
                "query_uid": uid,
                "status": "reading",
                "last_query_at": attempted_at.isoformat(),
                "use_system_proxy": use_system_proxy,
                "next_query_at": (attempted_at + timedelta(seconds=60)).isoformat(),
            }
            await self.owner._store_value(STORE_KEY, state)
            self.in_flight = True
            try:
                if self.fetch is None:
                    response = await asyncio.to_thread(fetch_showcase, uid, use_system_proxy=use_system_proxy)
                else:
                    response = await asyncio.to_thread(self.fetch, uid)
                now = self.clock()
                preview = await asyncio.to_thread(
                    normalize_showcase,
                    response.payload,
                    uid=uid,
                    registry={"records": self.owner._characters.records, "meta": self.owner._characters.meta},
                    catalog=self.owner._catalog,
                    now=now,
                    ttl=response.ttl,
                )
                result = {
                    **state,
                    "schema_version": 1,
                    "query_uid": uid,
                    "status": "preview",
                    "preview": preview,
                    "error_code": None,
                    "error_info": None,
                    "last_query_at": now.isoformat(),
                    "next_query_at": preview["next_query_at"],
                }
            except ShowcaseError as exc:
                now = self.clock()
                # Previous committed builds and preview remain intact on failure.
                result = {
                    **state,
                    "status": "error",
                    "error_code": exc.code,
                    "error_info": {
                        "http_status": exc.http_status,
                        "network_mode": "system_proxy" if use_system_proxy else "inherit",
                    },
                    "last_query_at": now.isoformat(),
                    "next_query_at": (now + timedelta(seconds=max(60, exc.retry_seconds))).isoformat(),
                }
            except asyncio.CancelledError:
                if generation == getattr(self.owner, "_uid_import_generation", 0):
                    await self.owner._store_value(STORE_KEY, {**state, "status": "cancelled"})
                raise
            finally:
                self.in_flight = False
            if generation != getattr(self.owner, "_uid_import_generation", 0):
                return {**self.view(await self.read()), "status": "cancelled"}
            await self.owner._store_value(STORE_KEY, result)
            return self.view(result)

    async def confirm(self, preview_id):
        if self.lock.locked():
            return {**self.view(await self.read()), "status": "reading"}
        async with self.lock:
            state = await self.read()
            try:
                result = confirm_preview(state, preview_id, now=self.clock())
            except ShowcaseError as exc:
                return {**self.view(state), "status": "error", "error_code": exc.code}
            result.update(status="saved", error_code=None, error_info=None)
            await self.owner._store_value(STORE_KEY, result)
            return self.view(result)

    async def discard(self, preview_id):
        if self.lock.locked():
            return {**self.view(await self.read()), "status": "reading"}
        async with self.lock:
            state = await self.read()
            if (state.get("preview") or {}).get("preview_id") == preview_id:
                state.update(preview=None, status="saved" if state.get("characters") else "empty")
                await self.owner._store_value(STORE_KEY, state)
            return self.view(state)

    async def clear(self, confirmed=False):
        if confirmed is not True:
            return {**self.view(await self.read()), "status": "error", "error_code": "clear_confirmation_required"}
        if self.lock.locked():
            return {**self.view(await self.read()), "status": "reading"}
        async with self.lock:
            previous = await self.read()
            # Forget builds, but preserve throttling even if a user rebinds UID.
            state = {"status": "empty", "next_query_at": previous.get("next_query_at")}
            await self.owner._store_value(STORE_KEY, state)
            return self.view(state)


def uid_service(owner):
    service = getattr(owner, "_uid_import_service", None)
    if service is None:
        owner._uid_import_service = service = UidImportService(owner)
    return service
