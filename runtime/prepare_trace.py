"""Bounded prepare diagnostics, NOT an observation, routing or speech gate.

Capture kwargs before Python applies defaults. No chat, images, raw exception
messages or fact values are retained. Failed calls alone use the existing
plugin KV store; successful/missing reads have no extra IO. Hard process death
or host rejection before this wrapper is outside this plugin-local boundary.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from uuid import uuid4

from .query_plan import SCREEN_FIELDS, SEMANTIC_INTENTS
from .consultation_contract import CORRECTION_REASONS, CURRENT_SCOPES

STORE_KEY = "prepare_failure_records_v1"
LIMIT = 4
PERSIST_TIMEOUT = .35
ROOT = Path(__file__).resolve().parents[1]
_active = ContextVar("hsr_prepare_trace", default=None)
STAGES = {"entry", "argument_binding", "initialization", "pending_reply", "equipment_reply", "query_plan",
          "guidance_catalog", "guidance_plan", "data_pack", "session", "player_context", "observation",
          "fact_projection", "grounding", "tool_audit", "delivery_boundary", "evaluation",
          "request_completion", "followup_registration", "payload", "answer_delivery", "returned",
          "consultation_corrected"}
ENUMS = set(SCREEN_FIELDS) | set(SEMANTIC_INTENTS) | {
    "conversation", "character_identity", "knowledge", "team", "profile", "correction", "game_state",
    "chat", "voice", "vision", "character", "light_cone", "relic", "brief", "full", "cone",
    "current_relic", "current_cone", "current_panel", "reference_build", "saved_cone", "inherit",
    "specified", "unresolved", "current", "rows", "assessment", "next_step", "options", "confirm", "cancel",
    "new_request", "uncertain", "ascension", "trace", ""}
ARGUMENTS = ("query", "intent", "observed_character_names", "modality", "category", "cultivation_plan",
             "screen_fields", "response_detail", "discussion_character", "consultation", "player_reply", "pending_reply")
NESTED = {"scope", "subject_state", "character", "topics", "answer_parts", "request_id", "relation",
          "player_message", "kind", "entity_id", "current", "target"}
PRIVATE = {"query", "player_reply", "player_message"}
ROUTING_CODES = {"intent", "scope", "subject_state", "screen_fields", "response_detail", "modality",
                 "category", "topics", "answer_parts", "relation", "kind"}
ERROR_CODES = {"invalid_screen_fields", "invalid_response_detail", "missing_screen_fields",
               "screen_intent_conflict", "invalid_consultation_declaration", "invalid_consultation_subject",
               "invalid_consultation_topics", "invalid_consultation_answer_parts", "scope_intent_conflict",
               "subject_conflict", "internal_exception", "request_cancelled", "request_timeout", "tool_error"}
STATES = {"ready", "partial", "unconfirmed", "paused", "capture_failed", "not_requested", "complete",
          "needs_evidence", "needs_subject", "needs_subject_binding", "subject_mismatch", "rules_unavailable",
          "needs_source", "needs_saved_build", "needs_cone_data", "superseded", "experiment_disabled"}


def _digest(value):
    # Do not call arbitrary objects' repr/str while diagnosing malformed input.
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=True,
                                                         default=lambda x: type(x).__name__)
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _node(owner, value, key, depth=0, budget=None):
    kind = "null" if value is None else type(value).__name__
    result = {"presence": "provided", "type": kind if kind in {"null", "str", "list", "tuple", "dict", "bool", "int", "float"} else "other"}
    if budget is not None:
        if budget[0] <= 0:
            return {**result, "truncated": True}
        budget[0] -= 1
    if value is None:
        return result
    if isinstance(value, str):
        result.update(length=len(value), sha256=_digest(value))
        if key not in PRIVATE and (value in ENUMS or key in ROUTING_CODES and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value)
                                  or key == "request_id" and re.fullmatch(r"[a-f0-9]{32}", value)):
            result["value"] = value
        elif key in {"character", "discussion_character", "observed_character_names"}:
            catalog = getattr(owner, "_catalog", None)
            try:
                role = catalog.resolve_character(value) if catalog else None
                if role:
                    result["resolved_id"] = role["id"]
            except Exception:
                pass  # Capturing evidence cannot change the real validation.
    elif type(value) in {bool, int, float}:
        if (type(value) in {bool, int} and abs(value) <= 100000 or
                type(value) is float and math.isfinite(value) and abs(value) <= 100000):
            result["value"] = value
        else:
            result["sha256"] = _digest(value)
    elif isinstance(value, (list, tuple)) and depth < 3:
        cap = min(16, max(0, budget[0])) if budget is not None else 16
        result.update(count=len(value), items=[_node(owner, x, key, depth+1, budget) for x in value[:cap]])
        if len(value) > cap:
            result["truncated"] = True
    elif isinstance(value, dict) and depth < 3:
        result.update(count=len(value), fields={name: _node(owner, value[name], name, depth+1, budget)
                      if name in value else {"presence": "omitted"} for name in sorted(NESTED)},
                      extra_key_count=sum(k not in NESTED for k in value))
    return result


def capture_arguments(owner, kwargs):
    budget = [64]
    return {key: _node(owner, kwargs[key], key, budget=budget) if key in kwargs else {"presence": "omitted"}
            for key in ARGUMENTS}


def _context(owner):
    prior = getattr(owner, "_guidance_context", None)
    prior = prior if isinstance(prior, dict) else {}
    age = time.monotonic() - prior["at"] if type(prior.get("at")) in {float, int} else None
    return {"discussion_generation": getattr(owner, "_discussion_generation", None),
            "kind": prior.get("kind"), "execution_intent": prior.get("execution_intent"),
            "request_focus": prior.get("request_focus"), "evidence_source": prior.get("evidence_source"),
            "age_seconds": age, "subject_ids": prior.get("character_ids")}


def request_id(owner):
    active = _active.get()
    return active[1]["request_id"] if active and active[0] is owner else None


def prepare_stage(stage, *, completed=False, snapshot=None, visual=None):
    active = _active.get()
    if not active or stage not in STAGES or not active[1].get("active"):
        return
    record = active[1]
    record["stage"] = stage
    if completed:
        record["last_completed_stage"] = stage
        if stage == "initialization":
            record["generation"] = getattr(active[0], "_answer_query_generation", None)
    if snapshot is not None and visual is not None:
        from .equipment import equipment_card, equipment_digest
        record["selection"] = {"epoch": snapshot.epoch, "serial": snapshot.serial}
        try:
            record["selection"].update(page_sha256=_digest(visual.get("page_type")), evidence_sha256=_digest(visual),
                                       equipment_sha256=equipment_digest(equipment_card(visual)))
        except Exception:
            # An optional diagnostic hash must never invalidate good evidence.
            record["selection_hash_available"] = False


def prepare_correction(corrections):
    active = _active.get()
    if not active:
        return
    record = active[1]
    rows = record.setdefault("corrections", [])
    for item in corrections if isinstance(corrections, (list, tuple)) else ():
        if isinstance(item, dict):
            rows.append({key: item[key] for key in (
                "reason", "original_scope", "original_subject_state",
                "corrected_scope", "corrected_subject_state", "intent") if key in item})


def prepare_failure(exc):
    active = _active.get()
    if not active:
        return
    record = active[1]
    code = "request_cancelled" if isinstance(exc, asyncio.CancelledError) else "request_timeout" if isinstance(exc, TimeoutError) else "internal_exception"
    try:
        message = str(exc)
        prefix = message.split(":", 1)[0]
        if prefix in ERROR_CODES:
            code = prefix
        for text, known in (("screen_fields 必须", "invalid_screen_fields"),
                            ("response_detail 必须", "invalid_response_detail"),
                            ("read_screen 必须", "missing_screen_fields"),
                            ("screen_fields 与", "screen_intent_conflict")):
            if message.startswith(text):
                code = known
    except Exception:
        pass
    record.update(outcome="cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
                  error_code=code, exception_type=type(exc).__name__, origin=[])
    tb = exc.__traceback__
    while tb:
        path = Path(tb.tb_frame.f_code.co_filename)
        if path.is_absolute() and path.is_relative_to(ROOT):
            record["origin"].append({"file": path.relative_to(ROOT).as_posix(), "line": tb.tb_lineno})
        tb = tb.tb_next
    record["origin"] = record["origin"][-6:]


def _public_node(node, depth=0, key="", budget=None):
    if not isinstance(node, dict) or depth > 3:
        return {"presence": "unknown"}
    if node.get("presence") == "omitted":
        return {"presence": "omitted"}
    result = {"presence": "provided", "type": node.get("type") if isinstance(node.get("type"), str) and node.get("type") in {
        "null", "str", "list", "tuple", "dict", "bool", "int", "float", "other"} else "other"}
    if budget is not None and budget[0] <= 0:
        return {**result, "truncated": True}
    if node.get("truncated") is True:
        result["truncated"] = True
    if budget is not None:
        budget[0] -= 1
    for size_name in ("length", "count", "extra_key_count"):
        if type(node.get(size_name)) is int and 0 <= node[size_name] <= 1000000:
            result[size_name] = node[size_name]
    if re.fullmatch(r"[a-f0-9]{64}", str(node.get("sha256"))):
        result["sha256"] = node["sha256"]
    value = node.get("value")
    if (key not in PRIVATE and isinstance(value, str) and (value in ENUMS or
            key in ROUTING_CODES and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value) or
            key == "request_id" and re.fullmatch(r"[a-f0-9]{32}", value)) or
            type(value) in {bool, int} and abs(value) <= 100000 or
            type(value) is float and math.isfinite(value) and abs(value) <= 100000):
        result["value"] = value
    if re.fullmatch(r"character\.[a-z0-9_.-]{1,80}", str(node.get("resolved_id"))):
        result["resolved_id"] = node["resolved_id"]
    if isinstance(node.get("items"), list):
        result["items"] = [_public_node(x, depth+1, key, budget) for x in node["items"][:16]]
    if isinstance(node.get("fields"), dict):
        result["fields"] = {k: _public_node(v, depth+1, k, budget) for k, v in node["fields"].items() if k in NESTED}
    return result


def public_records(records):
    """Validate again at the diagnostic/export boundary, including disk input."""
    result = []
    for row in (records if isinstance(records, list) else [])[-LIMIT:]:
        if not isinstance(row, dict):
            continue
        record = {k: row.get(k) if isinstance(row.get(k), str) and row.get(k) in allowed else None for k, allowed in (
            ("stage", STAGES), ("last_completed_stage", STAGES),
            ("outcome", {"returned", "complete", "incomplete", "failed", "cancelled"}),
            ("error_code", ERROR_CODES),
            ("persistence", {"memory_only", "saved", "unavailable", "busy"}),
            ("query_state", STATES), ("completion_state", STATES))}
        record["request_id"] = row.get("request_id") if re.fullmatch(r"[a-f0-9]{32}", str(row.get("request_id"))) else None
        exception = row.get("exception_type")
        record["exception_type"] = (exception if isinstance(exception, str) and (
            exception == "Exception" or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,56}(?:Error|Exception|Timeout)", exception))
            else "Exception" if exception is not None else None)
        record["exception_type_sha256"] = _digest(exception) if isinstance(exception, str) else None
        record["capture_boundary"] = "plugin_callable_kwargs_before_defaults"
        record["selection_hash_available"] = bool(row.get("selection")) and row.get("selection_hash_available") is not False
        record["generation"] = row.get("generation") if type(row.get("generation")) is int and row["generation"] >= 0 else None
        context = row.get("context") if isinstance(row.get("context"), dict) else {}
        record["context"] = {k: context.get(k) if isinstance(context.get(k), str) and context[k] in allowed else None
            for k, allowed in (("kind", {"build", "challenge"}), ("execution_intent", SEMANTIC_INTENTS),
                               ("request_focus", {"equipment", "attributes"}), ("evidence_source", {"saved_profile", "current_screen", "unresolved"}))}
        record["context"]["subject_ids"] = [x for x in context.get("subject_ids", [])[:4]
            if isinstance(x, str) and re.fullmatch(r"character\.[a-z0-9_.-]{1,80}", x)] if isinstance(context.get("subject_ids"), (list, tuple)) else []
        for name in ("discussion_generation", "age_seconds"):
            v = context.get(name)
            record["context"][name] = round(v, 3) if type(v) in {int, float} and 0 <= v < 1e9 else None
        stamp = row.get("started_at")
        try:
            parsed = datetime.fromisoformat(stamp)
            record["started_at"] = parsed.isoformat() if parsed.tzinfo else None
        except (TypeError, ValueError):
            record["started_at"] = None
        record["elapsed_ms"] = round(row["elapsed_ms"], 3) if type(row.get("elapsed_ms")) in {float, int} and 0 <= row["elapsed_ms"] < 1e9 else None
        budget = [64]
        record["arguments"] = {k: _public_node(v, key=k, budget=budget) for k, v in (row.get("arguments") or {}).items()
                               if k in ARGUMENTS} if isinstance(row.get("arguments"), dict) else {}
        record["followup_pending"] = row.get("followup_pending") is True
        record["missing_fields"] = [x for x in row.get("missing_fields", []) if isinstance(x, str) and x in SCREEN_FIELDS] if isinstance(row.get("missing_fields"), list) else []
        frames = row.get("origin") if isinstance(row.get("origin"), list) else []
        record["origin"] = [{"file": f["file"], "line": f["line"]} for f in frames[-6:]
            if isinstance(f, dict) and isinstance(f.get("file"), str) and re.fullmatch(r"(?:runtime/|tests/)?[a-zA-Z0-9_]+\.py", f["file"])
            and type(f.get("line")) is int and 0 < f["line"] < 100000]
        selection = row.get("selection") or {}
        record["selection"] = {k: v for k, v in selection.items() if (
            k in {"epoch", "serial"} and type(v) is int and v >= 0 or
            k in {"page_sha256", "evidence_sha256", "equipment_sha256"} and isinstance(v, str) and re.fullmatch(r"[a-f0-9]{64}", v))} if isinstance(selection, dict) else {}
        corrections = row.get("corrections") if isinstance(row.get("corrections"), list) else []
        record["corrections"] = [
            {key: item.get(key) if isinstance(item.get(key), str) and item.get(key) in allowed else None
             for key, allowed in (
                 ("reason", CORRECTION_REASONS),
                 ("original_scope", CURRENT_SCOPES),
                 ("original_subject_state", {"inherit", "specified", "unresolved", "current", "legacy"}),
                 ("corrected_scope", CURRENT_SCOPES),
                 ("corrected_subject_state", {"inherit", "specified", "unresolved", "current"}),
                 ("intent", ENUMS),
             )}
            for item in corrections[:4] if isinstance(item, dict)
        ]
        result.append(record)
    return result


def _warn(owner, record):
    logger = getattr(owner, "logger", None)
    if logger:
        try:
            logger.warning("HSR prepare failure request=%s stage=%s code=%s persistence=%s",
                           record["request_id"], record["stage"], record["error_code"], record["persistence"])
        except Exception:
            pass


async def _finish(owner, record, started):
    record.update(active=False, elapsed_ms=(time.monotonic()-started)*1000, persistence="memory_only")
    journal = getattr(owner, "_prepare_call_records", [])
    journal.append(record)
    owner._prepare_call_records = journal[-LIMIT:]
    if record["outcome"] != "failed":
        return
    if getattr(owner, "_prepare_trace_writing", False):
        record["persistence"] = "busy"
        _warn(owner, record)
        return

    async def save():
        from plugin.sdk.plugin import Err, unwrap_or
        stored = await owner._get_store(STORE_KEY)
        if isinstance(stored, Err):
            raise RuntimeError("diagnostic_storage_read_failed")
        old = public_records(unwrap_or(stored, []))
        # Disk presence on a later read proves this particular write happened;
        # in-memory status is only marked saved after the SDK acknowledges it.
        candidate = {**record, "persistence": "saved"}
        await owner._store_value(STORE_KEY, public_records([*old, candidate]))

    owner._prepare_trace_writing = True
    try:
        await asyncio.wait_for(save(), timeout=PERSIST_TIMEOUT)
        record["persistence"] = "saved"
    except Exception:
        record["persistence"] = "unavailable"
    finally:
        owner._prepare_trace_writing = False
    _warn(owner, record)


def trace_prepare(function):
    @wraps(function)
    async def wrapped(owner, *args, **kwargs):
        try:
            arguments = capture_arguments(owner, kwargs)
        except Exception:
            arguments = {}  # Keep original execution even if diagnostic capture fails.
        record = {"request_id": uuid4().hex, "started_at": datetime.now(timezone.utc).isoformat(),
                  "stage": "argument_binding", "last_completed_stage": "entry", "outcome": "returned",
                  "arguments": arguments, "context": _context(owner), "active": True}
        token = _active.set((owner, record))
        started = time.monotonic()
        result = None
        try:
            result = await function(owner, *args, **kwargs)
            if isinstance(result, dict):
                answer = result.get("query_answer") if isinstance(result.get("query_answer"), dict) else {}
                completion = result.get("request_completion") if isinstance(result.get("request_completion"), dict) else {}
                followup = result.get("followup") if isinstance(result.get("followup"), dict) else {}
                record.update(query_state=answer.get("state"), completion_state=completion.get("state"),
                    missing_fields=answer.get("missing_fields", []), followup_pending=followup.get("pending") is True)
                if result.get("is_error") and record["outcome"] != "failed":
                    record.update(outcome="failed", error_code="tool_error")
                elif record["outcome"] != "failed":
                    record["outcome"] = "incomplete" if record["missing_fields"] or completion.get("service_ready") is False else "complete"
            return result
        except BaseException as exc:
            prepare_failure(exc)
            raise
        finally:
            try:
                try:
                    await _finish(owner, record, started)
                except asyncio.CancelledError as exc:
                    prepare_failure(exc)
                    raise
                except Exception:
                    # Diagnostics must never replace the original result/error.
                    record["persistence"] = "unavailable"
                    _warn(owner, record)
                if isinstance(result, dict) and result.get("is_error") and isinstance(result.get("output"), dict):
                    result["output"]["failure_receipt"] = {k: v for k, v in public_records([record])[0].items()
                        if k in {"request_id", "stage", "last_completed_stage", "error_code", "exception_type", "persistence"}}
            finally:
                _active.reset(token)
    return wrapped


async def read_records(owner):
    """Read-only recovery. No recognition, messages, settings or task restart."""
    from plugin.sdk.plugin import Err, unwrap_or
    state = "available"
    try:
        value = await asyncio.wait_for(owner._get_store(STORE_KEY), timeout=PERSIST_TIMEOUT)
        if isinstance(value, Err):
            raise RuntimeError("diagnostic_storage_unavailable")
        disk = public_records(unwrap_or(value, []))
    except Exception:
        state, disk = "unavailable", []
    merged = {r["request_id"]: r for r in [*disk, *public_records(getattr(owner, "_prepare_call_records", []))]}
    records = sorted(merged.values(), key=lambda r: r.get("started_at") or "")[-LIMIT:]
    return {"storage_read": state, "records": records}
