"""Bounded public read-only receipts for ONE user-led recording trial.

No observer controls, tool triggers, chat sends, screenshots or config writes.
Only new HSR request receipts and matching explicit-followup bodies are saved.
This is not a model-consumption acknowledgement or a capability grader.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = "demo-page-body-r3-20261021.1"
BASE = "http://127.0.0.1:48916"
DIAG = "/plugin/hsr_companion/hosted-ui/action/diagnose_companion"
MESSAGES = "/plugin/messages?plugin_id=hsr_companion&max_count=200"


def get(path, *, timeout):
    if path not in {DIAG, MESSAGES}:
        raise ValueError("Read-only route allowlist")
    data = json.dumps({"args": {}, "kind": "panel", "surface_id": "demo"}).encode() if path == DIAG else None
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"})
    # Loopback only. Never inherit an external/system proxy for local reads.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as response:
        raw = json.load(response)
    return json.loads(raw["result"]["diagnostic"]["copy_text"]) if path == DIAG else raw


def epoch(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.timestamp() if result.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def new_request_ids(diag, baseline, started):
    result = set()
    service = diag.get("service_request") or {}
    for row in [service, *(diag.get("answer_deliveries") or [])]:
        request_id = row.get("request_id")
        at = epoch(row.get("started_at") or row.get("at"))
        if isinstance(request_id, str) and request_id not in baseline and at is not None and at >= started:
            result.add(request_id)
    return result


def matching_messages(messages, request_ids):
    selected = []
    for item in messages.get("messages") or []:
        meta = item.get("metadata") or {}
        if (item.get("plugin_id") != "hsr_companion" or meta.get("request_id") not in request_ids
                or meta.get("kind") != "query_followup"):
            continue
        content = item.get("content")
        text = content if isinstance(content, str) else None
        digest = hashlib.sha256(text.encode()).hexdigest() if text is not None else None
        selected.append({"message_id": item.get("message_id"), "timestamp": item.get("timestamp"),
                         "request_id": meta["request_id"], "origin": meta.get("origin"),
                         "projection": meta.get("message_projection"), "content_sha256": digest,
                         "content": text if text is not None and len(text) <= 6000 else None,
                         "exact_text_available": text is not None and len(text) <= 6000,
                         "ack_scope": "public_message_record_not_model_consumption"})
    return selected


def diagnostic_slice(diag, request_ids):
    return {"checked_at": diag.get("checked_at"),
            "service_request": diag.get("service_request"),
            "last_query": diag.get("last_query"),
            "explicit_followup": diag.get("explicit_followup"),
            "answer_deliveries": [row for row in diag.get("answer_deliveries", [])
                                  if row.get("request_id") in request_ids]}


def collect(output, seconds):
    if not 10 <= seconds <= 120:
        raise ValueError("One bounded 10-120 second collection; no automatic renewal")
    output = output.resolve()
    if not output.is_relative_to(ROOT / "dist") or output.exists():
        raise ValueError("Use a new plugin-owned report; never replace a previous attempt")
    initial = get(DIAG, timeout=10)
    if initial.get("build_revision") != BUILD:
        raise ValueError("Frozen runtime revision mismatch; do not silently switch baseline")
    baseline = {row.get("request_id") for row in initial.get("answer_deliveries", [])}
    baseline.add((initial.get("service_request") or {}).get("request_id"))
    started, end = time.time(), time.monotonic() + seconds
    report = {"stage": 2, "trial": "panel_warm_real_cat_01", "status": "collecting",
              "started_at": datetime.now(timezone.utc).isoformat(), "duration_limit_seconds": seconds,
              "build_revision": BUILD, "prepared_page_before_query": True,
              "not_a_cold_page_handoff_test": True,
              "initial_observer": initial.get("observer"), "initial_assistant": initial.get("assistant"),
              "request_ids": [], "samples": [], "messages": [], "read_errors": [],
              "not_done": ["production_changes", "installation", "observer_pause", "game_operation",
                           "chat_send", "screenshots", "trace_enablement", "model_consumption_claim"]}
    with output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps({"event": "ready", "file": str(output), "seconds": seconds}), flush=True)
    ids, sample_keys, message_keys = set(), set(), set()

    def save():
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    while time.monotonic() < end:
        try:
            remaining = end - time.monotonic()
            diag = get(DIAG, timeout=max(.1, min(5, remaining)))
            if diag.get("build_revision") != BUILD:
                report["status"] = "stopped_runtime_changed"
                break
            ids |= new_request_ids(diag, baseline, started)
            if ids:
                sample = diagnostic_slice(diag, ids)
                # Poll time alone is not a new execution event.
                key = json.dumps({k: v for k, v in sample.items() if k != "checked_at"}, sort_keys=True)
                if key not in sample_keys and len(report["samples"]) < 64:
                    sample_keys.add(key)
                    report["samples"].append(sample)
                    print(json.dumps({"event": "request_progress", "ids": sorted(ids),
                                      "followup": sample.get("explicit_followup")}), flush=True)
                remaining = end - time.monotonic()
                if remaining > .1:
                    raw = get(MESSAGES, timeout=min(5, remaining))
                    for message in matching_messages(raw, ids):
                        key = (message["message_id"], message["request_id"], message["content_sha256"])
                        if key not in message_keys and len(report["messages"]) < 8:
                            message_keys.add(key)
                            report["messages"].append(message)
            report["request_ids"] = sorted(ids)
        except Exception as exc:
            if len(report["read_errors"]) < 12:
                report["read_errors"].append({"at": datetime.now(timezone.utc).isoformat(),
                                               "type": type(exc).__name__})
        save()
        time.sleep(max(0, min(2, end - time.monotonic())))
    if report["status"] == "collecting":
        report["status"] = "capture_complete" if ids else "no_new_request_captured"
    report["ended_at"] = datetime.now(timezone.utc).isoformat()
    report["observed_cat_transcript"] = "pending_user_provided_output_not_inferred_from_SDK"
    save()
    print(json.dumps({"event": "finished", "status": report["status"], "requests": len(ids),
                      "exact_messages": sum(m["exact_text_available"] for m in report["messages"])}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds", type=int, default=100)
    args = parser.parse_args()
    collect(args.output, args.seconds)


if __name__ == "__main__":
    main()
