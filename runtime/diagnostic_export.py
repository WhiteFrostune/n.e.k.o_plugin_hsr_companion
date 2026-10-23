"""Write only the server-produced, allowlisted diagnostic snapshot."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def export_diagnostic(directory: Path, report: dict) -> dict:
    text = report["copy_text"]
    if not isinstance(text, str) or len(text.encode("utf-8")) > 131072:
        raise ValueError("Invalid diagnostic snapshot")
    if not isinstance(json.loads(text), dict):
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
    return {"path": str(target), "filename": filename, "checked_at": report["checked_at"]}
