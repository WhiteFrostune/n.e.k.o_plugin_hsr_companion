"""Validate and package the frozen spoken-focus candidate; NEVER install it."""
from __future__ import annotations

import json
import shutil
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from .install_page_activity_candidate import digest, tree

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "dist/prepare-trace-candidate-20261023.json"
INSTALLED = Path("C:/Users/cai/AppData/Local/N.E.K.O/.neko-plugin-installations/plugins/hsr_companion")
BUILD = "demo-spoken-focus-20261023.1"
REPORT = ROOT / "dist/spoken-demand-final3-20261023.xml"
ALLOWED = {"__init__.py", "runtime/consultation.py", "runtime/consultation_contract.py",
           "runtime/semantic_guidance.py", "runtime/service_contract.py", "runtime/grounding.py",
           "runtime/build_info.py", "tests/test_reply_delivery_closeout_20261022.py",
           "tests/test_d1_handoff_closeout.py", "DESIGN.md"}
ADDED = {"tests/test_spoken_demand_20261023.py", "docs/TEST_RUN_SPOKEN_FOCUS_20261023.md"}


def main():
    from plugin.neko_plugin_cli.commands.validate_cmd import validate_plugin_dir
    from plugin.neko_plugin_cli.core import build_plugin, inspect_package

    baseline = json.loads(BASE.read_text(encoding="utf-8"))["source_hashes"]
    assert len(baseline) == 329 and tree(INSTALLED) == baseline, "Installed baseline drift; do not replace"
    cases = list(ET.parse(REPORT).getroot().iter("testcase"))
    assert len(cases) == 539
    assert not any(c.find(tag) is not None for c in cases for tag in ("failure", "error", "skipped"))
    names = set(baseline) | ADDED
    hashes = {}
    for name in sorted(names):
        source = (ROOT / name).resolve(strict=True)
        assert source.is_relative_to(ROOT) and not name.startswith("dist/")
        hashes[name] = digest(source)
    changed = {n for n, expected in baseline.items() if hashes[n] != expected}
    assert changed <= ALLOWED, "Unexpected source change: " + repr(changed - ALLOWED)
    assert all(hashes[n] == value for n, value in baseline.items()
               if n == "assistant.py" or n.startswith("assistant_runtime/"))
    assert BUILD in (ROOT / "runtime/build_info.py").read_text(encoding="utf-8")
    run = ROOT / "dist" / ("spoken-focus-candidate-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    run.mkdir()
    stage = run / "hsr_companion"
    for name in sorted(names):
        target = (stage / name).resolve()
        assert target.is_relative_to(stage)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
    assert tree(stage) == hashes
    issues = validate_plugin_dir(stage)
    assert not any(level == "error" for level, _ in issues), issues
    package = run / ("hsr_companion-0.9.6-" + BUILD + ".neko-plugin")
    build_plugin(stage, package)
    inspected = inspect_package(package)
    assert inspected.package_id == "hsr_companion" and inspected.package_type == "plugin"
    assert inspected.payload_hash_verified
    assert tree(INSTALLED) == baseline and all(digest(ROOT / n) == value for n, value in hashes.items())
    evidence = ["spoken-demand-red-20261023.xml", "spoken-demand-draft2-20261023.xml",
                "spoken-demand-final1-20261023.xml", "spoken-demand-final2-20261023.xml",
                "spoken-demand-final3-20261023.xml", "spoken-projection-red-20261023.xml",
                "spoken-focus-benchmark-20261023.json"]
    if (ROOT / "dist/spoken-grounding-red-20261023.xml").exists():
        evidence.append("spoken-grounding-red-20261023.xml")
    receipt = {"schema_version": 1, "plugin_version": "0.9.6", "build_revision": BUILD,
        "frozen_at": datetime.now(timezone.utc).isoformat(), "installed": False,
        "scope": "existing_demand_normalization_reply_plan_and_model_projection_only",
        "source_file_count": len(hashes), "source_hashes": hashes, "baseline_hashes": baseline,
        "changed_baseline_files": sorted(changed), "new_files": sorted(ADDED),
        "package": str(package), "package_sha256": digest(package), "check": issues,
        "tests": {"file": REPORT.relative_to(ROOT).as_posix(), "tests": len(cases),
                  "errors": 0, "failures": 0, "skipped": 0, "sha256": digest(REPORT)},
        "evidence_sha256": {n: digest(ROOT / "dist" / n) for n in evidence},
        "installed_files_unchanged": True, "ocr_or_helper_changed": False,
        "real_cat_validation": "pending",
        "acceptance_limits": ["No new OCR/model call; local benchmark does not measure real generation latency",
            "Full raw evidence retained; model may still elaborate, so real speech quality remains unverified",
            "A model can still explicitly choose full detail incorrectly; no second NLP classifier was added",
            "12:04 cause remains unproven; 12:59 subject_conflict and automatic retry are separate observed evidence",
            "Inherited nine stale assertions outside the fixed gate remain recorded, not silently counted as passes"]}
    with (run / "candidate.json").open("x", encoding="utf-8") as stream:
        json.dump(receipt, stream, ensure_ascii=False, indent=2)
    print(json.dumps({"candidate": str(run / "candidate.json"), "package": str(package),
                      "files": len(hashes), "tests": len(cases), "changed": sorted(changed),
                      "check": issues, "installed": False}), flush=True)


if __name__ == "__main__":
    main()
