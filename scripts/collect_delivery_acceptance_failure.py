"""Read-only evidence for the 13:26 acceptance failure; no refresh or restart."""
from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from pathlib import Path

from .install_page_activity_candidate import request


def main():
    import ormsgpack

    root = Path(__file__).resolve().parents[1]
    output = root / "dist/delivery-boundary-acceptance-group1-20261020.json"
    if output.exists():
        raise ValueError("Never overwrite acceptance evidence")
    app = Path("C:/Users/cai/AppData/Local/N.E.K.O")
    report = {"production_changed": False, "installed_changed": False, "observer_paused": False,
              "test_messages_sent": False, "images_stored": False, "user_report": {
                  "question_at_local": "2026-10-20 13:26:18", "first_reply_at_local": "2026-10-20 13:26:32",
                  "reply_was_unconfirmed": True, "acknowledgment_at_local": "2026-10-20 13:26:57",
                  "page_switched": False, "camera_zoomed": True}}
    try:
        result = request("/plugin/hsr_companion/hosted-ui/action/diagnose_companion",
            {"args": {}, "kind": "panel", "surface_id": "demo"}, timeout=20)
        report["diagnostic"] = json.loads(result["result"]["diagnostic"]["copy_text"])
    except Exception as exc:
        report["diagnostic_read_error"] = type(exc).__name__
    database = app / "data-packs/hsr_companion/player-state/store.db"
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
        rows = db.execute("SELECT key,value FROM kv_store WHERE key IN (?,?)",
                          ("tool_audit", "last_local_vision")).fetchall()
    for key, value in rows:
        data = ormsgpack.unpackb(value)
        if key == "tool_audit":
            report["matching_tool_audit"] = [{k: row.get(k) for k in (
                "at", "tool", "intent", "query", "query_plan", "guidance_plan", "answer_status_at_audit", "refresh_diagnostics")}
                for row in data if isinstance(row, dict) and str(row.get("at", "")).startswith("2026-10-20T05:26:")]
        elif isinstance(data, dict):
            report["current_record_metadata_not_historical_frame"] = {
                "observed_at": data.get("observed_at"), "invalidated": data.get("invalidated"),
                "invalidated_reason": data.get("invalidated_reason"), "page": data.get("page"),
                "fact_counts": dict(Counter(f.get("scope") for f in (data.get("screen_facts") or {}).get("facts", [])))}
    text = (app / "logs/N.E.K.O_Plugin_20261020.log").read_text(encoding="utf-8", errors="replace")
    blocks = re.split(r"(?=^2026-10-20 \d\d:\d\d:\d\d - )", text, flags=re.MULTILINE)
    contexts = []
    for block in blocks:
        if not re.match(r"2026-10-20 13:2[56]:", block) or "[星铁当前观察" not in block.splitlines()[0]:
            continue
        contexts.append({"header": block.splitlines()[0][:250],
            "summary_lines": [line[:350] for line in block.splitlines()[1:]
                              if line.startswith(("识别时间：", "当前场景：", "当前已确认界面角色："))]})
    report["passive_context_summaries"] = contexts
    with output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps({"file": str(output), "audit": report.get("matching_tool_audit"),
        "contexts": contexts, "diagnostic_error": report.get("diagnostic_read_error")}, ensure_ascii=True))


if __name__ == "__main__":
    main()
