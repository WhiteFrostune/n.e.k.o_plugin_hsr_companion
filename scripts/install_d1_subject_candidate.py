"""User-authorized installation of the already frozen D1 plugin; no rebuild.

Only the host's normal single-plugin transaction writes the installation.
Back up first. Unknown outcomes stop without a blind retry.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from .install_page_activity_candidate import digest, player_records, request, tree


def main():
    from plugin.neko_plugin_cli.core import inspect_package

    root = Path(__file__).resolve().parents[1]
    workspace = root.parents[2]
    summary = json.loads((root / "dist/d1-subject-fit-result-20261020.json").read_text(encoding="utf-8"))
    freeze = json.loads((root / "dist" / summary["candidate_receipt"]).read_text(encoding="utf-8"))
    package = (root / "dist" / summary["package"]).resolve(strict=True)
    assert package.is_relative_to(root / "dist")
    revision = "demo-d1-subject-fit-20261020.1"
    assert summary["build_revision"] == freeze["build_revision"] == revision
    assert digest(package) == summary["package_sha256"] == freeze["package_sha256"]
    assert all(digest(root / name) == value for name, value in freeze["source_hashes"].items()), "tested source drift"
    for name, count in (("d1-subject-fit-frozen-20261020.xml", 321), ("d1-subject-fit-diagnostics-20261020.xml", 14)):
        suites = ET.parse(root / "dist" / name).getroot().findall("testsuite")
        assert sum(int(s.get("tests", 0)) for s in suites) == count
        assert not any(int(s.get(key, 0)) for s in suites for key in ("failures", "errors", "skipped"))
    inspected = inspect_package(package)
    assert inspected.package_id == "hsr_companion" and inspected.package_type == "plugin" and inspected.payload_hash_verified
    app = Path("C:/Users/cai/AppData/Local/N.E.K.O")
    installed = app / ".neko-plugin-installations/plugins/hsr_companion"
    profile = app / ".neko-package-profiles/hsr_companion"
    legacy = app / "plugins/hsr_companion/config"
    resources = app / "data-packs/hsr_companion"
    helper_pointer = resources / "assistant/current.json"
    helper = (helper_pointer.parent / json.loads(helper_pointer.read_text(encoding="utf-8"))["executable"]).resolve(strict=True)
    game_pointer = resources / "current.json"
    game = (resources / json.loads(game_pointer.read_text(encoding="utf-8"))["database"]).resolve(strict=True)
    store = resources / "player-state/store.db"
    assert helper.is_relative_to(resources / "assistant") and game.is_relative_to(resources)
    run = root / "dist" / ("d1-subject-fit-install-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    run.mkdir()
    report = {"build_revision": revision, "plugin_version": "0.9.6", "installed": False,
              "package": str(package), "package_sha256": digest(package), "events": []}

    def save(name, value):
        (run / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

    def event(name, **details):
        report["events"].append({"stage": name, **details})
        save("receipt.json", report)
        print(json.dumps({"stage": name, **details}, ensure_ascii=True), flush=True)

    def protected():
        return {"profiles": tree(profile), "legacy_config": tree(legacy),
                "helper_pointer": digest(helper_pointer), "helper_executable": digest(helper),
                "game_pointer": digest(game_pointer), "game_database": digest(game), "player_records": player_records(store)}

    before = protected()
    core_names = ["main_logic/core/__init__.py", "plugin/server/application/plugin_cli/service.py",
                  "plugin/server/routes/plugin_cli.py", "plugin/sdk/plugin/__init__.py"]
    core_before = {name: digest(workspace / name) for name in core_names}
    initial = request("/plugin/hsr_companion/hosted-ui/context?kind=panel&id=demo", timeout=20).get("state") or {}
    report["observer_enabled_before"] = initial.get("observer", {}).get("enabled")
    backup = run / "backup"
    shutil.copytree(installed, backup / "plugin", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(profile, backup / "profiles")
    if legacy.exists():
        shutil.copytree(legacy, backup / "legacy-config")
    shutil.copy2(helper_pointer, backup / "assistant-current.json")
    with sqlite3.connect(store.as_uri() + "?mode=ro", uri=True) as src, sqlite3.connect(backup / "player-state.db") as dst:
        src.backup(dst)
    report["backup"] = str(backup)
    save("protected-before.json", before)
    event("backup_ready", files=len(tree(installed)))
    boundary = "hsr-frozen-d1-subject-local-upload"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{package.name}\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n").encode() + package.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    upload = request("/plugin-cli/upload", body, content_type="multipart/form-data; boundary=" + boundary)
    payload = {"package": upload["path"]}
    save("host-inspect.json", request("/plugin-cli/inspect", payload))
    plan = request("/plugin-cli/install-plan", payload)
    save("install-plan.json", {"upload": upload, "plan": plan})
    assert plan["plugin_id"] == plan["directory_name"] == "hsr_companion"
    assert plan["action"] in {"upgrade", "reinstall"} and plan["target_version"] == "0.9.6" and plan["confirmation_token"]
    assert not plan.get("legacy_plugin_ids"), "unexpected additional targets"
    event("install_plan_verified", action=plan["action"])
    try:
        result = request("/plugin-cli/install", {**payload, "confirm_upgrade": True,
            "confirmation_token": plan["confirmation_token"], "on_conflict": "fail"}, timeout=55)
    except Exception as exc:
        event("install_outcome_unknown", error_type=type(exc).__name__)
        raise
    save("host-install-result.json", result)
    targets = result["installed_plugins"]
    assert len(targets) == 1 and Path(targets[0]["target_dir"]).resolve() == installed.resolve()
    assert result["payload_hash_verified"] is True and result["restarted"] is True
    actual = tree(installed)
    assert actual == freeze["source_hashes"], "installed files differ from frozen candidate"
    after = protected()
    preserved = {name: after[name] == value for name, value in before.items()}
    assert all(preserved.values()), "protected resources changed"
    assert core_before == {name: digest(workspace / name) for name in core_names}
    report.update(installed=True, files_verified=len(actual), preserved=preserved,
                  host_core_changed=False, helper_updated=False, market_uploaded=False, real_chat_acceptance="pending")
    event("installation_files_verified", files=len(actual))
    for attempt in range(3):
        try:
            live = request("/plugin/hsr_companion/hosted-ui/action/diagnose_companion",
                {"args": {}, "kind": "panel", "surface_id": "demo"}, timeout=20)
            save("running-diagnostic.json", live)
            diagnostic = json.loads(live["result"]["diagnostic"]["copy_text"])
            if diagnostic.get("build_revision") == revision:
                report.update(running_revision_verified=True, observer=diagnostic.get("observer"),
                              helper=diagnostic.get("assistant"))
                event("runtime_revision_verified", revision=revision)
                break
        except Exception as exc:
            event("runtime_handshake_pending", attempt=attempt + 1, error_type=type(exc).__name__)
        time.sleep(2)
    save("receipt.json", report)
    if not report.get("running_revision_verified"):
        raise RuntimeError("Files installed; live revision not confirmed. Do not reinstall blindly.")
    print(str(run), flush=True)


if __name__ == "__main__":
    main()
