"""Package the immutable Stage 3 freeze; explicitly install HSR only with backup."""
from __future__ import annotations

import argparse
import json
import shutil
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from . import install_character_page_proof_candidate as transaction
from .install_page_activity_candidate import digest, tree
from .install_request_chain_candidate import stable_player_records

ROOT = Path(__file__).resolve().parents[1]
FREEZE = ROOT / "dist/stage3-candidate-20261022.json"
BUILD = "demo-recording-stage3-20261022.1"


def save(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def check_freeze():
    frozen = json.loads(FREEZE.read_text(encoding="utf-8"))
    assert frozen["build_revision"] == BUILD and frozen["plugin_version"] == "0.9.6"
    assert len(frozen["source_hashes"]) == 319
    for name, expected in frozen["source_hashes"].items():
        source = (ROOT / name).resolve(strict=True)
        assert source.is_relative_to(ROOT) and not name.startswith("dist/")
        assert digest(source) == expected, "Frozen source drift: " + name
    baseline_path = ROOT / frozen["baseline"]["file"]
    assert digest(baseline_path) == frozen["baseline"]["sha256"]
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))["source_hashes"]
    assert tree(transaction.INSTALLED) == baseline, "Installed baseline differs; inspect, do not overwrite"
    tests = frozen["tests"]
    report = ROOT / tests["file"]
    assert digest(report) == tests["sha256"]
    cases = list(ET.parse(report).getroot().iter("testcase"))
    assert len(cases) == tests["tests"] == 370
    assert not any(c.find(tag) is not None for c in cases for tag in ("failure", "error", "skipped"))
    for name, expected in frozen["evidence_sha256"].items():
        assert digest(ROOT / "dist" / name) == expected, "Evidence drift: " + name
    assert all(frozen["source_hashes"][name] == value for name, value in baseline.items()
               if name == "assistant.py" or name.startswith("assistant_runtime/"))
    return frozen, baseline


def prepare():
    from plugin.neko_plugin_cli.commands.validate_cmd import validate_plugin_dir
    from plugin.neko_plugin_cli.core import build_plugin, inspect_package

    frozen, baseline = check_freeze()
    run = ROOT / "dist" / ("stage3-install-candidate-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    run.mkdir()
    stage = run / "hsr_companion"
    for name in frozen["source_hashes"]:
        target = (stage / name).resolve()
        assert target.is_relative_to(stage)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
    assert tree(stage) == frozen["source_hashes"]
    issues = validate_plugin_dir(stage)
    assert not any(level == "error" for level, _ in issues), issues
    package = run / ("hsr_companion-0.9.6-" + BUILD + ".neko-plugin")
    build_plugin(stage, package)
    inspected = inspect_package(package)
    assert inspected.package_id == "hsr_companion" and inspected.package_type == "plugin"
    assert inspected.payload_hash_verified
    candidate = {"build_revision": BUILD, "plugin_version": "0.9.6", "installed": False,
        "package": str(package), "package_sha256": digest(package),
        "source_hashes": frozen["source_hashes"], "baseline_hashes": baseline,
        "freeze": str(FREEZE), "freeze_sha256": digest(FREEZE), "check": issues,
        "regression_tests": 370, "remaining_blockers": frozen["remaining_blockers"],
        "acceptance_limits": frozen["acceptance_limits"], "real_cat_validation": "pending"}
    save(run / "candidate.json", candidate)
    print(json.dumps({"candidate": str(run / "candidate.json"), "build": BUILD,
                      "files": len(frozen["source_hashes"]), "check": issues}), flush=True)


def install(candidate_path):
    candidate_path = candidate_path.resolve(strict=True)
    assert candidate_path.is_relative_to(ROOT / "dist")
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    frozen, baseline = check_freeze()
    assert candidate["freeze_sha256"] == digest(FREEZE)
    assert candidate["source_hashes"] == frozen["source_hashes"]
    assert candidate["baseline_hashes"] == baseline
    before = stable_player_records()
    save(candidate_path.parent / "stable-player-records-before.json", {"sha256": before})
    transaction.REVISION = BUILD
    transaction.install(candidate_path)
    after = stable_player_records()
    save(candidate_path.parent / "stable-player-records-after.json",
         {"before": before, "after": after, "unchanged": before == after})
    assert before == after, "Saved roster/profile/corrections changed"
    print(json.dumps({"installed": True, "build": BUILD, "stable_player_records_unchanged": True}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", type=Path)
    args = parser.parse_args()
    install(args.install) if args.install else prepare()
