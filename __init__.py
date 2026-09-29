"""N.E.K.O 《崩坏：星穹铁道》游戏搭子插件。"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from plugin.sdk.plugin import (
    Err,
    NekoPluginBase,
    Ok,
    SdkError,
    lifecycle,
    llm_tool,
    neko_plugin,
    plugin_entry,
    ui,
    unwrap_or,
)

from .adapters import LocalVisionAdapter
from .core import (
    CharacterRegistry,
    KnowledgeBase,
    analyze_team,
    apply_profile_proposal,
    build_profile_proposal,
    evaluate_event,
    merge_player_context,
)
from .data import ExternalKnowledgeCatalog
from .game_state import (
    GAME_STATE_LABELS,
    build_game_state_observation,
    public_game_state,
)
from .observer import capture_game_window, find_hsr_window, frame_difference
from .runtime import CompanionRuntime, build_event_delivery
from .session import (
    DEFAULT_SESSION_TTL_SECONDS,
    build_companion_context,
    normalize_observed_name,
    public_correction_summary,
    remember_character_correction,
    resolve_character_candidates,
    sanitize_character_corrections,
    session_is_active,
    start_session,
    stop_session,
    utc_now_iso,
    visual_fingerprint,
    visual_fingerprints_match,
)
from .vision import prepare_image_for_vision

PROFILE_PROPERTIES = {
    "owned_characters": {"type": "array", "items": {"type": "string"}},
    "favorite_characters": {"type": "array", "items": {"type": "string"}},
    "training_targets": {"type": "array", "items": {"type": "string"}},
    "common_teams": {"type": "array", "items": {"type": "string"}},
    "current_goals": {"type": "array", "items": {"type": "string"}},
    "preferences": {"type": "object", "additionalProperties": True},
    "progression_note": {"type": "string"},
}


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def _request_is_recent(request: object, *, max_age_seconds: float = 120.0) -> bool:
    if not isinstance(request, dict) or not request.get("fingerprint"):
        return False
    try:
        created = datetime.fromisoformat(str(request.get("created_at") or "").replace("Z", "+00:00"))
    except ValueError:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - created.astimezone(timezone.utc)).total_seconds()
    return 0 <= age <= max(1.0, float(max_age_seconds))


@neko_plugin
class HsrCompanionPlugin(NekoPluginBase):
    """Star Rail knowledge and player-context capability for N.E.K.O."""

    def __init__(self, ctx: Any):
        super().__init__(ctx)
        # These packaged resources are deliberately fixed and auditable. Runtime
        # configuration stays available for a later source-adapter phase, where
        # changes can be validated before replacing the audited seed catalog.
        catalog_path = self.config_dir / "resources" / "knowledge.demo.json"
        sources_path = self.config_dir / "resources" / "sources.json"
        registry_path = self.config_dir / "resources" / "character_registry.json"
        self._knowledge = KnowledgeBase(
            _load_json(catalog_path),
            _load_json(sources_path),
        )
        registry_payload = _load_json(registry_path)
        self._characters = CharacterRegistry(registry_payload)
        self._catalog = ExternalKnowledgeCatalog(registry=registry_payload)
        self._vision = LocalVisionAdapter(self._catalog, logger=self.logger)
        self._runtime = CompanionRuntime(confirmation_frames=2)
        self._max_experiences = 100
        self._event_dedup_limit = 200
        self._session_ttl_seconds = DEFAULT_SESSION_TTL_SECONDS
        self._context_audit_limit = 50
        self._tool_audit_limit = 100
        self._observer_task: asyncio.Task[None] | None = None
        self._data_pack_task: asyncio.Task[None] | None = None
        self._data_pack_runtime: dict[str, Any] = self._catalog.status()
        self._observer_last_signature: bytes | None = None
        self._observer_last_push_monotonic = 0.0
        self._observer_status: dict[str, Any] = {
            "enabled": False,
            "proactive_enabled": True,
            "state": "stopped",
            "message": "自动观察尚未开启",
        }

    @lifecycle(id="startup")
    async def startup(self, **_):
        # Some released N.E.K.O hosts construct the plugin before the effective
        # config (including ``plugin.store.enabled``) has reached the SDK.  The
        # built-in memo/lifekit plugins keep the same compatibility guard.  This
        # only enables this plugin's own sandboxed KV store; it does not touch
        # host or game files.
        if not self.store.enabled:
            self.store.enabled = True
            self.logger.info("Store force-enabled for hsr_companion")
        corrections, disabled_corrections = sanitize_character_corrections(
            await self._read_corrections(), registry=self._characters
        )
        if disabled_corrections:
            await self._store_value("character_corrections", corrections)
            self.logger.info(
                "Disabled %s unsafe legacy character correction(s)",
                disabled_corrections,
            )
        legacy_state = await self._read_game_state()
        if legacy_state and str(legacy_state.get("verification") or "unverified") not in {
            "verified",
            "player_confirmed",
        }:
            # v0.7 allowed a model-authored state to occupy the authoritative
            # slot. Preserve it only as an audit hint and clear the slot so an
            # upgrade cannot display old model guesses as current truth.
            await self._store_value("last_unverified_game_state_hint", legacy_state)
            await self._store_value("current_game_state", {})
        session = await self._read_session()
        if session and not session_is_active(session, ttl_seconds=self._session_ttl_seconds):
            session = stop_session(session, reason="expired_before_startup")
            await self._store_value("companion_session", session)
        elif session_is_active(session, ttl_seconds=self._session_ttl_seconds):
            await self._push_companion_context("plugin_startup_restore")
        observer_settings = unwrap_or(await self.store.get("observer_settings"), {})
        if not isinstance(observer_settings, dict):
            observer_settings = {}
        self._observer_status["proactive_enabled"] = bool(observer_settings.get("proactive_enabled", True))
        if observer_settings.get("enabled") is True:
            # startup is invoked through a transient asyncio.run() loop.  A
            # task created here would be cancelled as soon as startup returns;
            # restore it on the first panel/tool call instead.
            self._observer_status = {
                "enabled": True,
                "proactive_enabled": bool(observer_settings.get("proactive_enabled", True)),
                "state": "waiting",
                "message": "打开插件面板或开始聊天后恢复自动观察",
                "updated_at": utc_now_iso(),
                "restored": True,
            }
        if not self._catalog.status().get("installed"):
            # Data-pack preparation is finite work, so await it here rather
            # than spawning it on the transient lifecycle loop.
            self._data_pack_runtime = {
                **self._catalog.status(),
                "state": "installing",
                "message": "正在准备星铁资料组件",
            }
            await self._install_data_pack()
        return Ok(
            {
                "status": "ready",
                "catalog": self._catalog.meta,
                "record_count": self._catalog.record_count,
                "coverage": self._catalog.coverage,
                "session_active": session_is_active(session, ttl_seconds=self._session_ttl_seconds),
                "observer_enabled": bool(self._observer_status.get("enabled")),
            }
        )

    @lifecycle(id="shutdown")
    async def shutdown(self, **_):
        await self._cancel_observer_task(update_status=False)
        task = self._data_pack_task
        self._data_pack_task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await asyncio.to_thread(self._vision.close)
        session = await self._read_session()
        if session_is_active(session, ttl_seconds=self._session_ttl_seconds):
            self.push_message(
                visibility=[],
                ai_behavior="read",
                parts=[
                    {
                        "type": "text",
                        "text": (
                            "[星铁陪玩插件暂时停止]\n"
                            "停止使用本插件此前注入的星铁会话规则；"
                            "插件重新启动并恢复会话后再继续使用。"
                        ),
                    }
                ],
                source="hsr_companion",
                priority=1,
                coalesce_key="hsr_session_context",
            )
        return Ok({"status": "stopped"})

    def _set_observer_status(self, state: str, message: str, **details: Any) -> None:
        self._observer_status = {
            "enabled": bool(self._observer_status.get("enabled")),
            "proactive_enabled": bool(self._observer_status.get("proactive_enabled", True)),
            "state": str(state or "unknown"),
            "message": str(message or ""),
            "updated_at": utc_now_iso(),
            **details,
        }

    def _start_data_pack_task(self, *, force: bool = False) -> None:
        if self._data_pack_task and not self._data_pack_task.done():
            return
        if self._catalog.status().get("installed") and not force:
            self._data_pack_runtime = self._catalog.status()
            return
        self._data_pack_runtime = {
            **self._catalog.status(),
            "state": "installing",
            "message": "正在准备星铁资料组件",
        }
        self._data_pack_task = asyncio.create_task(self._install_data_pack(), name="hsr-companion-data-pack")

    async def _install_data_pack(self) -> None:
        try:
            status = await asyncio.to_thread(self._catalog.install_pinned)
            self._data_pack_runtime = status
            if session_is_active(await self._read_session(), ttl_seconds=self._session_ttl_seconds):
                await self._push_companion_context("knowledge_pack_ready")
        except asyncio.CancelledError:
            self._data_pack_runtime = {
                **self._catalog.status(),
                "state": "missing",
                "message": "资料组件尚未准备，可在面板一键重试",
            }
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced in panel, minimal registry remains safe
            self.logger.warning("HSR data pack preparation failed: %s", exc)
            self._data_pack_runtime = {
                **self._catalog.status(),
                "state": "error",
                "message": "资料组件准备失败，可在面板重试；角色防幻觉名单仍然有效",
                "error": f"{type(exc).__name__}: {exc}",
            }

    def _public_data_pack_status(self) -> dict[str, Any]:
        status = dict(self._data_pack_runtime or self._catalog.status())
        status.pop("database", None)
        status.pop("root", None)
        return status

    async def _wait_for_data_pack(self, *, force: bool = False) -> dict[str, Any]:
        self._start_data_pack_task(force=force)
        task = self._data_pack_task
        if task is not None:
            await task
        return self._public_data_pack_status()

    async def _apply_local_analysis(
        self,
        analysis: dict[str, Any],
        *,
        image_fingerprint: str,
        source: str,
        deliver_event: bool = True,
    ) -> dict[str, Any]:
        """Commit only stabilized local observations and deliver one event at most."""

        corrections = await self._read_corrections()
        corrected_characters: list[dict[str, Any]] = []
        for candidate in list(analysis.get("characters") or []):
            if not isinstance(candidate, dict):
                continue
            observed_key = normalize_observed_name(candidate.get("name"))
            candidate_region_fingerprint = str(candidate.get("visual_fingerprint") or "")
            matched = [
                item
                for item in corrections
                if isinstance(item, dict)
                and item.get("modality") == "vision"
                and item.get("scope") == "visual_fingerprint"
                and normalize_observed_name(item.get("observed_name")) == observed_key
                and candidate_region_fingerprint
                and visual_fingerprints_match(
                    item.get("scope_value"),
                    candidate_region_fingerprint,
                    max_distance=4,
                )
            ]
            if not matched:
                corrected_characters.append(dict(candidate))
                continue
            matched.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
            correction = matched[0]
            record = self._catalog.resolve_character(str(correction.get("canonical_name") or ""))
            if record:
                corrected_characters.append(
                    {
                        **dict(candidate),
                        "entity_id": record["id"],
                        "name": record["name"],
                        "evidence": "player_visual_region_correction",
                        "confidence": "high",
                        "correction_id": correction.get("correction_id"),
                    }
                )
        analysis = {**analysis, "characters": corrected_characters}
        await self._store_value(
            "active_game_visual_request",
            {
                "fingerprint": image_fingerprint,
                "created_at": utc_now_iso(),
                "source": source,
                "image_stored": False,
                "character_regions": [
                    {
                        "name": str(item.get("name") or ""),
                        "fingerprint": str(item.get("visual_fingerprint") or ""),
                    }
                    for item in corrected_characters
                    if item.get("visual_fingerprint")
                ],
            },
        )

        event, decisions = self._runtime.ingest(analysis)
        scene = dict(analysis.get("scene") or {})
        stable_scene = self._runtime.stable_scene
        accepted = bool(stable_scene != "unknown" and stable_scene == scene.get("primary_state"))
        if accepted:
            observation = build_game_state_observation(
                primary_state=stable_scene,
                substate=str(scene.get("substate") or ""),
                confidence=str(scene.get("confidence") or "mixed"),
                evidence=list(scene.get("evidence") or []),
                summary=(
                    f"本地页面识别确认："
                    f"{(analysis.get('page') or {}).get('label') or GAME_STATE_LABELS.get(stable_scene, stable_scene)}"
                ),
                source=source,
                verification="verified",
                visual_fingerprint=image_fingerprint,
            )
            await self._store_value("current_game_state", observation)
            await self._store_value("pending_game_state_candidate", {})
            history = await self._read_game_state_history()
            history.append(
                {
                    **observation,
                    "accepted": True,
                    "transition_reason": "local_runtime_stabilized",
                }
            )
            await self._store_value("game_state_history", history[-50:])

        public_analysis = {
            "ok": bool(analysis.get("ok")),
            "scene": scene,
            "page": dict(analysis.get("page") or {}),
            "characters": list(analysis.get("characters") or []),
            "ocr": dict(analysis.get("ocr") or {}),
            "catalog_revision": self._catalog.revision,
            "image_stored": False,
            "visual_fingerprint": image_fingerprint,
            "source": source,
            "observed_at": utc_now_iso(),
            "runtime": self._runtime.snapshot(),
            "decisions": decisions,
        }
        await self._store_value("last_local_vision", public_analysis)

        delivery: dict[str, Any] = {"submitted": False, "reason": "no_stable_event"}
        if event is not None and deliver_event:
            observer_settings = await self._read_observer_settings()
            plan = build_event_delivery(
                event,
                catalog_revision=self._catalog.revision,
                proactive_enabled=bool(observer_settings.get("proactive_enabled", True)),
            )
            pushed = self.push_message(
                visibility=[],
                ai_behavior=plan.ai_behavior,
                parts=[{"type": "text", "text": plan.text}],
                source="hsr_companion",
                metadata=plan.metadata,
                priority=plan.priority,
                coalesce_key=plan.coalesce_key,
            )
            delivery = dict(pushed) if isinstance(pushed, dict) else {}
            delivery["ai_behavior"] = plan.ai_behavior
            delivery["event_kind"] = event.kind
            delivery["proactive_requested"] = bool(plan.metadata.get("proactive_requested"))
        return {**public_analysis, "delivery": delivery}

    def _start_observer_task(self, *, restored: bool = False) -> None:
        self._observer_status["enabled"] = True
        if self._observer_task and not self._observer_task.done():
            return
        self._set_observer_status(
            "waiting",
            "正在寻找《崩坏：星穹铁道》窗口",
            restored=restored,
        )
        self._observer_task = asyncio.create_task(self._observer_loop(), name="hsr-companion-observer")

    async def _cancel_observer_task(self, *, update_status: bool = True) -> None:
        task = self._observer_task
        self._observer_task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._observer_last_signature = None
        self._observer_last_push_monotonic = 0.0
        if update_status:
            self._observer_status = {
                "enabled": False,
                "proactive_enabled": bool(self._observer_status.get("proactive_enabled", True)),
                "state": "stopped",
                "message": "自动观察已关闭",
                "updated_at": utc_now_iso(),
            }

    async def _observer_loop(self) -> None:
        while True:
            try:
                window = await asyncio.to_thread(find_hsr_window)
                if window is None:
                    self._set_observer_status("waiting", "没有找到星铁窗口，请先启动游戏")
                    await asyncio.sleep(3)
                    continue
                public_window = window.public()
                if window.minimized:
                    self._set_observer_status("paused", "星铁已最小化，自动观察已暂停", window=public_window)
                    await asyncio.sleep(3)
                    continue
                if not window.foreground:
                    self._set_observer_status("paused", "切回星铁后会自动继续观察", window=public_window)
                    await asyncio.sleep(3)
                    continue

                image_bytes, mime, signature = await asyncio.to_thread(capture_game_window, window)
                now = time.monotonic()
                elapsed = now - self._observer_last_push_monotonic
                difference = frame_difference(self._observer_last_signature, signature)
                should_push = (
                    self._observer_last_signature is None
                    or (elapsed >= 3 and bool(self._runtime.pending_scene or self._runtime.pending_character_ids))
                    or (elapsed >= 8 and difference >= 0.055)
                    or elapsed >= 45
                )
                if should_push:
                    image_fingerprint = visual_fingerprint(image_bytes)
                    await self._store_value(
                        "active_game_visual_request",
                        {
                            "fingerprint": image_fingerprint,
                            "created_at": utc_now_iso(),
                            "mime": mime,
                            "source": "auto_window",
                            "image_stored": False,
                        },
                    )
                    analysis = await asyncio.to_thread(self._vision.analyze, image_bytes)
                    # The OCR detector supplies trusted structure; the image
                    # supplies the natural visual context that mature game
                    # companion plugins expose to N.E.K.O. It is read-only and
                    # coalesced so old frames cannot build up and be replayed.
                    self._push_visual_frame(
                        image_bytes,
                        mime,
                        analysis,
                        source="auto_window",
                    )
                    await self._store_value(
                        "active_game_visual_request",
                        {
                            "fingerprint": image_fingerprint,
                            "created_at": utc_now_iso(),
                            "mime": mime,
                            "source": "auto_window",
                            "image_stored": False,
                            "character_regions": [
                                {
                                    "name": str(item.get("name") or ""),
                                    "fingerprint": str(item.get("visual_fingerprint") or ""),
                                }
                                for item in analysis.get("characters") or []
                                if isinstance(item, dict) and item.get("visual_fingerprint")
                            ],
                        },
                    )
                    local_result = await self._apply_local_analysis(
                        analysis,
                        image_fingerprint=image_fingerprint,
                        source="auto_window_local_ocr",
                    )
                    self._observer_last_signature = signature
                    self._observer_last_push_monotonic = now
                    scene_key = str((analysis.get("scene") or {}).get("primary_state") or "unknown")
                    scene_label = GAME_STATE_LABELS.get(scene_key, GAME_STATE_LABELS["unknown"])
                    names = [
                        str(item.get("name") or "")
                        for item in analysis.get("characters") or []
                        if isinstance(item, dict)
                    ]
                    self._set_observer_status(
                        "observing",
                        f"本地识别：{scene_label}",
                        window=public_window,
                        last_frame_at=utc_now_iso(),
                        scene=scene_key,
                        recognized_characters=names,
                        ocr=dict(analysis.get("ocr") or {}),
                        runtime=self._runtime.snapshot(),
                        last_delivery={
                            "submitted": bool((local_result.get("delivery") or {}).get("submitted")),
                            "reason": str((local_result.get("delivery") or {}).get("reason") or ""),
                            "ai_behavior": str((local_result.get("delivery") or {}).get("ai_behavior") or ""),
                            "event_kind": str((local_result.get("delivery") or {}).get("event_kind") or ""),
                            "proactive_requested": bool(
                                (local_result.get("delivery") or {}).get("proactive_requested")
                            ),
                        },
                    )
                else:
                    self._set_observer_status(
                        "observing",
                        "画面没有明显变化，继续观察中",
                        window=public_window,
                        last_frame_at=self._observer_status.get("last_frame_at", ""),
                        last_delivery=self._observer_status.get("last_delivery", {}),
                    )
                await asyncio.sleep(3)
            except asyncio.CancelledError:
                raise
            except RuntimeError as exc:
                self._set_observer_status("paused", str(exc))
                await asyncio.sleep(3)
            except Exception as exc:  # keep the plugin alive if capture fails
                self.logger.warning("HSR observer paused: %s", exc)
                self._set_observer_status("error", "暂时无法读取星铁窗口，插件会自动重试")
                await asyncio.sleep(5)

    async def _store_value(self, key: str, value: Any) -> None:
        stored = await self.store.set(key, value)
        if isinstance(stored, Err):
            raise RuntimeError(f"STORE_FAILED:{key}")

    async def _read_observer_settings(self) -> dict[str, Any]:
        value = unwrap_or(await self.store.get("observer_settings"), {})
        settings = dict(value) if isinstance(value, dict) else {}
        settings["enabled"] = bool(settings.get("enabled", False))
        settings["proactive_enabled"] = bool(settings.get("proactive_enabled", True))
        return settings

    async def _read_session(self) -> dict[str, Any]:
        value = unwrap_or(await self.store.get("companion_session"), {})
        return value if isinstance(value, dict) else {}

    async def _read_corrections(self) -> list[dict[str, Any]]:
        value = unwrap_or(await self.store.get("character_corrections"), [])
        if not isinstance(value, list):
            return []
        corrections = [dict(item) for item in value if isinstance(item, dict)]
        sanitized, disabled = sanitize_character_corrections(corrections, registry=self._characters)
        if disabled:
            # Run the migration at the read boundary as well as startup. This
            # makes a hot-reloaded plugin safe before its next full restart.
            await self._store_value("character_corrections", sanitized)
        return sanitized

    def _push_visual_frame(
        self,
        image_bytes: bytes,
        mime: str,
        analysis: dict[str, Any],
        *,
        source: str,
    ) -> dict[str, Any]:
        """Stream the newest game frame to N.E.K.O without treating it as fact."""

        characters = [
            str(item.get("name") or "").strip()
            for item in analysis.get("characters") or []
            if isinstance(item, dict) and item.get("entity_id")
        ]
        scene = dict(analysis.get("scene") or {})
        scene_key = str(scene.get("primary_state") or "unknown")
        cue = (
            "[星铁自动观察画面｜最新帧覆盖旧帧]\n"
            f"插件本地页面状态：{GAME_STATE_LABELS.get(scene_key, GAME_STATE_LABELS['unknown'])}；"
            f"置信度：{scene.get('confidence') or 'low'}。\n"
            f"插件数据库从清晰文字确认的角色：{'、'.join(characters) if characters else '无'}。\n"
            "图片本身是未经角色身份验证的视觉上下文。可以理解画面和陪伴玩家，"
            "但如果上面没有数据库确认角色，就不得仅凭外观猜角色名，也不得复述旧纠错。"
            "玩家询问角色身份或星铁事实时，回答前必须调用 hsr_companion_prepare。"
        )
        result = self.push_message(
            visibility=[],
            ai_behavior="read",
            parts=[
                {"type": "text", "text": cue},
                {"type": "image", "data": image_bytes, "mime": mime},
            ],
            source="hsr_companion",
            metadata={
                "kind": "latest_game_frame",
                "frame_source": source,
                "catalog_revision": self._catalog.revision,
                "image_stored": False,
            },
            priority=3,
            # Match the mature game-agent plugins: queued old frames are
            # obsolete as soon as a newer one exists, so latest frame wins.
            coalesce_key="hsr_latest_game_frame",
        )
        return dict(result) if isinstance(result, dict) else {}

    async def _read_context_audit(self) -> list[dict[str, Any]]:
        value = unwrap_or(await self.store.get("context_audit"), [])
        if not isinstance(value, list):
            return []
        return [dict(item) for item in value if isinstance(item, dict)]

    async def _read_tool_audit(self) -> list[dict[str, Any]]:
        value = unwrap_or(await self.store.get("tool_audit"), [])
        if not isinstance(value, list):
            return []
        return [dict(item) for item in value if isinstance(item, dict)]

    async def _record_tool_use(self, tool_name: str, **details: Any) -> None:
        records = await self._read_tool_audit()
        records.append(
            {
                "at": utc_now_iso(),
                "tool": str(tool_name or ""),
                **{key: value for key, value in details.items() if value not in (None, "")},
            }
        )
        await self._store_value("tool_audit", records[-self._tool_audit_limit :])

    async def _append_context_audit(self, record: dict[str, Any]) -> None:
        records = await self._read_context_audit()
        records.append(dict(record))
        await self._store_value("context_audit", records[-self._context_audit_limit :])

    async def _read_visual_request(self) -> dict[str, Any]:
        value = unwrap_or(await self.store.get("active_visual_request"), {})
        return value if isinstance(value, dict) else {}

    async def _read_game_visual_request(self) -> dict[str, Any]:
        value = unwrap_or(await self.store.get("active_game_visual_request"), {})
        return value if isinstance(value, dict) else {}

    async def _read_game_state(self) -> dict[str, Any]:
        value = unwrap_or(await self.store.get("current_game_state"), {})
        return value if isinstance(value, dict) else {}

    async def _read_game_state_history(self) -> list[dict[str, Any]]:
        value = unwrap_or(await self.store.get("game_state_history"), [])
        if not isinstance(value, list):
            return []
        return [dict(item) for item in value if isinstance(item, dict)]

    async def _read_pending_game_state(self) -> dict[str, Any]:
        value = unwrap_or(await self.store.get("pending_game_state_candidate"), {})
        return value if isinstance(value, dict) else {}

    async def _touch_session(self, reason: str) -> tuple[dict[str, Any], bool]:
        current = await self._read_session()
        was_active = session_is_active(current, ttl_seconds=self._session_ttl_seconds)
        session = start_session(current, reason=reason)
        await self._store_value("companion_session", session)
        return session, not was_active

    async def _push_companion_context(self, reason: str) -> dict[str, Any]:
        session, _ = await self._touch_session(reason)
        profile = await self._read_profile()
        pending = await self._read_pending_profile()
        corrections = await self._read_corrections()
        game_state = public_game_state(await self._read_game_state())
        context_text = build_companion_context(
            session=session,
            profile=profile,
            pending=pending,
            corrections=corrections,
            registry_meta=self._characters.meta,
            registry_count=len(self._characters.records),
            catalog_meta=self._catalog.meta,
            game_state=game_state,
        )
        push_result = self.push_message(
            visibility=[],
            ai_behavior="read",
            parts=[{"type": "text", "text": context_text}],
            source="hsr_companion",
            metadata={"kind": "hsr_session_context", "reason": reason},
            priority=4,
            coalesce_key="hsr_session_context",
        )
        delivery = dict(push_result) if isinstance(push_result, dict) else {}
        session["context_push_count"] = int(session.get("context_push_count") or 0) + 1
        session["last_context_push_at"] = utc_now_iso()
        session["last_context_reason"] = str(reason or "")
        session["last_context_delivery"] = {
            "submitted": bool(delivery.get("submitted")),
            "reason": str(delivery.get("reason") or ""),
        }
        await self._store_value("companion_session", session)
        await self._append_context_audit(
            {
                "at": session["last_context_push_at"],
                "reason": reason,
                "submitted": bool(delivery.get("submitted")),
                "delivery_reason": str(delivery.get("reason") or ""),
                "profile_character_count": len(profile.get("owned_characters") or []),
                "correction_count": len(corrections),
                "pending": bool(pending),
                "game_state": game_state.get("primary_state") or "unknown",
            }
        )
        return {"session": session, "delivery": delivery, "context": context_text}

    async def _start_companion_session(self, reason: str) -> dict[str, Any]:
        return await self._push_companion_context(reason or "user_start")

    async def _stop_companion_session(self, reason: str) -> dict[str, Any]:
        current = await self._read_session()
        session = stop_session(current, reason=reason or "user_stop")
        await self._store_value("companion_session", session)
        push_result = self.push_message(
            visibility=[],
            ai_behavior="read",
            parts=[
                {
                    "type": "text",
                    "text": (
                        "[星铁陪玩会话结束]\n"
                        "玩家已经结束本次《崩坏：星穹铁道》陪玩。"
                        "请恢复普通聊天，不要把后续内容自动理解为星铁事件；"
                        "已确认的插件档案仍保留。"
                    ),
                }
            ],
            source="hsr_companion",
            metadata={"kind": "hsr_session_restore", "reason": reason},
            priority=4,
            coalesce_key="hsr_session_context",
        )
        delivery = dict(push_result) if isinstance(push_result, dict) else {}
        await self._append_context_audit(
            {
                "at": utc_now_iso(),
                "reason": reason or "user_stop",
                "submitted": bool(delivery.get("submitted")),
                "delivery_reason": str(delivery.get("reason") or ""),
                "event": "session_stopped",
            }
        )
        return {"session": session, "delivery": delivery}

    async def _read_profile(self) -> dict[str, Any]:
        value = unwrap_or(await self.store.get("player_context"), {})
        return value if isinstance(value, dict) else {}

    async def _write_profile(
        self,
        changes: dict[str, Any],
        *,
        mode: str = "merge",
        clear_fields: list[str] | None = None,
    ) -> dict[str, Any]:
        validated_changes = self._validate_profile_changes(changes)
        profile = merge_player_context(
            await self._read_profile(),
            validated_changes,
            mode=mode,
            clear_fields=clear_fields or [],
        )
        stored = await self.store.set("player_context", profile)
        if isinstance(stored, Err):
            raise RuntimeError("PLAYER_CONTEXT_STORE_FAILED")
        return profile

    def _validate_profile_changes(self, changes: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(changes, dict):
            raise ValueError("玩家档案更新必须是对象")
        validated = dict(changes)
        for field in ("owned_characters", "favorite_characters", "training_targets"):
            if field not in validated or validated[field] is None:
                continue
            raw = validated[field]
            candidates = raw if isinstance(raw, list) else [raw]
            accepted, rejected = self._characters.validate_candidates(candidates)
            if rejected:
                raise ValueError(
                    "角色数据库未收录：" + "、".join(rejected) + "。为避免把 AI 编造内容写进档案，本次更新已全部取消。"
                )
            validated[field] = accepted
        return validated

    async def _read_pending_profile(self) -> dict[str, Any]:
        value = unwrap_or(await self.store.get("pending_player_context"), {})
        if not isinstance(value, dict):
            return {}
        pending = dict(value)
        applied = pending.get("player_correction_applied")
        if not isinstance(applied, dict) or not applied.get("correction_id"):
            return pending
        correction_id = str(applied.get("correction_id") or "")
        correction = next(
            (item for item in await self._read_corrections() if str(item.get("correction_id") or "") == correction_id),
            None,
        )
        if correction is None or correction.get("active") is not False:
            return pending

        # A v0.8.1 pending proposal may already contain the result of the
        # unsafe rewrite. Downgrade it in place so opening the panel cannot
        # keep resurfacing that name as a confirmable recognition.
        recognized = [str(item).strip() for item in pending.get("recognized_characters") or [] if str(item).strip()]
        uncertain = list(
            dict.fromkeys(
                [
                    *recognized,
                    *[str(item).strip() for item in pending.get("uncertain_characters") or [] if str(item).strip()],
                ]
            )
        )
        pending.update(
            {
                "recognized_characters": [],
                "uncertain_characters": uncertain,
                "can_confirm": False,
                "confidence": "low",
                "visual_verification": "invalidated_legacy_correction",
                "player_correction_applied": {
                    **applied,
                    "active": False,
                    "disabled_reason": str(correction.get("disabled_reason") or ""),
                },
            }
        )
        await self._store_value("pending_player_context", pending)
        return pending

    async def _stage_profile_proposal(
        self,
        recognized_characters: list[str],
        *,
        uncertain_characters: list[str] | None = None,
        import_mode: str = "merge",
        source: str = "chat",
        confidence: str = "mixed",
        note: str = "",
    ) -> dict[str, Any]:
        session, started = await self._touch_session(f"stage_profile:{source}")
        if started:
            await self._push_companion_context(f"auto_start:{source}")
            session = await self._read_session()
        corrections = await self._read_corrections()
        modality = "vision" if source == "screenshot" else "voice" if source == "voice" else "chat"
        visual_request: dict[str, Any] = {}
        if modality == "vision":
            visual_request = await self._read_visual_request()
            game_visual_request = await self._read_game_visual_request()
            if _request_is_recent(game_visual_request) and str(game_visual_request.get("fingerprint") or "") == str(
                visual_request.get("fingerprint") or ""
            ):
                # The generic request carries roster import mode; the local
                # analysis request carries the OCR character-name regions.
                visual_request = {**visual_request, **game_visual_request}
        recent_visual_request = modality != "vision" or _request_is_recent(visual_request)
        visual_id = str(visual_request.get("fingerprint") or "") if recent_visual_request else ""
        recognized_input = list(recognized_characters or [])
        uncertain_input = list(uncertain_characters or [])
        visual_verification = "not_visual"
        if modality == "vision":
            plugin_visual_names = {
                normalize_observed_name(item.get("name"))
                for item in list(visual_request.get("character_regions") or [])
                if isinstance(item, dict) and item.get("name") and item.get("fingerprint")
            }
            plugin_bound = [
                name
                for name in recognized_input
                if recent_visual_request and normalize_observed_name(name) in plugin_visual_names
            ]
            model_only = [name for name in recognized_input if name not in plugin_bound]
            # A dialog model naming a real roster entry only proves that the
            # name exists. Only names found in an OCR character-name region
            # are allowed into the recognized side of a proposal.
            recognized_input = plugin_bound
            uncertain_input = [*model_only, *uncertain_input]
            if model_only or not recent_visual_request:
                confidence = "low"
                note = (f"{str(note or '').strip()} 未绑定到插件角色名区域的模型视觉候选只能作为不确定项。").strip()
            visual_verification = (
                "plugin_ocr_bound"
                if plugin_bound and not model_only
                else "mixed_plugin_and_model"
                if plugin_bound
                else "unverified_model_candidate"
            )
        recognized_resolution = resolve_character_candidates(
            recognized_input,
            registry=self._characters,
            corrections=corrections,
            modality=modality,
            visual_id=visual_id,
            session_id=str(session.get("session_id") or ""),
        )
        uncertain_resolution = resolve_character_candidates(
            uncertain_input,
            registry=self._characters,
            corrections=corrections,
            modality=modality,
            visual_id=visual_id,
            session_id=str(session.get("session_id") or ""),
        )
        recognized = list(recognized_resolution["accepted"])
        recognized_keys = {name.casefold() for name in recognized}
        uncertain = [name for name in uncertain_resolution["accepted"] if name.casefold() not in recognized_keys]
        ambiguous_candidates = [
            *recognized_resolution["ambiguous_candidates"],
            *uncertain_resolution["ambiguous_candidates"],
        ]
        # A saved correction between two real registry characters is only a
        # reminder that the input is ambiguous. Present both choices for
        # review, but never silently promote either one to a recognized fact.
        for ambiguity in ambiguous_candidates:
            for alternative in list(ambiguity.get("alternatives") or []):
                name = str(alternative or "").strip()
                if name and name.casefold() not in recognized_keys and name not in uncertain:
                    uncertain.append(name)
        rejected = list(
            dict.fromkeys(
                [
                    *recognized_resolution["rejected"],
                    *uncertain_resolution["rejected"],
                ]
            )
        )
        proposal = build_profile_proposal(
            recognized,
            uncertain_characters=uncertain,
            import_mode=import_mode,
            source=source,
            confidence=confidence,
            note=note,
            rejected_characters=rejected,
            registry_revision=str(self._characters.meta.get("upstream_revision") or ""),
        )
        proposal.update(
            {
                "proposal_id": uuid4().hex,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "session_id": str(session.get("session_id") or ""),
                "visual_fingerprint": visual_id,
                "visual_verification": visual_verification,
                "applied_corrections": [
                    *recognized_resolution["applied_corrections"],
                    *uncertain_resolution["applied_corrections"],
                ],
                "ambiguous_candidates": ambiguous_candidates,
            }
        )
        stored = await self.store.set("pending_player_context", proposal)
        if isinstance(stored, Err):
            raise RuntimeError("PROFILE_PROPOSAL_STORE_FAILED")
        await self._push_companion_context("profile_proposal_staged")
        return proposal

    async def _confirm_profile_proposal(self, proposal_id: str = "") -> dict[str, Any]:
        proposal = await self._read_pending_profile()
        if not proposal:
            raise ValueError("没有等待确认的识别结果")
        requested_id = str(proposal_id or "").strip()
        if requested_id and requested_id != str(proposal.get("proposal_id") or ""):
            raise ValueError("识别结果已经更新，请刷新后重新确认")
        profile = apply_profile_proposal(await self._read_profile(), proposal)
        stored = await self.store.set("player_context", profile)
        if isinstance(stored, Err):
            raise RuntimeError("PLAYER_CONTEXT_STORE_FAILED")
        await self.store.delete("pending_player_context")
        await self._push_companion_context("profile_confirmed")
        return {
            "profile": profile,
            "applied_characters": list(proposal.get("recognized_characters") or []),
            "import_mode": proposal.get("import_mode") or "merge",
            "proposal_id": proposal.get("proposal_id") or "",
            "memory_boundary": "识别结果经玩家确认后写入插件档案；不等同于 N.E.K.O 长期记忆。",
        }

    async def _remember_correction(
        self,
        *,
        observed_name: str,
        canonical_name: str,
        modality: str,
        note: str = "",
    ) -> dict[str, Any]:
        session, started = await self._touch_session(f"correction:{modality}")
        if started:
            await self._push_companion_context(f"auto_start:correction:{modality}")
            session = await self._read_session()
        pending = await self._read_pending_profile()
        visual_id = ""
        if str(modality or "").strip().lower() == "vision":
            visual_id = str(pending.get("visual_fingerprint") or "")
            if not visual_id:
                request = await self._read_game_visual_request()
                observed_key = normalize_observed_name(observed_name)
                matching_region = next(
                    (
                        item
                        for item in list(request.get("character_regions") or [])
                        if isinstance(item, dict)
                        and normalize_observed_name(item.get("name")) == observed_key
                        and item.get("fingerprint")
                    ),
                    None,
                )
                visual_id = str((matching_region or {}).get("fingerprint") or "")
            if not visual_id:
                raise ValueError(
                    "这次画面没有可绑定的角色名字区域，不能把整张相似界面记成角色纠错；"
                    "请在角色详情或队伍界面重新识别后再纠正。"
                )
        corrections, record = remember_character_correction(
            await self._read_corrections(),
            observed_name=observed_name,
            canonical_name=canonical_name,
            modality=modality,
            registry=self._characters,
            visual_id=visual_id,
            session_id=str(session.get("session_id") or ""),
            note=note,
        )
        await self._store_value("character_corrections", corrections)

        pending_updated = False
        if pending:
            observed_key = normalize_observed_name(observed_name)
            candidate_fields = [
                "recognized_characters",
                "uncertain_characters",
                "rejected_characters",
            ]
            matched = any(
                normalize_observed_name(item) == observed_key
                for field in candidate_fields
                for item in list(pending.get(field) or [])
            )
            if matched:
                pending_modality = (
                    "vision"
                    if str(pending.get("source") or "") == "screenshot"
                    else "voice"
                    if str(pending.get("source") or "") == "voice"
                    else "chat"
                )
                recognized_resolution = resolve_character_candidates(
                    list(pending.get("recognized_characters") or []),
                    registry=self._characters,
                    corrections=corrections,
                    modality=pending_modality,
                    visual_id=str(pending.get("visual_fingerprint") or ""),
                    session_id=str(session.get("session_id") or ""),
                )
                uncertain_resolution = resolve_character_candidates(
                    list(pending.get("uncertain_characters") or []),
                    registry=self._characters,
                    corrections=corrections,
                    modality=pending_modality,
                    visual_id=str(pending.get("visual_fingerprint") or ""),
                    session_id=str(session.get("session_id") or ""),
                )
                canonical = str(record.get("canonical_name") or "")
                recognized = list(recognized_resolution["accepted"])
                is_confirmation_only = record.get("scope") == "confirmation_only"
                if canonical and canonical not in recognized and not is_confirmation_only:
                    recognized.append(canonical)
                recognized_keys = {name.casefold() for name in recognized}
                uncertain = [
                    name for name in uncertain_resolution["accepted"] if name.casefold() not in recognized_keys
                ]
                ambiguous_candidates = [
                    *recognized_resolution["ambiguous_candidates"],
                    *uncertain_resolution["ambiguous_candidates"],
                ]
                for ambiguity in ambiguous_candidates:
                    for alternative in list(ambiguity.get("alternatives") or []):
                        name = str(alternative or "").strip()
                        if name and name.casefold() not in recognized_keys and name not in uncertain:
                            uncertain.append(name)
                rejected = [
                    item
                    for item in list(pending.get("rejected_characters") or [])
                    if normalize_observed_name(item) != observed_key
                ]
                pending.update(
                    {
                        "recognized_characters": recognized,
                        "uncertain_characters": uncertain,
                        "rejected_characters": rejected,
                        "can_confirm": bool(recognized),
                        "confidence": "mixed" if is_confirmation_only else "high",
                        "ambiguous_candidates": ambiguous_candidates,
                        "player_correction_applied": {
                            "observed_name": observed_name,
                            "canonical_name": canonical,
                            "correction_id": record.get("correction_id"),
                        },
                    }
                )
                await self._store_value("pending_player_context", pending)
                pending_updated = True

        context_result = await self._push_companion_context("player_correction_saved")
        return {
            "correction": record,
            "pending_profile_proposal": pending if pending_updated else {},
            "pending_updated": pending_updated,
            "session": context_result["session"],
            "context_delivery": context_result["delivery"],
            "instruction": (
                "这两个名字都是真实角色，已记为本次会话的歧义提醒；"
                "不得自动把其中一个替换成另一个，后续遇到时必须依据新证据或询问玩家。"
                if record.get("scope") == "confirmation_only"
                else "已记住玩家纠正。只有本次输入明确命中该别名或同一角色名区域时才能应用；"
                "不得把历史纠错主动复述为当前画面事实。"
            ),
        }

    @ui.action(
        id="start_companion_session",
        label="开启星铁陪玩",
        tone="success",
        refresh_context=True,
    )
    @plugin_entry(
        id="start_companion_session",
        name="开启星铁陪玩会话",
        description="开启会话并把玩家档案、纠错和严格事实边界主动送入 N.E.K.O 上下文。",
        input_schema={"type": "object", "properties": {}},
        llm_result_fields=["session", "delivery"],
    )
    async def start_companion_session(self, **_):
        try:
            return Ok(await self._start_companion_session("user_start"))
        except RuntimeError as exc:
            return Err(SdkError(str(exc)))

    @ui.action(
        id="stop_companion_session",
        label="结束本次陪玩",
        tone="default",
        refresh_context=True,
    )
    @plugin_entry(
        id="stop_companion_session",
        name="结束星铁陪玩会话",
        description="结束当前星铁场景并让 N.E.K.O 恢复普通聊天，插件档案不会删除。",
        input_schema={"type": "object", "properties": {}},
        llm_result_fields=["session", "delivery"],
    )
    async def stop_companion_session(self, **_):
        try:
            return Ok(await self._stop_companion_session("user_stop"))
        except RuntimeError as exc:
            return Err(SdkError(str(exc)))

    @ui.action(
        id="refresh_companion_context",
        label="重新同步给猫娘",
        tone="primary",
        refresh_context=True,
    )
    @plugin_entry(
        id="refresh_companion_context",
        name="刷新星铁陪玩上下文",
        description="重新把当前玩家档案、纠错和事实规则注入 N.E.K.O，不触发立即回复。",
        input_schema={"type": "object", "properties": {}},
        llm_result_fields=["session", "delivery"],
    )
    async def refresh_companion_context(self, **_):
        try:
            return Ok(await self._push_companion_context("manual_refresh"))
        except RuntimeError as exc:
            return Err(SdkError(str(exc)))

    @ui.action(
        id="remember_character_correction",
        label="记住角色纠正",
        tone="success",
        refresh_context=True,
    )
    @plugin_entry(
        id="remember_character_correction",
        name="记住玩家的星铁角色纠正",
        description=(
            "保存玩家明确给出的角色名纠错。语音或聊天别名可长期使用；"
            "视觉纠错只绑定当前角色名区域，避免误伤真实角色。"
            "方向必须是‘错误名字 → 玩家给出的正确名字’。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "observed_name": {
                    "type": "string",
                    "description": "纠正前的错误名字。例如‘不是花火，是火花’应填写花火",
                },
                "canonical_name": {
                    "type": "string",
                    "description": "玩家明确给出的正确名字。例如‘不是花火，是火花’应填写火花",
                },
                "modality": {
                    "type": "string",
                    "enum": ["chat", "voice", "vision"],
                    "default": "chat",
                },
                "note": {"type": "string", "default": ""},
            },
            "required": ["observed_name", "canonical_name"],
        },
        llm_result_fields=["correction", "pending_updated", "instruction"],
    )
    async def remember_character_correction_entry(
        self,
        observed_name: str,
        canonical_name: str,
        modality: str = "chat",
        note: str = "",
        **_,
    ):
        try:
            return Ok(
                await self._remember_correction(
                    observed_name=observed_name,
                    canonical_name=canonical_name,
                    modality=modality,
                    note=note,
                )
            )
        except (ValueError, RuntimeError) as exc:
            return Err(SdkError(str(exc)))

    @llm_tool(
        name="hsr_remember_character_correction",
        description=(
            "当玩家明确说星铁角色识别错了、名字听错了、或给出正确角色名时必须调用。"
            "它会把纠错保存到插件并立即刷新猫娘可见的星铁上下文。"
            "纠错方向必须是错误值到正确值：玩家说‘不是花火，是火花’时，"
            "observed_name 必须是‘花火’，canonical_name 必须是‘火花’，绝不能反过来。"
            "如果两个名字都是数据库中的真实角色，工具只会记录歧义，不会自动互换。"
            "不要只在自然语言里道歉而不调用本工具。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "observed_name": {
                    "type": "string",
                    "description": "纠正前的错误名字；如‘不是花火，是火花’，这里必须填花火",
                },
                "canonical_name": {
                    "type": "string",
                    "description": "玩家明确给出的正确角色名；如‘不是花火，是火花’，这里必须填火花",
                },
                "modality": {
                    "type": "string",
                    "enum": ["chat", "voice", "vision"],
                    "default": "chat",
                },
                "note": {"type": "string", "default": ""},
            },
            "required": ["observed_name", "canonical_name"],
        },
    )
    async def hsr_remember_character_correction(
        self,
        *,
        observed_name: str,
        canonical_name: str,
        modality: str = "chat",
        note: str = "",
    ):
        try:
            await self._record_tool_use("hsr_remember_character_correction", modality=modality)
            return await self._remember_correction(
                observed_name=observed_name,
                canonical_name=canonical_name,
                modality=modality,
                note=note,
            )
        except (ValueError, RuntimeError) as exc:
            return {
                "output": {"reason": str(exc)},
                "is_error": True,
                "error": "CHARACTER_CORRECTION_FAILED",
            }

    @llm_tool(
        name="hsr_report_game_state",
        description=(
            "记录模型对玩家星铁截图的未验证视觉线索。这个工具不会把模型判断升级为插件事实，"
            "也不能确认角色身份；只有插件本地页面识别或玩家明确确认才是可信状态。"
            "无法确认时报告 unknown，不得猜测。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "primary_state": {
                    "type": "string",
                    "enum": list(GAME_STATE_LABELS),
                    "description": "当前画面的主要游戏状态",
                },
                "substate": {
                    "type": "string",
                    "description": "更具体的页面或玩法，例如角色详情、模拟宇宙、剧情过场",
                    "default": "",
                },
                "confidence": {
                    "type": "string",
                    "enum": ["high", "mixed", "low"],
                    "default": "mixed",
                },
                "evidence": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "支持判断的可见界面证据，不超过六条",
                    "default": [],
                },
                "summary": {
                    "type": "string",
                    "description": "对玩家当前画面的简短事实描述，不要编造画面外内容",
                    "default": "",
                },
            },
            "required": ["primary_state"],
        },
    )
    async def hsr_report_game_state(
        self,
        *,
        primary_state: str,
        substate: str = "",
        confidence: str = "mixed",
        evidence: list[str] | None = None,
        summary: str = "",
    ):
        try:
            session, started = await self._touch_session("game_state_observed")
            if started:
                await self._push_companion_context("auto_start:game_state")
                session = await self._read_session()
            visual_request = await self._read_game_visual_request()
            previous_raw = await self._read_game_state()
            previous = public_game_state(previous_raw)
            observation = build_game_state_observation(
                primary_state=primary_state,
                substate=substate,
                confidence=confidence,
                evidence=evidence or [],
                summary=summary,
                source="model_visual_hint",
                verification="unverified",
                visual_fingerprint=str(visual_request.get("fingerprint") or ""),
            )
            accepted = False
            transition_reason = "unverified_model_hint_not_committed"
            await self._store_value("last_unverified_game_state_hint", observation)
            history = await self._read_game_state_history()
            history.append(
                {
                    **observation,
                    "accepted": accepted,
                    "transition_reason": transition_reason,
                }
            )
            await self._store_value("game_state_history", history[-50:])
            await self._record_tool_use(
                "hsr_report_game_state",
                primary_state=observation["primary_state"],
                confidence=observation["confidence"],
                accepted=accepted,
            )
            current = public_game_state(previous_raw)
            return {
                "game_state": current,
                "unverified_hint": public_game_state(observation),
                "transition": {
                    "from": previous.get("primary_state") or "unknown",
                    "to": current["primary_state"],
                    "changed": False,
                    "accepted": accepted,
                    "reason": transition_reason,
                },
                "session": session,
                "instruction": ("这只是模型视觉线索，插件不会把它当作已验证状态；不得据此断言角色身份或当前场景。"),
            }
        except (ValueError, RuntimeError) as exc:
            return {
                "output": {"reason": str(exc)},
                "is_error": True,
                "error": "GAME_STATE_REPORT_FAILED",
            }

    @llm_tool(
        name="hsr_companion_prepare",
        description=(
            "星铁对话的统一必经入口。凡是涉及《崩坏：星穹铁道》的角色身份、截图识别、"
            "语音角色名、玩家拥有角色、培养、队伍或游戏事实，回答前必须先调用本工具。"
            "它一次性读取玩家档案、待确认结果、严格角色注册表和可用知识来源，"
            "并只对本次明确出现的名字应用相关纠错。"
            "不得因为你自认为知道答案而跳过。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "玩家当前的星铁问题或意图"},
                "intent": {
                    "type": "string",
                    "enum": [
                        "conversation",
                        "character_identity",
                        "knowledge",
                        "team",
                        "profile",
                        "correction",
                        "game_state",
                    ],
                    "default": "conversation",
                },
                "observed_character_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                },
                "modality": {
                    "type": "string",
                    "enum": ["chat", "voice", "vision"],
                    "default": "chat",
                },
                "category": {"type": "string", "default": ""},
            },
            "required": ["query"],
        },
    )
    async def hsr_companion_prepare(
        self,
        *,
        query: str,
        intent: str = "conversation",
        observed_character_names: list[str] | None = None,
        modality: str = "chat",
        category: str = "",
    ):
        try:
            if not self._catalog.status().get("installed"):
                await self._wait_for_data_pack()
            session, started = await self._touch_session(f"prepare:{intent}")
            if started:
                await self._push_companion_context(f"auto_start:prepare:{intent}")
                session = await self._read_session()
            profile = await self._read_profile()
            pending = await self._read_pending_profile()
            corrections = await self._read_corrections()
            game_state = public_game_state(await self._read_game_state())
            visual_id = str(pending.get("visual_fingerprint") or "") if modality == "vision" else ""
            candidates = list(observed_character_names or [])
            normalized_query = normalize_observed_name(query)
            if modality in {"voice", "chat"}:
                for correction in corrections:
                    if not isinstance(correction, dict):
                        continue
                    if correction.get("active") is False:
                        continue
                    observed = str(correction.get("observed_name") or "").strip()
                    observed_key = str(correction.get("observed_key") or "")
                    if (
                        observed
                        and observed_key
                        and observed_key in normalized_query
                        and observed not in candidates
                        and correction.get("scope") == "global_alias"
                        and correction.get("modality") in {modality, "any"}
                    ):
                        candidates.append(observed)
            resolution = resolve_character_candidates(
                candidates,
                registry=self._characters,
                corrections=corrections,
                modality=modality,
                visual_id=visual_id,
                session_id=str(session.get("session_id") or ""),
            )
            type_map = {
                "character": ["character", "character_form"],
                "light_cone": ["light_cone"],
                "relic": ["relic_set"],
            }
            results = self._catalog.search(
                query,
                entity_types=type_map.get(category, []),
                limit=5,
            )
            authoritative_context = build_companion_context(
                session=session,
                profile=profile,
                pending=pending,
                corrections=corrections,
                registry_meta=self._characters.meta,
                registry_count=len(self._characters.records),
                catalog_meta=self._catalog.meta,
                game_state=game_state,
            )
            await self._record_tool_use(
                "hsr_companion_prepare",
                intent=intent,
                modality=modality,
                query=str(query or "")[:160],
                knowledge_hits=len(results),
                observed_count=len(candidates),
            )
            return {
                "session": session,
                "authoritative_context": authoritative_context,
                "player_context": profile,
                "pending_profile_proposal": pending,
                "character_resolution": resolution,
                "knowledge_results": results,
                "database": {
                    "revision": self._catalog.revision,
                    "coverage": self._catalog.coverage,
                    "strict_entity_ids": True,
                    "pack": self._public_data_pack_status(),
                },
                "correction_status": {
                    "active_count": sum(1 for item in corrections if item.get("active") is not False),
                    "disabled_count": sum(1 for item in corrections if item.get("active") is False),
                    "policy": "历史纠错不是当前事实；只使用 character_resolution.applied_corrections。",
                },
                "current_game_state": game_state,
                "answer_policy": {
                    "plugin_facts_first": True,
                    "unknown_means_uncertain": True,
                    "visual_results_require_confirmation": True,
                    "do_not_invent_characters": True,
                    "missing_pack_means_no_knowledge_answer": True,
                },
            }
        except (ValueError, RuntimeError) as exc:
            return {
                "output": {"reason": str(exc)},
                "is_error": True,
                "error": "HSR_PREPARE_FAILED",
            }

    @ui.action(
        id="start_auto_observation",
        label="开始自动观察",
        tone="success",
        refresh_context=True,
    )
    @plugin_entry(
        id="start_auto_observation",
        name="开始自动观察星铁窗口",
        description="经玩家明确开启后，只观察前台星铁窗口；画面仅在内存中处理，不操作游戏。",
        input_schema={"type": "object", "properties": {}},
        llm_result_fields=["observer", "session"],
    )
    async def start_auto_observation(self, **_):
        try:
            settings = await self._read_observer_settings()
            settings["enabled"] = True
            await self._store_value("observer_settings", settings)
            self._observer_status["proactive_enabled"] = bool(settings.get("proactive_enabled", True))
            self._start_observer_task()
            context = await self._start_companion_session("auto_observation_started")
            return Ok({"observer": dict(self._observer_status), **context})
        except RuntimeError as exc:
            return Err(SdkError(str(exc)))

    @ui.action(
        id="stop_auto_observation",
        label="停止自动观察",
        tone="default",
        refresh_context=True,
    )
    @plugin_entry(
        id="stop_auto_observation",
        name="停止自动观察星铁窗口",
        description="立即停止读取星铁窗口；玩家档案、纠错和当前陪玩会话仍保留。",
        input_schema={"type": "object", "properties": {}},
        llm_result_fields=["observer"],
    )
    async def stop_auto_observation(self, **_):
        try:
            settings = await self._read_observer_settings()
            settings["enabled"] = False
            await self._store_value("observer_settings", settings)
            await self._cancel_observer_task()
            return Ok({"observer": dict(self._observer_status)})
        except RuntimeError as exc:
            return Err(SdkError(str(exc)))

    @ui.action(
        id="set_proactive_companionship",
        label="切换主动陪伴",
        tone="default",
        refresh_context=True,
    )
    @plugin_entry(
        id="set_proactive_companionship",
        name="设置星铁主动陪伴",
        description="控制已验证游戏事件是否请求猫娘主动回应；关闭后仍会静默同步可靠状态。",
        input_schema={
            "type": "object",
            "properties": {"enabled": {"type": "boolean"}},
            "required": ["enabled"],
        },
        llm_result_fields=["proactive_companionship"],
    )
    async def set_proactive_companionship(self, enabled: bool, **_):
        try:
            settings = await self._read_observer_settings()
            settings["proactive_enabled"] = bool(enabled)
            await self._store_value("observer_settings", settings)
            self._observer_status["proactive_enabled"] = bool(enabled)
            return Ok(
                {
                    "proactive_companionship": {
                        "enabled": bool(enabled),
                        "message": (
                            "猫娘会在合适的星铁事件出现时主动回应"
                            if enabled
                            else "猫娘只接收静默状态，不会由插件主动发言"
                        ),
                    }
                }
            )
        except RuntimeError as exc:
            return Err(SdkError(str(exc)))

    @ui.action(
        id="prepare_data_pack",
        label="准备资料组件",
        tone="primary",
        refresh_context=True,
    )
    @plugin_entry(
        id="prepare_data_pack",
        name="准备星铁资料组件",
        description="在插件外部下载并校验固定版本的星铁结构化资料包。",
        input_schema={"type": "object", "properties": {}},
        llm_result_fields=["data_pack"],
    )
    async def prepare_data_pack(self, **_):
        return Ok({"data_pack": await self._wait_for_data_pack(force=True)})

    @ui.context(id="demo", title="星铁游戏搭子")
    async def demo_ui_context(self, **_):
        observer_settings = await self._read_observer_settings()
        if (
            isinstance(observer_settings, dict)
            and observer_settings.get("enabled") is True
            and (self._observer_task is None or self._observer_task.done())
        ):
            # Hosted UI contexts run on the process command loop, which is
            # long-lived and can safely own the restored observer task.
            self._start_observer_task(restored=True)
        experiences = unwrap_or(await self.store.get("experiences"), [])
        if not isinstance(experiences, list):
            experiences = []
        profile = await self._read_profile()
        pending_profile = await self._read_pending_profile()
        session = await self._read_session()
        corrections = await self._read_corrections()
        game_state = public_game_state(await self._read_game_state())
        context_audit = await self._read_context_audit()
        tool_audit = await self._read_tool_audit()
        character_options = [
            {
                "name": str(record.get("name") or "").strip(),
                "entity_id": str(record.get("id") or "").strip(),
            }
            for record in self._catalog.records
            if record.get("entity_type") == "character" and record.get("name")
        ]
        local_vision = unwrap_or(await self.store.get("last_local_vision"), {})
        if not isinstance(local_vision, dict):
            local_vision = {}
        return {
            "status": "ready",
            "version": "0.8.2",
            "catalog": self._catalog.meta,
            "record_count": self._catalog.record_count,
            "database_coverage": self._catalog.coverage,
            "data_pack": self._public_data_pack_status(),
            "character_registry": {
                **self._characters.meta,
                "record_count": len(self._characters.records),
                "strict_validation": True,
            },
            "tool_count": 11,
            "session": {
                **session,
                "active": session_is_active(session, ttl_seconds=self._session_ttl_seconds),
                "ttl_seconds": self._session_ttl_seconds,
            },
            "context_delivery": context_audit[-1] if context_audit else {},
            "last_tool_use": tool_audit[-1] if tool_audit else {},
            "tool_use_count": len(tool_audit),
            "observer": dict(self._observer_status),
            "proactive_companionship": {
                "enabled": bool(observer_settings.get("proactive_enabled", True)),
                "mode": "event_gated",
                "idle_scene": "exploration",
                "idle_interval_seconds": self._runtime.idle_chatter_seconds,
            },
            "local_vision": local_vision,
            "vision_backend": self._vision.status(),
            "runtime": self._runtime.snapshot(),
            "correction_count": sum(1 for item in corrections if item.get("active") is not False),
            "disabled_correction_count": sum(1 for item in corrections if item.get("active") is False),
            "recent_corrections": list(
                reversed(
                    public_correction_summary([item for item in corrections if item.get("active") is not False])[-8:]
                )
            ),
            "current_game_state": game_state,
            "unverified_game_state_hint": public_game_state(
                unwrap_or(await self.store.get("last_unverified_game_state_hint"), {})
            ),
            "profile": profile,
            "pending_profile_proposal": pending_profile,
            "character_options": character_options,
            "experience_count": len(experiences),
            "recent_experiences": list(reversed(experiences[-5:])),
            "memory_boundary": (
                "玩家档案、会话和纠错保存在插件中，并会在陪玩开启时主动同步给 N.E.K.O；"
                "它们仍不等同于 N.E.K.O 本体的长期情感记忆。"
            ),
        }

    @ui.action(
        id="submit_game_screenshot",
        label="识别当前游戏画面",
        tone="primary",
        refresh_context=False,
    )
    @plugin_entry(
        id="submit_game_screenshot",
        name="让 N.E.K.O 判断当前星铁画面",
        description=(
            "把用户主动选择的当前游戏截图交给 N.E.K.O，判断剧情、战斗、探索、菜单等状态；原图不写入插件存储。"
        ),
        input_schema={
            "type": "object",
            "properties": {"image_data_url": {"type": "string"}},
            "required": ["image_data_url"],
        },
        llm_result_fields=["submitted", "message", "image_stored"],
    )
    async def submit_game_screenshot(self, image_data_url: str, **_):
        try:
            image_bytes, mime = prepare_image_for_vision(image_data_url)
            image_fingerprint = visual_fingerprint(image_bytes)
            await self._store_value(
                "active_game_visual_request",
                {
                    "fingerprint": image_fingerprint,
                    "created_at": utc_now_iso(),
                    "mime": mime,
                    "image_stored": False,
                    "source": "manual_screenshot_local_ocr",
                },
            )
            analysis = await asyncio.to_thread(self._vision.analyze, image_bytes)
            local_result = await self._apply_local_analysis(
                analysis,
                image_fingerprint=image_fingerprint,
                source="manual_screenshot_local_ocr",
                deliver_event=False,
            )
            scene = dict(local_result.get("scene") or {})
            characters = list(local_result.get("characters") or [])
            scene_key = str(scene.get("primary_state") or "unknown")
            names = [str(item.get("name") or "") for item in characters]
            prompt = (
                "[星铁插件本地截图识别结果]\n"
                f"场景：{scene_key}（{GAME_STATE_LABELS.get(scene_key, GAME_STATE_LABELS['unknown'])}）\n"
                f"置信度：{scene.get('confidence') or 'low'}\n"
                f"证据：{'；'.join(scene.get('evidence') or []) or '无足够证据'}\n"
                f"数据库确认角色：{'、'.join(names) if names else '无'}\n"
                f"数据库版本：{self._catalog.revision}\n"
                "请直接转述插件结果；unknown 就说明无法确认。禁止补猜未返回实体 ID 的角色。"
            )
            push_result = self.push_message(
                visibility=[],
                ai_behavior="respond",
                parts=[
                    {"type": "text", "text": prompt},
                    {"type": "image", "data": image_bytes, "mime": mime},
                ],
                source="hsr_companion",
                metadata={
                    "kind": "game_state_local_ocr_result",
                    "image_stored": False,
                    "catalog_revision": self._catalog.revision,
                },
                priority=5,
                coalesce_key="hsr_game_state_scan",
            )
            submitted = bool(push_result.get("submitted"))
            return Ok(
                {
                    "submitted": submitted,
                    "message": (
                        f"本地识别完成：{GAME_STATE_LABELS.get(scene_key, GAME_STATE_LABELS['unknown'])}。"
                        if submitted
                        else "本地识别已完成，但结果没有成功送达聊天。"
                    ),
                    "delivery_reason": push_result.get("reason"),
                    "image_stored": False,
                    "analysis": local_result,
                    "visual_fingerprint": image_fingerprint,
                }
            )
        except (ValueError, RuntimeError) as exc:
            return Err(SdkError(str(exc)))

    @ui.action(
        id="submit_roster_screenshot",
        label="识别角色截图",
        tone="primary",
        refresh_context=False,
    )
    @plugin_entry(
        id="submit_roster_screenshot",
        name="让 N.E.K.O 识别角色截图",
        description="把用户主动选择的角色列表截图交给 N.E.K.O 视觉能力；原图不写入插件存储，识别结果必须再次确认。",
        input_schema={
            "type": "object",
            "properties": {
                "image_data_url": {"type": "string"},
                "import_mode": {
                    "type": "string",
                    "enum": ["merge", "replace"],
                    "default": "merge",
                },
            },
            "required": ["image_data_url"],
        },
        llm_result_fields=["submitted", "message", "image_stored"],
    )
    async def submit_roster_screenshot(
        self,
        image_data_url: str,
        import_mode: str = "merge",
        **_,
    ):
        try:
            mode = str(import_mode or "merge").strip().lower()
            if mode not in {"merge", "replace"}:
                raise ValueError("请选择这是完整列表还是补充截图")
            image_bytes, mime = prepare_image_for_vision(image_data_url)
            image_fingerprint = visual_fingerprint(image_bytes)
            await self._store_value(
                "active_visual_request",
                {
                    "fingerprint": image_fingerprint,
                    "created_at": utc_now_iso(),
                    "import_mode": mode,
                    "mime": mime,
                    "image_stored": False,
                },
            )
            await self._store_value(
                "active_game_visual_request",
                {
                    "fingerprint": image_fingerprint,
                    "created_at": utc_now_iso(),
                    "mime": mime,
                    "source": "roster_screenshot_local_ocr",
                    "image_stored": False,
                },
            )
            analysis = await asyncio.to_thread(self._vision.analyze, image_bytes)
            local_result = await self._apply_local_analysis(
                analysis,
                image_fingerprint=image_fingerprint,
                source="roster_screenshot_local_ocr",
                deliver_event=False,
            )
            names = [
                str(item.get("name") or "")
                for item in local_result.get("characters") or []
                if isinstance(item, dict) and item.get("entity_id")
            ]
            proposal: dict[str, Any] = {}
            if names:
                proposal = await self._stage_profile_proposal(
                    names,
                    import_mode=mode,
                    source="screenshot",
                    confidence="high",
                    note="由 0.8 页面分区 OCR 与版本化角色注册表严格匹配；等待玩家确认。",
                )
            prompt = (
                "[星铁插件本地角色识别结果]\n"
                f"数据库确认角色：{'、'.join(names) if names else '没有可靠匹配'}\n"
                f"数据库实体 ID：{'、'.join(str(item.get('entity_id')) for item in local_result.get('characters') or []) or '无'}\n"
                f"数据库版本：{self._catalog.revision}\n"
                + (
                    "识别结果已暂存，必须等待玩家确认后才能写入档案。"
                    if proposal
                    else "不要猜角色；请建议玩家截取包含清晰角色名字的列表区域。"
                )
            )
            push_result = self.push_message(
                visibility=[],
                ai_behavior="respond",
                parts=[
                    {"type": "text", "text": prompt},
                    {"type": "image", "data": image_bytes, "mime": mime},
                ],
                source="hsr_companion",
                metadata={
                    "kind": "roster_local_ocr_result",
                    "import_mode": mode,
                    "image_stored": False,
                    "catalog_revision": self._catalog.revision,
                },
                priority=5,
                coalesce_key="hsr_roster_scan",
            )
            submitted = bool(push_result.get("submitted"))
            return Ok(
                {
                    "submitted": submitted,
                    "message": (
                        "本地识别已完成，结果确认前不会写入档案。"
                        if names
                        else "没有识别到数据库中的角色，请截取包含清晰角色名字的区域。"
                    ),
                    "delivery_reason": push_result.get("reason"),
                    "image_stored": False,
                    "analysis": local_result,
                    "proposal": proposal,
                    "visual_fingerprint": image_fingerprint,
                }
            )
        except (ValueError, RuntimeError) as exc:
            return Err(SdkError(str(exc)))

    @plugin_entry(
        id="stage_player_context",
        name="暂存星铁玩家档案识别结果",
        description="暂存从截图或自然语言中整理出的角色列表，等待玩家确认后再写入正式档案。",
        input_schema={
            "type": "object",
            "properties": {
                "recognized_characters": {"type": "array", "items": {"type": "string"}},
                "uncertain_characters": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                },
                "import_mode": {
                    "type": "string",
                    "enum": ["merge", "replace"],
                    "default": "merge",
                },
                "source": {
                    "type": "string",
                    "enum": ["chat", "voice", "screenshot", "panel"],
                    "default": "chat",
                },
                "confidence": {
                    "type": "string",
                    "enum": ["high", "mixed", "low"],
                    "default": "mixed",
                },
                "note": {"type": "string", "default": ""},
            },
            "required": ["recognized_characters"],
        },
        llm_result_fields=["proposal", "confirmation_required", "rejected_characters"],
    )
    async def stage_player_context(
        self,
        recognized_characters: list[str],
        uncertain_characters: list[str] | None = None,
        import_mode: str = "merge",
        source: str = "chat",
        confidence: str = "mixed",
        note: str = "",
        **_,
    ):
        try:
            proposal = await self._stage_profile_proposal(
                recognized_characters,
                uncertain_characters=uncertain_characters,
                import_mode=import_mode,
                source=source,
                confidence=confidence,
                note=note,
            )
            return Ok(
                {
                    "proposal": proposal,
                    "confirmation_required": True,
                    "rejected_characters": proposal.get("rejected_characters", []),
                    "recognition_status": proposal.get("visual_verification", "not_visual"),
                }
            )
        except (ValueError, RuntimeError) as exc:
            return Err(SdkError(str(exc)))

    @llm_tool(
        name="hsr_stage_player_context",
        description=(
            "把玩家截图中识别到、或聊天中尚待确认的星铁角色列表暂存为候选档案。"
            "视觉识别必须使用本工具，绝不能直接调用 hsr_update_player_context 写正式档案。"
            "必须区分确定角色和不确定角色，且不得补猜未显示内容。"
            "插件会用固定版本角色数据库做严格校验，未收录名字会被拦截；"
            "但‘名字存在于数据库’不代表‘截图里就是这个角色’。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "recognized_characters": {"type": "array", "items": {"type": "string"}},
                "uncertain_characters": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                },
                "import_mode": {
                    "type": "string",
                    "enum": ["merge", "replace"],
                    "default": "merge",
                },
                "source": {
                    "type": "string",
                    "enum": ["chat", "voice", "screenshot", "panel"],
                    "default": "chat",
                },
                "confidence": {
                    "type": "string",
                    "enum": ["high", "mixed", "low"],
                    "default": "mixed",
                },
                "note": {"type": "string", "default": ""},
            },
            "required": ["recognized_characters"],
        },
    )
    async def hsr_stage_player_context(
        self,
        *,
        recognized_characters: list[str],
        uncertain_characters: list[str] | None = None,
        import_mode: str = "merge",
        source: str = "chat",
        confidence: str = "mixed",
        note: str = "",
    ):
        try:
            proposal = await self._stage_profile_proposal(
                recognized_characters,
                uncertain_characters=uncertain_characters,
                import_mode=import_mode,
                source=source,
                confidence=confidence,
                note=note,
            )
            await self._record_tool_use(
                "hsr_stage_player_context",
                source=source,
                recognized_count=len(recognized_characters or []),
                uncertain_count=len(uncertain_characters or []),
            )
            return {
                "proposal": proposal,
                "confirmation_required": True,
                "rejected_characters": proposal.get("rejected_characters", []),
                "recognition_status": proposal.get("visual_verification", "not_visual"),
                "instruction": (
                    "这是插件刚处理的截图候选，可以请玩家核对；确认前不得写入档案。"
                    if proposal.get("visual_verification") == "plugin_bound"
                    else "这只是模型视觉猜测，数据库仅证明角色名存在，不能告诉玩家已经识别成功；"
                    "请明确说无法可靠确认，并建议使用插件面板识别包含清晰角色名的画面。"
                ),
            }
        except (ValueError, RuntimeError) as exc:
            return {
                "output": {"reason": str(exc)},
                "is_error": True,
                "error": "PROFILE_STAGE_FAILED",
            }

    @ui.action(
        id="confirm_profile_proposal",
        label="确认并保存",
        tone="success",
        refresh_context=True,
    )
    @plugin_entry(
        id="confirm_profile_proposal",
        name="确认星铁档案识别结果",
        description="在玩家明确确认后，把等待中的识别结果写入插件玩家档案。",
        input_schema={
            "type": "object",
            "properties": {"proposal_id": {"type": "string", "default": ""}},
        },
        llm_result_fields=["profile", "applied_characters", "memory_boundary"],
    )
    async def confirm_profile_proposal(self, proposal_id: str = "", **_):
        try:
            return Ok(await self._confirm_profile_proposal(proposal_id))
        except (ValueError, RuntimeError) as exc:
            return Err(SdkError(str(exc)))

    @llm_tool(
        name="hsr_confirm_player_context",
        description="仅当玩家明确说确认、保存或没问题时，确认最近一次待处理的星铁档案识别结果。",
        parameters={
            "type": "object",
            "properties": {"proposal_id": {"type": "string", "default": ""}},
        },
    )
    async def hsr_confirm_player_context(self, *, proposal_id: str = ""):
        try:
            await self._record_tool_use("hsr_confirm_player_context")
            return await self._confirm_profile_proposal(proposal_id)
        except (ValueError, RuntimeError) as exc:
            return {
                "output": {"reason": str(exc)},
                "is_error": True,
                "error": "PROFILE_CONFIRM_FAILED",
            }

    @ui.action(
        id="discard_profile_proposal",
        label="这次不保存",
        tone="default",
        refresh_context=True,
    )
    @plugin_entry(
        id="discard_profile_proposal",
        name="放弃星铁档案识别结果",
        description="丢弃当前等待确认的识别结果，不修改正式玩家档案。",
        input_schema={"type": "object", "properties": {}},
        llm_result_fields=["discarded"],
    )
    async def discard_profile_proposal(self, **_):
        pending = await self._read_pending_profile()
        await self.store.delete("pending_player_context")
        if session_is_active(await self._read_session(), ttl_seconds=self._session_ttl_seconds):
            await self._push_companion_context("profile_proposal_discarded")
        return Ok({"discarded": bool(pending), "profile_changed": False})

    @llm_tool(
        name="hsr_discard_player_context",
        description="当玩家明确否认、取消或要求不要保存最近的星铁档案识别结果时，丢弃待确认结果。",
        parameters={"type": "object", "properties": {}},
    )
    async def hsr_discard_player_context(self):
        pending = await self._read_pending_profile()
        await self.store.delete("pending_player_context")
        await self._record_tool_use("hsr_discard_player_context")
        if session_is_active(await self._read_session(), ttl_seconds=self._session_ttl_seconds):
            await self._push_companion_context("profile_proposal_discarded")
        return {"discarded": bool(pending), "profile_changed": False}

    def _lookup_payload(self, query: str, category: str = "", limit: int = 5):
        query = str(query or "").strip()
        if not query:
            raise ValueError("query is required")
        type_map = {
            "character": ["character", "character_form"],
            "light_cone": ["light_cone"],
            "relic": ["relic_set"],
            "item": ["item"],
            "path": ["path"],
            "element": ["element"],
        }
        results = self._catalog.search(
            query,
            entity_types=type_map.get(category, []),
            limit=limit,
        )
        pack = self._public_data_pack_status()
        source = "external_v08" if pack.get("installed") else "minimal_character_registry"
        return {
            "query": query,
            "count": len(results),
            "results": results,
            "catalog": self._catalog.meta,
            "data_pack": pack,
            "result_source": source,
            "notice": (
                "结果来自固定版本、带来源的外部本地资料组件。"
                if pack.get("installed")
                else "完整资料组件尚未就绪；当前只能确认最小角色注册表中的身份，不得补猜技能或攻略。"
            ),
        }

    @ui.action(id="lookup", label="查询知识", tone="primary", refresh_context=False)
    @plugin_entry(
        id="lookup",
        name="查询星铁知识",
        description="查询带来源、数据版本和校验状态的星铁结构化数据库。",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "category": {"type": "string", "default": ""},
                "limit": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
        llm_result_fields=["query", "count", "results", "notice"],
    )
    async def lookup(self, query: str, category: str = "", limit: int = 5, **_):
        try:
            return Ok(self._lookup_payload(query, category, limit))
        except ValueError as exc:
            return Err(SdkError(str(exc)))

    @llm_tool(
        name="hsr_lookup",
        description="查询《崩坏：星穹铁道》知识。结果包含来源和版本校验状态；回答事实问题时应优先调用。",
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "角色、光锥、遗器或机制名称",
                },
                "category": {
                    "type": "string",
                    "enum": [
                        "",
                        "character",
                        "light_cone",
                        "relic",
                        "item",
                        "path",
                        "element",
                        "mechanic",
                        "game_mode",
                    ],
                    "default": "",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
            },
            "required": ["query"],
        },
    )
    async def hsr_lookup(self, *, query: str, category: str = "", limit: int = 5):
        try:
            _, started = await self._touch_session("tool:hsr_lookup")
            if started:
                await self._push_companion_context("auto_start:hsr_lookup")
            await self._record_tool_use("hsr_lookup", query=str(query or "")[:160], category=category)
            return self._lookup_payload(query, category, limit)
        except ValueError as exc:
            return {
                "output": {"reason": str(exc)},
                "is_error": True,
                "error": "INVALID_QUERY",
            }

    @ui.action(
        id="update_player_context",
        label="更新玩家档案",
        tone="success",
        refresh_context=True,
    )
    @plugin_entry(
        id="update_player_context",
        name="更新玩家星铁语境",
        description="根据玩家明确提供的信息更新档案；角色字段必须通过固定版本角色数据库校验。",
        input_schema={
            "type": "object",
            "properties": {
                "changes": {"type": "object", "properties": PROFILE_PROPERTIES},
                "mode": {
                    "type": "string",
                    "enum": ["merge", "replace"],
                    "default": "merge",
                },
                "clear_fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                },
            },
            "required": ["changes"],
        },
        llm_result_fields=["profile", "memory_boundary"],
    )
    async def update_player_context(
        self,
        changes: dict[str, Any],
        mode: str = "merge",
        clear_fields: list[str] | None = None,
        **_,
    ):
        try:
            profile = await self._write_profile(changes, mode=mode, clear_fields=clear_fields)
            if session_is_active(await self._read_session(), ttl_seconds=self._session_ttl_seconds):
                await self._push_companion_context("profile_updated")
            return Ok(
                {
                    "profile": profile,
                    "memory_boundary": "插件结构化档案；不等同于 N.E.K.O 长期情感记忆。",
                }
            )
        except (ValueError, RuntimeError) as exc:
            return Err(SdkError(str(exc)))

    @llm_tool(
        name="hsr_update_player_context",
        description=(
            "仅在玩家用文字明确提供并要求记住、更正或删除星铁信息时，更新结构化游戏档案。"
            "不得用于截图或视觉推断；视觉结果必须先调用 hsr_stage_player_context 并等待确认。"
            "角色字段必须通过插件角色数据库校验，禁止编造或近似猜测。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "changes": {"type": "object", "properties": PROFILE_PROPERTIES},
                "mode": {
                    "type": "string",
                    "enum": ["merge", "replace"],
                    "default": "merge",
                },
                "clear_fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                },
            },
            "required": ["changes"],
        },
    )
    async def hsr_update_player_context(
        self,
        *,
        changes: dict[str, Any],
        mode: str = "merge",
        clear_fields: list[str] | None = None,
    ):
        try:
            _, started = await self._touch_session("tool:hsr_update_player_context")
            if started:
                await self._push_companion_context("auto_start:hsr_update_player_context")
            profile = await self._write_profile(changes, mode=mode, clear_fields=clear_fields)
            await self._record_tool_use("hsr_update_player_context", mode=mode, field_count=len(changes or {}))
            await self._push_companion_context("profile_updated_by_chat")
            return {
                "profile": profile,
                "memory_boundary": "插件结构化档案；不等同于 N.E.K.O 长期情感记忆。",
            }
        except (ValueError, RuntimeError) as exc:
            return {
                "output": {"reason": str(exc)},
                "is_error": True,
                "error": "PROFILE_UPDATE_FAILED",
            }

    @ui.action(
        id="get_player_context",
        label="读取玩家档案",
        tone="default",
        refresh_context=False,
    )
    @plugin_entry(
        id="get_player_context",
        name="读取玩家星铁语境",
        description="读取玩家主动提供的结构化星铁档案。",
        llm_result_fields=["profile", "memory_boundary"],
    )
    async def get_player_context(self, **_):
        return Ok(
            {
                "profile": await self._read_profile(),
                "memory_boundary": "插件结构化档案；不等同于 N.E.K.O 长期情感记忆。",
            }
        )

    @llm_tool(
        name="hsr_get_player_context",
        description="读取玩家主动提供的星铁角色、目标、偏好和进度档案，用于个性化分析。",
        parameters={"type": "object", "properties": {}},
    )
    async def hsr_get_player_context(self):
        session, started = await self._touch_session("tool:hsr_get_player_context")
        if started:
            await self._push_companion_context("auto_start:hsr_get_player_context")
            session = await self._read_session()
        corrections = await self._read_corrections()
        await self._record_tool_use("hsr_get_player_context")
        return {
            "profile": await self._read_profile(),
            "session": session,
            "pending_profile_proposal": await self._read_pending_profile(),
            "correction_status": {
                "active_count": sum(1 for item in corrections if item.get("active") is not False),
                "disabled_count": sum(1 for item in corrections if item.get("active") is False),
                "policy": "历史纠错不会自动作为当前角色返回。",
            },
            "memory_boundary": "插件结构化档案；不等同于 N.E.K.O 长期情感记忆。",
        }

    async def _analysis_payload(self, members: list[str], goal: str = ""):
        return analyze_team(
            members,
            knowledge=self._knowledge,
            player_context=await self._read_profile(),
            goal=goal,
        )

    @ui.action(id="analyze_team", label="分析队伍", tone="primary", refresh_context=False)
    @plugin_entry(
        id="analyze_team",
        name="分析星铁队伍",
        description="结合带来源的结构化目录、精选机制笔记和玩家档案做透明的基础队伍检查。",
        input_schema={
            "type": "object",
            "properties": {
                "members": {"type": "array", "items": {"type": "string"}},
                "goal": {"type": "string", "default": ""},
            },
            "required": ["members"],
        },
        llm_result_fields=[
            "members",
            "goal",
            "strengths",
            "issues",
            "recommended_next_steps",
            "evidence",
            "disclaimer",
        ],
    )
    async def analyze_team_entry(self, members: list[str], goal: str = "", **_):
        return Ok(await self._analysis_payload(members, goal))

    @llm_tool(
        name="hsr_analyze_team",
        description="结合玩家已记录的角色与目标，检查星铁队伍的输出、生存、辅助结构；结论会标明规则覆盖范围。",
        parameters={
            "type": "object",
            "properties": {
                "members": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                },
                "goal": {"type": "string", "default": ""},
            },
            "required": ["members"],
        },
    )
    async def hsr_analyze_team(self, *, members: list[str], goal: str = ""):
        _, started = await self._touch_session("tool:hsr_analyze_team")
        if started:
            await self._push_companion_context("auto_start:hsr_analyze_team")
        await self._record_tool_use("hsr_analyze_team", member_count=len(members or []), goal=goal[:160])
        return await self._analysis_payload(members, goal)

    async def _record_experience(
        self,
        *,
        event_type: str,
        summary: str,
        importance: str = "normal",
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        summary = str(summary or "").strip()
        if not summary:
            raise ValueError("summary is required")
        experiences = unwrap_or(await self.store.get("experiences"), [])
        if not isinstance(experiences, list):
            experiences = []
        item = {
            "experience_id": uuid4().hex,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "event_type": str(event_type or "other").strip() or "other",
            "summary": summary,
            "importance": importance if importance in {"normal", "important", "milestone"} else "normal",
            "tags": list(dict.fromkeys(str(tag).strip() for tag in (tags or []) if str(tag).strip())),
            "memory_status": "plugin_record_only",
        }
        experiences.append(item)
        experiences = experiences[-self._max_experiences :]
        stored = await self.store.set("experiences", experiences)
        if isinstance(stored, Err):
            raise RuntimeError("EXPERIENCE_STORE_FAILED")
        return item

    @ui.action(
        id="record_experience",
        label="记录游戏经历",
        tone="success",
        refresh_context=True,
    )
    @plugin_entry(
        id="record_experience",
        name="记录星铁经历",
        description="记录玩家明确确认的星铁经历；记录只保存在插件档案中。",
        input_schema={
            "type": "object",
            "properties": {
                "event_type": {"type": "string"},
                "summary": {"type": "string"},
                "importance": {
                    "type": "string",
                    "enum": ["normal", "important", "milestone"],
                    "default": "normal",
                },
                "tags": {"type": "array", "items": {"type": "string"}, "default": []},
            },
            "required": ["event_type", "summary"],
        },
        llm_result_fields=["experience", "memory_boundary"],
    )
    async def record_experience(
        self,
        event_type: str,
        summary: str,
        importance: str = "normal",
        tags: list[str] | None = None,
        **_,
    ):
        try:
            item = await self._record_experience(
                event_type=event_type,
                summary=summary,
                importance=importance,
                tags=tags,
            )
            return Ok(
                {
                    "experience": item,
                    "memory_boundary": "这是插件内可更正的事实记录，不会直接写入 N.E.K.O 长期记忆。",
                }
            )
        except (ValueError, RuntimeError) as exc:
            return Err(SdkError(str(exc)))

    @llm_tool(
        name="hsr_record_experience",
        description="当玩家明确确认一段值得保留的星铁经历时记录它。不要把猜测、攻略结论或未发生的愿望记成经历。",
        parameters={
            "type": "object",
            "properties": {
                "event_type": {"type": "string"},
                "summary": {"type": "string"},
                "importance": {
                    "type": "string",
                    "enum": ["normal", "important", "milestone"],
                    "default": "normal",
                },
                "tags": {"type": "array", "items": {"type": "string"}, "default": []},
            },
            "required": ["event_type", "summary"],
        },
    )
    async def hsr_record_experience(
        self,
        *,
        event_type: str,
        summary: str,
        importance: str = "normal",
        tags: list[str] | None = None,
    ):
        try:
            _, started = await self._touch_session("tool:hsr_record_experience")
            if started:
                await self._push_companion_context("auto_start:hsr_record_experience")
            await self._record_tool_use("hsr_record_experience", event_type=event_type, importance=importance)
            return {
                "experience": await self._record_experience(
                    event_type=event_type,
                    summary=summary,
                    importance=importance,
                    tags=tags,
                ),
                "memory_boundary": "这是插件内可更正的事实记录，不会直接写入 N.E.K.O 长期记忆。",
            }
        except (ValueError, RuntimeError) as exc:
            return {
                "output": {"reason": str(exc)},
                "is_error": True,
                "error": "EXPERIENCE_RECORD_FAILED",
            }

    @plugin_entry(
        id="notify_game_event",
        name="提供星铁事件",
        description="向 N.E.K.O 提供一个去重、可过期的星铁事件。默认仅预览，不推送。",
        input_schema={
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "event_type": {"type": "string"},
                "summary": {"type": "string"},
                "occurred_at": {"type": "string", "description": "ISO 8601 时间"},
                "expires_in_seconds": {"type": "integer", "default": 300},
                "request_response": {"type": "boolean", "default": False},
                "dry_run": {"type": "boolean", "default": True},
            },
            "required": ["event_id", "event_type", "summary", "occurred_at"],
        },
        llm_result_fields=["accepted", "reason", "submitted", "dry_run"],
    )
    async def notify_game_event(
        self,
        event_id: str,
        event_type: str,
        summary: str,
        occurred_at: str,
        expires_in_seconds: int = 300,
        request_response: bool = False,
        dry_run: bool = True,
        **_,
    ):
        _, started = await self._touch_session(f"event:{event_type}")
        if started or str(event_type or "") == "game_session_start":
            await self._push_companion_context(f"event_context:{event_type}")
        seen = unwrap_or(await self.store.get("seen_event_ids"), [])
        seen = seen if isinstance(seen, list) else []
        decision = evaluate_event(
            event_id=event_id,
            occurred_at=occurred_at,
            expires_in_seconds=expires_in_seconds,
            seen_event_ids=seen,
        )
        if not decision["accepted"] or dry_run:
            return Ok({**decision, "submitted": False, "dry_run": bool(dry_run)})

        message = (
            "[星铁陪玩事件]\n"
            f"事件类型：{str(event_type).strip()}\n"
            f"已知事实：{str(summary).strip()}\n"
            "请结合你自己的人格、当前对话和玩家语境，自行决定如何自然回应；不要把事件扩写成未发生的事实。"
        )
        push_result = self.push_message(
            visibility=[],
            ai_behavior="respond" if request_response else "read",
            parts=[{"type": "text", "text": message}],
            source="hsr_companion",
            metadata={"event_id": event_id, "event_type": event_type},
            priority=5 if request_response else 1,
        )
        submitted = bool(push_result.get("submitted"))
        if submitted:
            seen.append(event_id)
            await self.store.set("seen_event_ids", seen[-self._event_dedup_limit :])
        return Ok(
            {
                **decision,
                "submitted": submitted,
                "dry_run": False,
                "delivery_reason": push_result.get("reason"),
                "ai_behavior": "respond" if request_response else "read",
            }
        )

    @ui.action(
        id="reset_demo_data",
        label="清空插件数据",
        tone="danger",
        confirm="只清空星铁插件保存的玩家档案、纠错、状态和经历？",
        refresh_context=True,
    )
    @plugin_entry(
        id="reset_demo_data",
        name="清空星铁插件数据",
        description="仅清空本插件保存的玩家档案、纠错、画面状态、经历和事件去重记录。",
        input_schema={"type": "object", "properties": {}},
        llm_result_fields=["cleared_keys"],
    )
    async def reset_demo_data(self, **_):
        await self._cancel_observer_task()
        self._runtime.reset()
        if session_is_active(await self._read_session(), ttl_seconds=self._session_ttl_seconds):
            await self._stop_companion_session("reset_demo_data")
        cleared: list[str] = []
        for key in (
            "player_context",
            "pending_player_context",
            "experiences",
            "seen_event_ids",
            "companion_session",
            "character_corrections",
            "context_audit",
            "tool_audit",
            "active_visual_request",
            "active_game_visual_request",
            "current_game_state",
            "last_unverified_game_state_hint",
            "game_state_history",
            "pending_game_state_candidate",
            "observer_settings",
            "last_local_vision",
        ):
            deleted = unwrap_or(await self.store.delete(key), False)
            if deleted:
                cleared.append(key)
        return Ok({"cleared_keys": cleared})


__all__ = ["HsrCompanionPlugin"]
