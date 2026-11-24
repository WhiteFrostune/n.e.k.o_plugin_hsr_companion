"""Write only the server-produced, allowlisted diagnostic snapshot."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def export_diagnostic(directory: Path, report: dict) -> dict:
    snapshot = report.get("snapshot")
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != 2:
        raise ValueError("Invalid diagnostic snapshot")
    from .diagnostic_snapshot import canonical_text, summaries
    text = canonical_text(snapshot)
    copied = "\n".join([*summaries(snapshot), text])
    if (len(copied.encode("utf-8")) > 131072 or report.get("copy_text") != copied
            or report.get("diagnostic_id") != snapshot.get("diagnostic_id")
            or report.get("checked_at") != snapshot.get("checked_at")):
        raise ValueError("Invalid diagnostic snapshot")
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"hsr-diagnostic-{stamp}-{uuid4().hex}.json"
    target = directory / filename
    # Exclusive creation: exporting again never overwrites earlier evidence.
    with target.open("x", encoding="utf-8", newline="\n") as output:
        output.write(text)
        output.write("\n")
    return {"path": str(target), "filename": filename, "checked_at": report["checked_at"],
            "diagnostic_id": snapshot["diagnostic_id"]}
