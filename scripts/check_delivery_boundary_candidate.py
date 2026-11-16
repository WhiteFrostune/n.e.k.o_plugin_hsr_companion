"""Copy current package files into a clean checker-only stage; never install."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", default="delivery-boundary-check-20261020")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9-]{1,80}", args.label):
        raise ValueError("Use a bounded stage label")
    root = Path(__file__).resolve().parents[1]
    stage = root / "dist" / args.label / "hsr_companion"
    report = root / "dist" / (args.label + ".json")
    if stage.exists() or report.exists():
        raise ValueError("Never overwrite a previous check")
    prefix = "payload/plugins/hsr_companion/"
    with zipfile.ZipFile(root / "dist/hsr_companion-0.9.6-demo-page-activity-20261020.1.neko-plugin") as archive:
        names = {n[len(prefix):] for n in archive.namelist() if n.startswith(prefix) and not n.endswith("/")}
    names.update({"runtime/delivery_boundary.py", "tests/test_delivery_boundary_20261020.py", "runtime/pending_reply.py",
                  "runtime/revocation_trace.py", "tests/test_page_first_protocol_20261020.py"})
    digests = {}
    for name in sorted(names):
        source, target = (root / name).resolve(strict=True), (stage / name).resolve()
        if not source.is_relative_to(root) or not target.is_relative_to(stage):
            raise ValueError("Stage escaped plugin workspace")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        digests[name] = hashlib.sha256(source.read_bytes()).hexdigest()
    checked = subprocess.run([sys.executable, "-m", "plugin.neko_plugin_cli", "check", str(stage)],
        capture_output=True, encoding="utf-8", errors="replace", timeout=90)
    with report.open("x", encoding="utf-8") as stream:
        json.dump({"stage": str(stage), "installed": False, "source_hashes": digests,
            "returncode": checked.returncode, "stdout": checked.stdout, "stderr": checked.stderr}, stream,
            ensure_ascii=False, indent=2)
    print(checked.stdout.encode("ascii", "backslashreplace").decode("ascii"))
    if checked.stderr:
        print(checked.stderr.encode("ascii", "backslashreplace").decode("ascii"))
    raise SystemExit(checked.returncode)


if __name__ == "__main__":
    main()
