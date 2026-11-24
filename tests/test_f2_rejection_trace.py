"""F2 records actual refusals; failing diagnostics never become business gates."""

from __future__ import annotations

import ast
import asyncio
import copy
import json
import subprocess
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from plugin.plugins.hsr_companion.runtime import current_regions as compact
from plugin.plugins.hsr_companion.runtime import diagnostic_session, followup_delivery, pending_handler, review_policy
from plugin.plugins.hsr_companion.runtime import rejection_trace as trace
from plugin.plugins.hsr_companion.runtime.diagnostic_export import export_diagnostic
from plugin.plugins.hsr_companion.runtime.diagnostics import build_diagnostic_report
from plugin.plugins.hsr_companion.runtime.query_plan import build_query_plan
from plugin.plugins.hsr_companion.tests.diagnostic_sections import section
from plugin.plugins.hsr_companion.tests.test_current_character_regions import owner_at_character, raw_character
from plugin.plugins.hsr_companion.tests.test_diagnostics import inputs

ID = "a" * 32
OLD = "b" * 32
ROOT = Path(__file__).resolve().parents[1]


def events(owner):
    return trace.snapshot(owner)["events"]


def test_ring_request_limits_and_preserved_first_refusal():
    owner = SimpleNamespace()
    for i in range(300):
        trace.emit(owner, "Q14", "compact", "reject", "no_result", request_id=f"{i:032x}")
    snap = trace.snapshot(owner)
    assert len(snap["events"]) == 256 and snap["overwritten"] == 44
    assert len(snap["requests"]) == 8 and snap["requests"][0]["request_id"] == f"{292:032x}"
    first = snap["requests"][-1]["first_rejection"]
    for i in range(300):
        trace.emit(owner, "F07", "full", "allow", "parsed", request_id=f"{i + 400:032x}")
    assert first["reason"] == "no_result"  # Detached snapshots cannot be mutated by eviction.


def test_coalescing_interleaved_checks_but_not_changed_outcomes():
    owner = SimpleNamespace()
    for _ in range(30):
        trace.emit(owner, "Q14", "compact", "reject", "no_result", request_id=ID)
        trace.emit(owner, "X05", "activity", "retry", "activity_retry", request_id=ID)
    assert len(events(owner)) == 2
    assert all(e["count"] == 30 and e["first_at"] <= e["last_at"] for e in events(owner))
    trace.emit(owner, "Q14", "compact", "allow", "submitted", request_id=ID)
    trace.emit(owner, "Q14", "compact", "reject", "no_result", request_id=ID)
    assert len(events(owner)) == 4


def test_unknown_privacy_and_snapshot_is_detached():
    owner = SimpleNamespace()
    secret = "PRIVATE_CHAT_OCR_NAME_UID_C:/secret_token"
    trace.emit(
        owner, secret, secret, secret, secret, request_id=secret, related_request_id=secret, duration_ms=float("nan")
    )
    snap = trace.snapshot(owner)
    assert secret not in json.dumps(snap)
    assert snap["unmapped_count"] == 4 and snap["events"][0]["reason"] == "unmapped_code"
    snap["events"].clear()
    assert len(events(owner)) == 1


def test_broken_recording_is_fail_open(monkeypatch):
    owner = SimpleNamespace()

    def broken(*a, **kw):
        raise RuntimeError("private")

    monkeypatch.setattr(trace, "_record", broken)
    trace.emit(owner, "Q14", "compact", "reject", "no_result")
    assert trace.snapshot(owner)["recording_errors"] == 1
    assert trace.snapshot(owner)["events"] == []


def test_auto_restore_and_repeated_connection_do_not_evict_early_stages():
    owner = SimpleNamespace()
    diagnostic_session.begin_auto_restore(owner)
    diagnostic_session.begin_auto_restore(owner)
    for _ in range(100):
        diagnostic_session.safe_note(owner, "assistant_connection", "started")
        diagnostic_session.safe_note(owner, "assistant_connection", "unavailable")
    s = diagnostic_session.snapshot(owner)
    assert len(s["attempts"]) == 1 and s["attempts"][0]["source"] == "auto_restore"
    rows = s["recent_events"]
    assert len(rows) == 5
    assert (
        next(r for r in rows if r["stage"] == "assistant_connection" and r["outcome"] == "unavailable")["count"] == 100
    )
    assert any(r["stage"] == "enable_request" for r in rows)
    manual = SimpleNamespace()
    diagnostic_session.begin_enable(manual)
    diagnostic_session.begin_auto_restore(manual)
    assert diagnostic_session.snapshot(manual)["attempts"][0]["source"] == "manual"


def test_schema2_unique_floors_rejections_copy_export_privacy_and_size(tmp_path):
    owner = SimpleNamespace()
    for i in range(256):
        trace.emit(owner, "Q14", "compact", "reject", "receipt_rejected", request_id=f"{i:032x}", duration_ms=i)
    values = inputs()
    values["rejection_trace"] = trace.snapshot(owner)
    report = build_diagnostic_report(**values)
    s = report["snapshot"]
    forbidden = {"checks", "last_query", "observation_health", "query_timings", "session", "prepare_calls"}
    assert not forbidden.intersection(report) and not forbidden.intersection(s)
    assert len(report["copy_text"].encode("utf-8")) <= 131072
    assert json.loads(report["copy_text"].split("\n", 5)[5]) == s
    assert len(section(s, "observation_health")) > 0
    floor = s["floors"][1]
    assert floor["metrics"]["rejection_trace"]["request_limit"] == 8
    assert any(e.get("branch_id") == "Q14" for e in floor["recent_events"])
    saved = export_diagnostic(tmp_path, report)
    assert json.loads(Path(saved["path"]).read_text(encoding="utf-8")) == s
    poisoned = {"events": [{"branch_id": "PRIVATE", "reason": "PRIVATE", "ocr": "PRIVATE", "request_id": "PRIVATE"}]}
    values["rejection_trace"] = poisoned
    assert "PRIVATE" not in build_diagnostic_report(**values)["copy_text"]


CASES = [
    ("unsupported", "Q01", "unsupported"),
    ("no_source", "Q02", "no_source"),
    ("prepare_not_live", "Q02", "not_live"),
    ("no_analysis", "Q02", "no_analysis"),
    ("wrong_page", "Q02", "wrong_page"),
    ("scene", "Q02", "scene_unverified"),
    ("worker_not_live", "Q02", "not_live"),
    ("worker_expired", "Q02", "deadline"),
    ("no_controls", "Q10", "no_controls"),
    ("no_frame", "Q10", "no_frame"),
    ("window", "Q10", "window_changed"),
    ("latest_not_live", "Q10", "not_live"),
    ("controls", "Q11", "controls_changed"),
    ("epoch", "Q12", "epoch_changed"),
    ("publication_not_live", "Q12", "not_live"),
    ("review_expired", "Q13", "deadline"),
    ("guards_not_live", "Q13", "not_live"),
    ("no_result", "Q14", "no_result"),
    ("superseded", "Q14", "superseded"),
    ("review_not_live", "Q14", "not_live"),
    ("receipt", "Q14", "receipt_rejected"),
    ("matches", "Q14", "snapshot_rejected"),
    ("allow", "Q14", "admitted"),
]


@pytest.mark.parametrize("case,branch,reason", CASES)
@pytest.mark.parametrize("broken", [False, True])
def test_actual_compact_guard_and_fault_equivalence(tmp_path, monkeypatch, case, branch, reason, broken):
    owner, _, _ = owner_at_character(tmp_path)
    plan = build_query_plan("", "read_screen", screen_fields=["characters"])
    raw = raw_character()
    deadline = time.monotonic() + 30
    count = 0

    async def capture(*a, **kw):
        nonlocal count
        count += 1
        if (case == "no_source" and count == 1) or (case == "no_frame" and count == 2):
            return None
        if case in {"prepare_not_live", "latest_not_live"} and count == (1 if case == "prepare_not_live" else 2):
            owner._observer_stop_requested = True
        return SimpleNamespace(
            frame_seq=count,
            timestamp=time.time(),
            image_bytes=b"same",
            game={"pid": 2 if case == "window" and count == 2 else 1},
        )

    @asynccontextmanager
    async def turn(*a):
        yield

    async def worker(o, reader, *, deadline, prepare):
        if await prepare() is None:
            return None
        if case == "worker_not_live":
            owner._observer_stop_requested = True
        if case == "worker_expired":
            monkeypatch.setattr(compact.time, "monotonic", lambda: deadline + 1)
        if case == "no_analysis":
            return None
        if case == "wrong_page":
            raw["page"]["page_type"] = "unknown"
        if case == "scene":
            raw["scene"]["verified"] = False
        if case == "no_controls":
            raw["recognition"]["scene_regions"] = []
        return raw

    async def publish(*a):
        if case == "epoch":
            owner._observation_epoch = 99
        if case == "publication_not_live":
            owner._observer_stop_requested = True

    def stamp(*a):
        if case == "review_expired":
            monkeypatch.setattr(compact.time, "monotonic", lambda: deadline + 1)
        if case == "guards_not_live":
            owner._observer_stop_requested = True
        return ()

    async def review(*a, **kw):
        if case == "review_not_live":
            owner._observer_stop_requested = True
        if case == "no_result":
            return None
        if case == "superseded":
            return compact._SUPERSEDED
        return {"page_type": "character_detail", "reuse": {}}

    owner._publish_scene_preview = publish
    monkeypatch.setattr(compact, "supports_current_character", lambda *a: case != "unsupported")
    monkeypatch.setattr(compact, "delivery_turn", turn)
    monkeypatch.setattr(compact, "fetch_recent_frame", capture)
    monkeypatch.setattr(compact, "run_query_worker", worker)
    monkeypatch.setattr(compact, "regions_changed", lambda *a: case == "controls")
    # Force pixel-check branch for the controls case only.
    if case == "controls":
        original = capture

        async def changed_capture(*a, **kw):
            f = await original(*a, **kw)
            f.image_bytes = str(count).encode()
            return f

        monkeypatch.setattr(compact, "fetch_recent_frame", changed_capture)
    monkeypatch.setattr(compact, "region_stamp", stamp)
    monkeypatch.setattr(compact, "frame_stamp", lambda *a: ())
    monkeypatch.setattr(compact, "review_snapshot", review)
    monkeypatch.setattr(compact, "checked_receipt", lambda *a, **kw: case != "receipt")
    monkeypatch.setattr(
        compact.ObservationSnapshot, "select", lambda *a, **kw: SimpleNamespace(matches=lambda *a: case != "matches")
    )
    monkeypatch.setattr(compact, "_page_guard", lambda *a: None)
    if broken:

        def fail(*a, **kw):
            raise ValueError("private recorder")

        monkeypatch.setattr(trace, "_record", fail)
    result = asyncio.run(compact.read_current_character(owner, plan, deadline))
    assert (result is not None) == (case == "allow")
    selected = [e for e in events(owner) if e["branch_id"] == branch]
    if broken:
        assert not selected
    else:
        assert selected[0]["reason"] == reason
        assert not [e for e in events(owner) if e["outcome"] == "reject" and case == "allow"]


@pytest.mark.parametrize(
    "reason",
    [
        "not_observable",
        "generation_changed",
        "fields_missing",
        "invalid_timestamp",
        "frame_stale",
        "snapshot_rejected",
        "allow",
    ],
)
@pytest.mark.parametrize("broken", [False, True])
def test_receipt_short_circuit_and_call_count(monkeypatch, reason, broken):
    owner = SimpleNamespace(_answer_query_generation=1)
    evidence = {
        "reuse": {
            "answer_generation": 0 if reason == "generation_changed" else 1,
            "validated_at": "bad" if reason == "invalid_timestamp" else datetime.now(timezone.utc).isoformat(),
            "reviewed_fields": [] if reason == "fields_missing" else ["characters"],
        }
    }
    plan = SimpleNamespace(fields={"characters"})
    monkeypatch.setattr(review_policy, "observable", lambda o: reason != "not_observable")
    if reason == "frame_stale":
        evidence["reuse"]["validated_at"] = "2000-01-01T00:00:00+00:00"
    from plugin.plugins.hsr_companion.runtime.observation_store import ObservationSnapshot

    calls = []

    def select(*a, **kw):
        calls.append("matches")
        return SimpleNamespace(matches=lambda *a: reason != "snapshot_rejected")

    monkeypatch.setattr(ObservationSnapshot, "select", select)
    if broken:

        def fail(*a, **kw):
            raise RuntimeError("private")

        monkeypatch.setattr(trace, "_record", fail)
    assert review_policy.checked_receipt(owner, evidence, plan=plan) == (reason == "allow")
    assert len(calls) == (1 if reason in {"allow", "snapshot_rejected"} else 0)
    assert [e["reason"] for e in events(owner)] == ([] if broken or reason == "allow" else [reason])


@pytest.mark.parametrize("payload,reason", [(None, "invalid_relation"), ({}, "request_not_current")])
def test_pending_refusals_with_old_and_new_request_ids(monkeypatch, payload, reason):
    owner = SimpleNamespace(_pending_answer=None)
    result = asyncio.run(pending_handler.handle_pending_reply(owner, payload))
    assert result["accepted"] is False and result["reason"] == reason
    assert events(owner)[0]["branch_id"] == "X01" and events(owner)[0]["reason"] == reason


@pytest.mark.parametrize(
    "case,reason",
    [
        ("exhausted", "retry_exhausted"),
        ("replaced", "request_replaced"),
        ("expired", "deadline"),
        ("held", "relation_hold"),
        ("unavailable", "activity_retry"),
        ("available", None),
    ],
)
def test_activity_retry_guards_do_not_change_budget(monkeypatch, case, reason):
    request = SimpleNamespace(
        request_id=ID,
        activity_budget={"attempts": followup_delivery.ACTIVITY_ATTEMPTS if case == "exhausted" else 0},
        expires_at=time.monotonic() + (-1 if case == "expired" else 10),
        target="private",
    )
    owner = SimpleNamespace(
        _pending_answer=None if case == "replaced" else request,
        _observer_status={"enabled": True, "state": "observing"},
        _target_character=lambda: "private",
    )
    if case == "held":
        owner._followup_relation_hold = ID

    async def activity(*a, **kw):
        return ("unavailable" if case == "unavailable" else "available"), None

    async def sleep(*a):
        pass

    monkeypatch.setattr(followup_delivery, "latest_user_activity", activity)
    monkeypatch.setattr(followup_delivery.asyncio, "sleep", sleep)
    result = asyncio.run(followup_delivery.checked_activity(owner, request, SimpleNamespace()))
    assert result[0] == {"replaced": "cancelled", "held": "awaiting_relation", "available": "available"}.get(
        case, "unavailable"
    )
    assert [e["reason"] for e in events(owner)] == ([] if reason is None else [reason])


@pytest.mark.parametrize("reason", ["user_cancelled", "new_request", "activity_unavailable_after_retries"])
def test_cancel_logs_without_replacing_original_status(reason):
    request = SimpleNamespace(request_id=ID)
    owner = SimpleNamespace(_pending_answer=request)
    asyncio.run(_cancel(owner, reason))
    assert owner._pending_answer is None
    assert owner._followup_status["reason"] == reason
    assert events(owner)[0]["branch_id"] == "X06" and events(owner)[0]["request_id"] == ID


async def _cancel(owner, reason):
    followup_delivery.cancel_followup(owner, reason)


@pytest.mark.parametrize(
    "file,names",
    [
        ("runtime/live_refresh.py", ["_guarded_analysis"]),
        ("runtime/query_reuse.py", ["review_snapshot"]),
        ("runtime/delivery_boundary.py", ["review"]),
        ("__init__.py", ["_observer_loop", "_apply_local_analysis"]),
    ],
)
def test_instrumentation_erasure_matches_f1_business_ast(file, names):
    """Every return/await/exception/condition is identical after passive calls erase."""
    baseline = subprocess.run(
        ["git", "show", f"02610e6:{file}"], cwd=ROOT, capture_output=True, check=True
    ).stdout.decode("utf-8-sig")
    before = ast.parse(baseline)
    after = ast.parse((ROOT / file).read_text(encoding="utf-8-sig"))

    class Strip(ast.NodeTransformer):
        def visit_Expr(self, node):
            if (
                isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id in {"rejection_emit", "emit"}
            ):
                return None
            return self.generic_visit(node)

        def visit_With(self, node):
            if any(
                isinstance(i.context_expr, ast.Call)
                and isinstance(i.context_expr.func, ast.Name)
                and i.context_expr.func.id == "rejection_context"
                for i in node.items
            ):
                return [self.visit(n) for n in node.body]
            return self.generic_visit(node)

        def visit_If(self, node):
            node = self.generic_visit(node)
            return node if node.body else None

        def visit_ImportFrom(self, node):
            return None if node.module and node.module.endswith("rejection_trace") else node

    for name in names:
        a = next(
            n for n in ast.walk(before) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
        )
        b = next(
            n for n in ast.walk(after) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
        )
        assert ast.dump(a, include_attributes=False) == ast.dump(Strip().visit(b), include_attributes=False)


@pytest.mark.parametrize(
    "case,reason",
    [
        ("unsupported", "unsupported_activity"),
        ("busy", "activity_busy"),
        ("err", "sdk_error"),
        ("error", "activity_error"),
        ("timeout", "timeout"),
        ("cancel", "cancelled"),
        ("ok", None),
    ],
)
def test_real_activity_reader_reports_safe_failure_without_changing_returns(monkeypatch, case, reason):
    from concurrent.futures import Future

    from plugin.plugins.hsr_companion.runtime import chat_activity
    from plugin.sdk.plugin import Err, SdkError

    owner = SimpleNamespace()

    def get(**kw):
        if case == "err":
            return Err(SdkError("PRIVATE"))
        if case == "error":
            raise ValueError("PRIVATE")
        return []

    bus = SimpleNamespace(memory=SimpleNamespace(get=get))
    if case == "unsupported":
        bus.memory = None
    if case == "busy":
        chat_activity._state_for(bus).inflight = Future()
    if case in {"timeout", "cancel"}:

        async def wait(*a, **kw):
            if case == "cancel":
                raise asyncio.CancelledError
            raise TimeoutError("PRIVATE")

        monkeypatch.setattr(chat_activity.asyncio, "wait_for", wait)

    async def run():
        with trace.context(owner, ID):
            return await chat_activity.latest_user_activity(bus, target="PRIVATE")

    if case == "cancel":
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(run())
    else:
        assert asyncio.run(run())[0] == (
            "available" if case == "ok" else "unsupported" if case == "unsupported" else "unavailable"
        )
    assert [e["reason"] for e in events(owner)] == ([] if reason is None else [reason])
    assert "PRIVATE" not in json.dumps(trace.snapshot(owner))


@pytest.mark.parametrize(
    "case,reason",
    [
        ("invalid", "invalid_relation"),
        ("activity", "activity_unavailable"),
        ("message", "message_not_current"),
        ("confirm", "confirm"),
        ("cancel", "cancel"),
        ("new_request", "new_request"),
        ("uncertain", "uncertain"),
    ],
)
@pytest.mark.parametrize("broken", [False, True])
def test_real_pending_relations_and_old_new_id_binding(monkeypatch, case, reason, broken):
    from plugin.plugins.hsr_companion.runtime import chat_activity
    from plugin.plugins.hsr_companion.runtime.pending_contract import UserActivity, message_digest

    request = SimpleNamespace(request_id=OLD, expires_at=time.monotonic() + 30, target="PRIVATE", asked_at=0)
    owner = SimpleNamespace(_pending_answer=request, _target_character=lambda: "PRIVATE", bus=object())
    relation = case if case in {"confirm", "cancel", "new_request", "uncertain"} else "confirm"
    payload = {"request_id": OLD, "relation": "invalid" if case == "invalid" else relation, "player_message": "PRIVATE"}

    async def activity(*a, **kw):
        if case == "activity":
            return "unsupported", None
        return "available", UserActivity(1, "bad" if case == "message" else message_digest("PRIVATE"))

    monkeypatch.setattr(chat_activity, "latest_user_activity", activity)
    if broken:

        def fail(*a, **kw):
            raise ValueError("private")

        monkeypatch.setattr(trace, "_record", fail)
    with trace.context(owner, ID):
        result = asyncio.run(pending_handler.handle_pending_reply(owner, payload))
    assert result["accepted"] == (case in {"confirm", "cancel", "new_request", "uncertain"})
    rows = [e for e in events(owner) if e["branch_id"] == "X01"]
    if broken:
        assert not rows
    else:
        assert rows[-1]["reason"] == reason
        assert rows[-1]["request_id"] == ID and rows[-1]["related_request_id"] == OLD
    assert "PRIVATE" not in json.dumps(trace.snapshot(owner))


@pytest.mark.parametrize(
    "reason",
    [
        "epoch_changed",
        "new_question",
        "capture_failed",
        "observer_unavailable",
        "source_unconfirmed",
        "pending_unproved",
        "page_changed",
        "revision_unproved",
        "unscoped_revision",
        "page_proof_missing",
        "page_control_challenged",
        "allow",
    ],
)
def test_real_boundary_refusals_match_branch_ids(monkeypatch, reason):
    from plugin.plugins.hsr_companion.runtime import delivery_boundary as boundary

    owner = SimpleNamespace(
        _observation_epoch=1,
        _observation_serial=1,
        _answer_query_generation=1,
        _capture_failure_revision=0,
        _observer_status={"enabled": True, "state": "observing"},
    )
    evidence = {
        "fresh": reason != "source_unconfirmed",
        "page_type": "character_detail",
        "screen_facts": {"facts": [], "visible_text": []},
        "characters": [],
        "reuse": {},
    }
    snap = SimpleNamespace(
        epoch=1,
        serial=1,
        _fields=frozenset({"page"}),
        _generation=1,
        _capture_revision=0,
        _equipment_key=None,
        evidence=lambda: copy.deepcopy(evidence),
    )
    if reason == "epoch_changed":
        owner._observation_epoch = 2
    if reason == "new_question":
        owner._answer_query_generation = 2
    if reason == "capture_failed":
        owner._capture_failure_revision = 1
    if reason == "observer_unavailable":
        owner._observer_status["enabled"] = False
    if reason == "pending_unproved":
        owner._observation_pending_reason = "changed"
    if reason in {"revision_unproved", "unscoped_revision"}:
        owner._observation_serial = 2
    if reason == "unscoped_revision":
        snap._fields = frozenset()
        owner._committed_observation = {"page": {"page_type": "character_detail"}}
        monkeypatch.setattr(boundary, "page_key", lambda e: "same")
    if reason == "page_changed":
        owner._committed_observation = {"page": {"page_type": "relic_detail"}}
    if reason in {"page_proof_missing", "page_control_challenged"}:
        evidence["reuse"].update(validated_revision=1, epoch=1, reviewed_coordinates=[])
        if reason == "page_proof_missing":
            evidence["reuse"]["page_control_coordinates"] = []
        else:
            owner._deferred_verification = (1, 1, {(1, 2, 3, 4)})
            owner._accepted_frame_page_regions = [{"region": {"left": 1, "top": 2, "right": 3, "bottom": 4}}]
    result = boundary.review(snap, owner)
    assert result["delivery_boundary"]["state"] == ("retained" if reason == "allow" else "rejected")
    if reason == "allow":
        assert not events(owner)
    else:
        assert events(owner)[0]["reason"] == reason
        assert events(owner)[0]["branch_id"] == (
            "B02"
            if reason
            in {
                "page_changed",
                "revision_unproved",
                "unscoped_revision",
                "page_proof_missing",
                "page_control_challenged",
            }
            else "B01"
        )


@pytest.mark.parametrize(
    "fields,decision,reason",
    [
        ({"attributes"}, {"state": "partial", "withheld_fields": ["characters"]}, "identity_withheld"),
        ({"characters"}, {"state": "rejected", "withheld_fields": []}, "fields_withheld"),
        ({"characters"}, {"state": "partial", "withheld_fields": ["characters"]}, "fields_withheld"),
        ({"page"}, {"state": "partial", "withheld_fields": ["characters"]}, None),
    ],
)
@pytest.mark.parametrize("broken", [False, True])
def test_matches_uses_one_existing_boundary_decision(monkeypatch, fields, decision, reason, broken):
    from plugin.plugins.hsr_companion.runtime.observation_store import ObservationSnapshot

    calls = []

    def delivery(owner):
        calls.append(1)
        return {"delivery_boundary": decision}

    snap = SimpleNamespace(_fields=fields, delivery=delivery)
    owner = SimpleNamespace()
    if broken:

        def fail(*a, **kw):
            raise ValueError("private")

        monkeypatch.setattr(trace, "_record", fail)
    assert ObservationSnapshot.matches(snap, owner) == (reason is None)
    assert calls == [1]
    assert [e["reason"] for e in events(owner)] == ([] if broken or reason is None else [reason])


@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
def test_auto_restore_records_initialization_at_actual_exit(outcome):
    owner = SimpleNamespace()

    @diagnostic_session.observe_restore
    async def startup(o):
        diagnostic_session.begin_auto_restore(o)
        before = diagnostic_session.snapshot(o)["attempts"][0]["stages"]
        assert next(r for r in before if r["stage"] == "initialization")["outcome"] == "started"
        if outcome == "error":
            raise ValueError("private")
        if outcome == "cancel":
            raise asyncio.CancelledError()
        return 42

    if outcome == "error":
        with pytest.raises(ValueError):
            asyncio.run(startup(owner))
    elif outcome == "cancel":
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(startup(owner))
    else:
        assert asyncio.run(startup(owner)) == 42
    stage = next(
        r for r in diagnostic_session.snapshot(owner)["attempts"][0]["stages"] if r["stage"] == "initialization"
    )
    assert stage["outcome"] == {"success": "completed", "error": "failed", "cancel": "cancelled"}[outcome]


@pytest.mark.parametrize(
    "reason",
    [
        "no_plan",
        "not_observable",
        "preview_pending",
        "answer_generation_missing",
        "refresh_generation_missing",
        "source_unconfirmed",
        "raw_revision_changed",
        "packet_revision_changed",
        "proof_epoch_changed",
        "proof_revision_changed",
        "capture_failed",
        "refresh_generation_changed",
        "generation_changed",
        "invalid_timestamp",
        "frame_stale",
        "pending_unproved",
        "missing_proof",
        "deferred",
        "snapshot_rejected",
        "allow",
    ],
)
def test_producer_receipt_each_short_circuit_guard(monkeypatch, reason):
    from plugin.plugins.hsr_companion.runtime import delivery_scope
    from plugin.plugins.hsr_companion.runtime.observation_store import ObservationSnapshot

    owner = SimpleNamespace(
        _observer_status={"enabled": True, "state": "observing"},
        _answer_query_generation=1,
        _query_refresh_generation=1,
        _observation_epoch=1,
        _observation_serial=1,
        _capture_failure_revision=0,
        _committed_observation={"observation_serial": 1},
    )
    evidence = {"fresh": True, "screen_facts": {"observation_serial": 1}}
    proof = {
        "epoch": 1,
        "serial": 1,
        "capture_failure_revision": 0,
        "query_generation": 1,
        "answer_generation": 1,
        "checked_frame_at": time.time(),
    }
    owner._producer_validation = proof
    if reason == "not_observable":
        owner._observer_status["enabled"] = False
    if reason == "preview_pending":
        owner._scene_preview = {"present": True}
    if reason == "answer_generation_missing":
        owner._answer_query_generation = 0
    if reason == "refresh_generation_missing":
        owner._query_refresh_generation = 0
    if reason == "source_unconfirmed":
        evidence["fresh"] = False
    if reason == "raw_revision_changed":
        owner._committed_observation["observation_serial"] = 0
    if reason == "packet_revision_changed":
        evidence["screen_facts"]["observation_serial"] = 0
    for code, key in [
        ("proof_epoch_changed", "epoch"),
        ("proof_revision_changed", "serial"),
        ("capture_failed", "capture_failure_revision"),
        ("refresh_generation_changed", "query_generation"),
        ("generation_changed", "answer_generation"),
    ]:
        if reason == code:
            proof[key] = -1
    if reason == "invalid_timestamp":
        proof["checked_frame_at"] = None
    if reason == "frame_stale":
        proof["checked_frame_at"] -= 3
    if reason == "pending_unproved":
        owner._observation_pending_reason = "changed"
    coord = (0.1, 0.1, 0.2, 0.2)
    if reason == "deferred":
        owner._deferred_verification = (1, 1, {coord})

    def scope(*a, **kw):
        return None if reason == "missing_proof" else (copy.deepcopy(evidence), ([], [(coord, b"")]))

    monkeypatch.setattr(delivery_scope, "select_scoped_guard", scope)
    count = []

    def select(*a, **kw):
        count.append(1)
        return SimpleNamespace(matches=lambda *a: reason != "snapshot_rejected")

    monkeypatch.setattr(ObservationSnapshot, "select", select)
    result = review_policy.producer_receipt(
        owner, evidence, plan=None if reason == "no_plan" else SimpleNamespace(fields={"page"})
    )
    assert (result is not None) == (reason == "allow")
    assert len(count) == (1 if reason in {"allow", "snapshot_rejected"} else 0)
    assert [e["reason"] for e in events(owner)] == ([] if reason == "allow" else [reason])


@pytest.mark.parametrize(
    "case,branch,reason",
    [("no_analysis", "F12", "no_analysis"), ("health", "F12", "capture_failed"), ("late", "F13", "late_result")],
)
def test_observer_refusal_before_wait_or_cancellation(tmp_path, monkeypatch, case, branch, reason):
    import importlib

    from plugin.plugins.hsr_companion.tests.test_current_observation import plugin_fixture
    from plugin.plugins.hsr_companion.tests.test_query_reuse import picture
    from plugin.plugins.hsr_companion.tests.test_resident_observer import configure_loop, health, sample

    root = importlib.import_module("plugin.plugins.hsr_companion")
    owner, _, _ = plugin_fixture(tmp_path)
    completed = [False]
    clock = [time.monotonic()]

    def healthy():
        return {"connected": False} if completed[0] and case == "health" else health()

    configure_loop(owner, SimpleNamespace(health=healthy, fetch_frame=lambda seq: sample(picture())))

    async def analysis(*a, **kw):
        completed[0] = True
        owner._observer_stop_requested = True
        if case == "late":
            clock[0] += 31
        return None if case == "no_analysis" else {"characters": [], "screen_facts": {"facts": []}}

    async def wait(*a):
        raise asyncio.CancelledError()

    monkeypatch.setattr(root, "guarded_analysis", analysis)
    monkeypatch.setattr(root, "time", SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
    owner._wait_observer_tick = wait
    try:
        asyncio.run(owner._observer_loop())
    except asyncio.CancelledError:
        pass
    assert any(e["branch_id"] == branch and e["reason"] == reason for e in events(owner))


@pytest.mark.parametrize(
    "case,bid,reason", [("global", "V13", "pixels_changed"), ("advanced", "V10", "proof_rejected")]
)
def test_cache_final_rejects_have_specific_branch(tmp_path, monkeypatch, case, bid, reason):
    from plugin.plugins.hsr_companion.runtime import live_refresh
    from plugin.plugins.hsr_companion.runtime import query_reuse as cache
    from plugin.plugins.hsr_companion.tests.test_current_observation import plugin_fixture

    owner, _, _ = plugin_fixture(tmp_path)
    owner._assistant_client = object()
    evidence = {
        "fresh": True,
        "page_type": "character_detail",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "screen_facts": {"facts": []},
    }
    snapshot = {
        "evidence": evidence,
        "guard": ([{"region": {"left": 0.1, "top": 0.1, "right": 0.2, "bottom": 0.2}}], [((0.1, 0.1, 0.2, 0.2), b"x")]),
        "epoch": getattr(owner, "_observation_epoch", 0),
        "game_pid": 1,
        "stamp": (),
        "detached_region": True,
    }
    frame = SimpleNamespace(
        timestamp=time.time(),
        game={"pid": 1, "running": True, "minimized": False},
        capture={"state": "capturing", "isolated_window": True},
        image_bytes=b"safe",
    )

    async def capture(*a, **kw):
        return frame

    async def verify(*a, **kw):
        return SimpleNamespace(capture=frame, confirmed_coordinates=set())

    calls = []

    def regions(*a):
        calls.append(1)
        return [((0.1, 0.1, 0.2, 0.2), b"y")] if len(calls) == 1 else []

    monkeypatch.setattr(live_refresh, "fetch_recent_frame", capture)
    monkeypatch.setattr(live_refresh, "compare_live_frames", lambda *a, **kw: case == "global")
    monkeypatch.setattr(live_refresh, "frame_stamp", lambda *a: ())
    monkeypatch.setattr(live_refresh, "revalidate_guard_text", verify)
    monkeypatch.setattr(cache, "region_stamp", regions)
    monkeypatch.setattr(cache, "regions_changed", lambda *a: True)
    result = asyncio.run(cache.review_snapshot(owner, snapshot, full=True))
    assert result is None
    assert any(e["branch_id"] == bid and e["reason"] == reason for e in events(owner))


@pytest.mark.parametrize("case,reason", [("paused", "observer_unavailable"), ("target", "target_changed")])
def test_activity_cancellation_cause_is_not_called_request_replacement(case, reason):
    request = SimpleNamespace(request_id=ID, activity_budget={}, expires_at=time.monotonic() + 5, target="private")
    owner = SimpleNamespace(
        _pending_answer=request,
        _observer_status={"enabled": case != "paused", "state": "observing"},
        _target_character=lambda: "different",
    )
    assert asyncio.run(followup_delivery.checked_activity(owner, request, object())) == ("cancelled", None)
    assert events(owner)[0]["reason"] == reason
    assert owner._pending_answer is request


def test_cache_missing_and_invalidated_are_distinct(tmp_path):
    from plugin.plugins.hsr_companion.runtime import query_reuse
    from plugin.plugins.hsr_companion.tests.test_current_observation import plugin_fixture

    owner, _, _ = plugin_fixture(tmp_path)
    assert asyncio.run(query_reuse.review_snapshot(owner, None)) is None
    owner._observation_invalidated = True
    assert asyncio.run(query_reuse.review_snapshot(owner, {"present": True})) is None
    assert [e["reason"] for e in events(owner)] == ["candidate_missing", "observation_invalidated"]
