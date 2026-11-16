"""Prepare a public distribution from an immutable candidate; never install it."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "dist/release-0.9.6-20261023"
SOURCE = RUN / "source/hsr_companion"
ASSETS = RUN / "assets"
FROZEN = ROOT / "dist/spoken-focus-candidate-20261023-134935/candidate.json"
HELPER = ROOT / "dist/assistant-0.9.3-stage2a-20261017.7"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def scrub(text):
    for old, new in (
        ("C:/Users/cai", "C:/Users/USER"),
        (r"C:\\Users\\cai", r"C:\\Users\\USER"),
        (r"C:\Users\cai", r"C:\Users\USER"),
        ("浅霜", "玩家"), ("林悠怡", "猫娘"),
        ("111091722", "100000000"),
    ):
        text = text.replace(old, new)
    return text


def prepare():
    assert SOURCE.is_dir() and (SOURCE / ".git").is_dir()
    assert SOURCE.resolve().is_relative_to((ROOT / "dist").resolve())
    frozen = json.loads(FROZEN.read_text(encoding="utf-8"))
    baseline = FROZEN.parent / "hsr_companion"
    assert sha(Path(frozen["package"])) == frozen["package_sha256"]
    selected = {name for name in frozen["source_hashes"]
                if not name.startswith("docs/") or name == "docs/quickstart.md"}
    selected -= {"DESIGN.md", "TESTING_STATUS.md"}
    for name in selected:
        src = baseline / name
        assert sha(src) == frozen["source_hashes"][name], name
    tracked = subprocess.run(["git", "ls-files", "-z"], cwd=SOURCE, check=True,
                             capture_output=True).stdout.decode().split("\0")
    removed = []
    for name in filter(None, tracked):
        if name in selected or name.startswith(".github/"):
            continue
        target = (SOURCE / name).resolve()
        assert target.is_relative_to(SOURCE.resolve()) and target != SOURCE.resolve()
        if target.is_file():
            target.unlink()  # Recoverable tracked files in this isolated clone only.
            removed.append(name)
    sanitized = []
    for name in sorted(selected):
        src, dest = baseline / name, SOURCE / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        if dest.suffix in {".md", ".py", ".json", ".toml", ".tsx", ".txt", ".yml"}:
            text = dest.read_text(encoding="utf-8")
            clean = scrub(text)
            if clean != text:
                assert name.startswith("scripts/") or dest.suffix == ".md", name
                dest.write_text(clean, encoding="utf-8", newline="\n")
                sanitized.append(name)
    shutil.copy2(ROOT / "README_v0.9.6_PREVIEW.md", SOURCE / "README.md")
    for name in ("release.yml", "verify.yml"):
        dest = SOURCE / ".github/workflows" / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / ".github/workflows" / name, dest)
    ASSETS.mkdir(exist_ok=False)
    helper = json.loads((HELPER / "hsr-vision-assistant-manifest.json").read_text())
    assert helper["version"] == "0.9.3" and helper["isolated_window"] is True
    assert sha(HELPER / "hsr-vision-assistant-win-x64.zip") == helper["sha256"]
    for name, expected in helper["source_sha256"].items():
        assert sha(SOURCE / "assistant_runtime" / name) == expected
    shutil.copy2(HELPER / "hsr-vision-assistant-win-x64.zip", ASSETS)
    helper["archive_url"] = (
        "https://github.com/WhiteFrostune/n.e.k.o_plugin_hsr_companion/"
        "releases/download/v0.9.6/hsr-vision-assistant-win-x64.zip")
    (ASSETS / "hsr-vision-assistant-manifest.json").write_text(
        json.dumps(helper, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (RUN / "preparation.json").write_text(json.dumps({
        "baseline_build": frozen["build_revision"], "baseline_package_sha256": frozen["package_sha256"],
        "selected_files": sorted(selected), "sanitized_development_files": sanitized,
        "excluded_private_history": True, "removed_obsolete_tracked_files": removed,
        "helper_binary_rebuilt": False, "installed": False,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"prepared": str(SOURCE), "files": len(selected), "sanitized": sanitized}))


def build():
    from plugin.neko_plugin_cli.commands.validate_cmd import validate_plugin_dir
    from plugin.neko_plugin_cli.core import build_plugin, inspect_package

    build_tmp = RUN / "build-tmp"
    build_tmp.mkdir(exist_ok=True)
    tempfile.tempdir = str(build_tmp)
    for source_file in SOURCE.rglob("*.py"):
        ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
    issues = validate_plugin_dir(SOURCE)
    assert not any(level == "error" for level, _ in issues), issues
    frozen = json.loads(FROZEN.read_text(encoding="utf-8"))
    protected = [n for n in frozen["source_hashes"] if n.endswith(".py")
                 and not n.startswith(("tests/", "scripts/"))
                 and n not in {"assistant.py", "runtime/build_info.py"}]
    assert all(sha(SOURCE / n) == frozen["source_hashes"][n] for n in protected)
    before = (FROZEN.parent / "hsr_companion/assistant.py").read_text(encoding="utf-8")
    after = (SOURCE / "assistant.py").read_text(encoding="utf-8")
    assert after == before.replace("releases/download/v0.9.2/hsr-vision-assistant-manifest.json",
                                  "releases/download/v0.9.6/hsr-vision-assistant-manifest.json")
    package = ASSETS / "hsr_companion.neko-plugin"
    build_plugin(SOURCE, package)
    inspected = inspect_package(package)
    assert inspected.package_id == "hsr_companion" and inspected.payload_hash_verified
    forbidden = (b"111091722", "浅霜".encode(), "林悠怡".encode(),
                 b"C:/Users/cai", b"C:\\Users\\cai")
    with zipfile.ZipFile(package) as archive:
        names = archive.namelist()
        assert not any(any(p in n for p in ("/.git/", "/.pytest", "/dist/", "/tests/", "/scripts/"))
                       or n.endswith(("store.db", ".env", ".sqlite")) for n in names)
        assert not any(token in archive.read(n) for n in names for token in forbidden)
    evidence = {
        "schema_version": 1, "plugin_id": "hsr_companion", "version": "0.9.6",
        "build_revision": "release-0.9.6-20261023.1", "tag": "v0.9.6", "prerelease": True,
        "baseline_build": frozen["build_revision"], "baseline_package_sha256": frozen["package_sha256"],
        "protected_production_files_unchanged": len(protected), "helper_binary_rebuilt": False,
        "distribution_changes": ["approved README and public release status", "private history excluded",
                                 "development-only paths redacted", "versioned helper download URL",
                                 "release identity", "development files excluded from installed payload"],
        "validation": {"plugin_check_issues": issues, "payload_hash_verified": True,
                       "private_artifacts_check": "passed", "baseline_regressions": frozen["tests"],
                       "new_live_tests": False, "stable_runtime_acceptance": False},
        "market_submission": False, "installed": False,
        "source_commit": subprocess.run(["git", "rev-parse", "HEAD"], cwd=SOURCE, check=True,
                                        capture_output=True, text=True).stdout.strip(),
        "privacy_checks": {"personal_store_or_media_in_payload": False,
                           "known_personal_identifiers_in_payload": False,
                           "private_development_history_published": False},
        "assets": {p.name: {"sha256": sha(p), "size": p.stat().st_size}
                   for p in sorted(ASSETS.iterdir())
                   if p.name not in {"release-evidence.json", "SHA256SUMS.txt"}},
    }
    # The baseline report contains only its relative filename, hash and counts, not player input.
    (ASSETS / "release-evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
                                               encoding="utf-8")
    (ASSETS / "SHA256SUMS.txt").write_text("".join(
        f"{sha(p)}  {p.name}\n" for p in sorted(ASSETS.iterdir()) if p.name != "SHA256SUMS.txt"),
        encoding="utf-8")
    print(json.dumps({"assets": list(evidence["assets"]), "protected_files": len(protected),
                      "check_issues": issues, "payload_hash_verified": True}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["prepare", "build"])
    arguments = parser.parse_args()
    prepare() if arguments.phase == "prepare" else build()
