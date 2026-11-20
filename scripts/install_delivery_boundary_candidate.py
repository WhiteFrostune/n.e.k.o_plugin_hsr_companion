"""Authorized HSR-only local update, with backup and tested-source checks."""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from .install_page_activity_candidate import digest, player_records, request, tree


def main():
    from plugin.neko_plugin_cli.core import build_plugin, inspect_package

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", choices=["delivery-boundary", "page-first"], default="delivery-boundary")
    parser.add_argument("--retry-known-rollback", type=Path,
                        help="Prior plugin dist run, only after confirming completed rollback and restoring old runtime")
    args = parser.parse_args()
    page_first = args.candidate == "page-first"
    root = Path(__file__).resolve().parents[1]
    workspace = root.parents[2]
    app = Path("C:/Users/cai/AppData/Local/N.E.K.O")
    installed = app / ".neko-plugin-installations/plugins/hsr_companion"
    profile = app / ".neko-package-profiles/hsr_companion"
    legacy = app / "plugins/hsr_companion/config"
    resources = app / "data-packs/hsr_companion"
    pointer = resources / "assistant/current.json"
    helper = (pointer.parent / json.loads(pointer.read_text(encoding="utf-8"))["executable"]).resolve(strict=True)
    game_pointer = resources / "current.json"
    game = (resources / json.loads(game_pointer.read_text(encoding="utf-8"))["database"]).resolve(strict=True)
    store = resources / "player-state/store.db"
    assert helper.is_relative_to(resources / "assistant") and game.is_relative_to(resources)
    revision = "demo-page-first-pending-20261020.2" if page_first else "demo-delivery-boundary-20261020.1"
    assert revision in (root / "runtime/build_info.py").read_text(encoding="utf-8")
    freeze = json.loads((root / "dist" / ("page-first-final2-live-20261020.json" if page_first
                                          else "delivery-boundary-live-final-20261020.json")).read_text(encoding="utf-8"))
    assert freeze["restored"] and freeze["source_hashes_unchanged"] and not freeze.get("failure")
    assert all(digest(root / name) == value for name, value in freeze["source_hashes"].items()), "tested runtime changed"
    suites = ET.parse(root / "dist" / ("page-first-frozen-acceptance-20261020.xml" if page_first
                                       else "delivery-boundary-final-20261020.xml")).getroot().findall("testsuite")
    assert sum(int(s.get("tests", 0)) for s in suites) == (423 if page_first else 492)
    assert not any(int(s.get(k, 0)) for s in suites for k in ("failures", "errors", "skipped"))
    checked = json.loads((root / "dist" / ("page-first-final-check-20261020.json" if page_first
                                          else "delivery-boundary-check-20261020.json")).read_text(encoding="utf-8"))
    assert checked["returncode"] == 0
    assert all(digest(root / name) == value for name, value in checked["source_hashes"].items()), "checked package source changed"
    previous_receipt = None
    if args.retry_known_rollback is not None:
        previous_run = args.retry_known_rollback.resolve(strict=True)
        assert previous_run.is_relative_to(root / "dist"), "prior run must be plugin-owned"
        previous_receipt = json.loads((previous_run / "receipt.json").read_text(encoding="utf-8"))
        assert previous_receipt["build_revision"] == revision and previous_receipt["installed"] is False
        assert previous_receipt["events"][-1]["stage"] == "install_outcome_unknown"
        assert tree(installed) == tree(previous_run / "backup/plugin"), "completed rollback not confirmed by files"
        status = request("/plugin/status?plugin_id=hsr_companion", timeout=10)
        assert status.get("status", {}).get("status") == "running", "old runtime must be recovered before retry"
    run = root / "dist" / (("page-first-install-" if page_first else "delivery-boundary-install-")
                            + datetime.now().strftime("%Y%m%d-%H%M%S"))
    run.mkdir()
    report = {"build_revision": revision, "installed": False, "run": str(run), "events": []}
    if previous_receipt is not None:
        report["known_completed_rollback"] = str(args.retry_known_rollback.resolve())

    def save(name, value):
        (run / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

    def event(name, **data):
        report["events"].append({"stage": name, **data})
        save("receipt.json", report)
        print(json.dumps({"stage": name, **data}, ensure_ascii=True), flush=True)

    protected = {"profiles": tree(profile), "legacy_config": tree(legacy),
        "helper_pointer": digest(pointer), "helper_executable": digest(helper),
        "game_pointer": digest(game_pointer), "game_database": digest(game), "player_records": player_records(store)}
    core_names = ["main_logic/core/__init__.py", "plugin/server/application/plugin_cli/service.py",
                  "plugin/server/routes/plugin_cli.py", "plugin/sdk/plugin/__init__.py"]
    core_before = {n: digest(workspace / n) for n in core_names}
    context = request("/plugin/hsr_companion/hosted-ui/context?kind=panel&id=demo").get("state") or {}
    report["observer_enabled_before"] = context.get("observer", {}).get("enabled")
    backup = run / "backup"
    shutil.copytree(installed, backup / "plugin", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(profile, backup / "profiles")
    if legacy.exists():
        shutil.copytree(legacy, backup / "legacy-config")
    shutil.copy2(pointer, backup / "assistant-current.json")
    with sqlite3.connect(store.as_uri() + "?mode=ro", uri=True) as src, sqlite3.connect(backup / "player-state.db") as dst:
        src.backup(dst)
    event("backup_ready", backup=str(backup), old_files=len(tree(installed)))
    stage = run / "stage/hsr_companion"
    # Use the exact checker-frozen payload list, including new protocol modules.
    # Never rebuild by scanning dist/ or from an older archive's file list.
    names = set(checked["source_hashes"])
    for name in sorted(names):
        source, target = (root / name).resolve(strict=True), (stage / name).resolve()
        assert source.is_relative_to(root) and target.is_relative_to(stage)
        if name.startswith(("assistant_runtime/", "adapters/", "detectors/", "resources/", "ui/", "data/")) or name == "assistant.py":
            assert digest(source) == digest(installed / name), "out-of-scope source changed: " + name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    assert tree(stage) == checked["source_hashes"], "stage differs from checked payload"
    package = root / "dist" / ("hsr_companion-0.9.6-" + revision + ".neko-plugin")
    if previous_receipt is not None:
        assert package.resolve() == Path(previous_receipt["package"]).resolve()
        assert digest(package) == previous_receipt["package_sha256"], "reuse only the identical frozen package"
    else:
        assert not package.exists(), "never overwrite a frozen package"
        build_plugin(stage, package)
    inspected = inspect_package(package)
    assert inspected.package_id == "hsr_companion" and inspected.package_type == "plugin" and inspected.payload_hash_verified is True
    report.update(package=str(package), package_sha256=digest(package), backup=str(backup))
    event("package_verified")
    boundary = "hsr-delivery-boundary-local-upload"
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
        raise  # Inspect state; never repeat a possibly successful transaction.
    save("host-install-result.json", result)
    targets = result["installed_plugins"]
    assert len(targets) == 1 and Path(targets[0]["target_dir"]).resolve() == installed.resolve()
    assert result["payload_hash_verified"] is True and result["restarted"] is True
    assert tree(installed) == tree(stage), "installed hashes differ from verified payload"
    after = {"profiles": tree(profile), "legacy_config": tree(legacy),
        "helper_pointer": digest(pointer), "helper_executable": digest(helper),
        "game_pointer": digest(game_pointer), "game_database": digest(game), "player_records": player_records(store)}
    preserved = {name: after[name] == value for name, value in protected.items()}
    assert all(preserved.values()), "protected data changed"
    assert core_before == {n: digest(workspace / n) for n in core_names}
    report.update(installed=True, files_verified=len(tree(installed)), preserved=preserved,
                  host_core_changed=False, helper_updated=False, market_uploaded=False,
                  real_chat_acceptance="pending", host_install=result)
    event("installation_files_verified", files=report["files_verified"])
    for attempt in range(3):
        try:
            live = request("/plugin/hsr_companion/hosted-ui/action/diagnose_companion",
                {"args": {}, "kind": "panel", "surface_id": "demo"}, timeout=20)
            save("running-diagnostic.json", live)
            if revision in json.dumps(live):
                report["running_revision_verified"] = True
                event("runtime_revision_verified")
                break
        except Exception as exc:
            event("runtime_handshake_pending", attempt=attempt + 1, error_type=type(exc).__name__)
        time.sleep(2)
    save("receipt.json", report)
    if not report.get("running_revision_verified"):
        raise RuntimeError("Installed files verified; live revision not confirmed. Do not reinstall blindly.")
    print(str(run), flush=True)


if __name__ == "__main__":
    main()
