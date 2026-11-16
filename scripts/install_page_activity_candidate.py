"""Explicitly authorized local HSR-only install; preserve data and original helper."""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import time
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree(path):
    return {p.relative_to(path).as_posix(): digest(p) for p in path.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}


def player_records(path):
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        rows = db.execute("SELECT key,value FROM kv_store WHERE key IN (?,?) ORDER BY key",
                          ("manual_player_builds", "uid_import_state")).fetchall()
    return hashlib.sha256(repr(rows).encode()).hexdigest()


def request(route, data=None, *, timeout=30, content_type="application/json"):
    if data is not None and not isinstance(data, bytes):
        data = json.dumps(data).encode()
    req = urllib.request.Request("http://127.0.0.1:48916" + route, data=data,
        headers={"Content-Type": content_type} if data is not None else {})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def main():
    from plugin.neko_plugin_cli.core import build_plugin, inspect_package

    root = Path(__file__).resolve().parents[1]
    workspace = root.parents[2]
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
    revision = "demo-page-activity-20261020.1"
    assert revision in (root / "runtime/build_info.py").read_text(encoding="utf-8")
    freeze = json.loads((root / "dist/page-activity-live3-final-20261020.json").read_text(encoding="utf-8"))
    assert all(digest(root / name) == value for name, value in freeze["source_hashes"].items()), "tested runtime changed"
    run = root / "dist" / ("page-activity-install-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    run.mkdir()
    report = {"build_revision": revision, "installed": False, "run": str(run), "events": []}

    def save(name, value):
        (run / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

    def event(name, **data):
        report["events"].append({"stage": name, **data})
        save("receipt.json", report)
        print(json.dumps({"stage": name, **data}, ensure_ascii=True), flush=True)

    protected = {"profiles": tree(profile), "legacy_config": tree(legacy),
        "helper_pointer": digest(helper_pointer), "helper_executable": digest(helper),
        "game_pointer": digest(game_pointer), "game_database": digest(game),
        "player_records": player_records(store)}
    # Representative host implementation/entry files; no writes to any host path.
    core_names = ["main_logic/core/__init__.py", "plugin/server/application/plugin_cli/service.py",
                  "plugin/server/routes/plugin_cli.py", "plugin/sdk/plugin/__init__.py"]
    core_before = {n: digest(workspace / n) for n in core_names}
    initial_context = request("/plugin/hsr_companion/hosted-ui/context?kind=panel&id=demo").get("state") or {}
    report["observer_enabled_before"] = initial_context.get("observer", {}).get("enabled")
    backup = run / "backup"
    shutil.copytree(installed, backup / "plugin", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(profile, backup / "profiles")
    if legacy.exists():
        shutil.copytree(legacy, backup / "legacy-config")
    shutil.copy2(helper_pointer, backup / "assistant-current.json")
    with sqlite3.connect(store.as_uri() + "?mode=ro", uri=True) as src, sqlite3.connect(backup / "player-state.db") as dst:
        src.backup(dst)
    event("backup_ready", backup=str(backup), old_files=len(tree(installed)))
    stage = run / "stage/hsr_companion"
    previous = root / "dist/hsr_companion-0.9.6-demo-semantic-routing-20261020.1.neko-plugin"
    prefix = "payload/plugins/hsr_companion/"
    with zipfile.ZipFile(previous) as archive:
        names = [n[len(prefix):] for n in archive.namelist() if n.startswith(prefix) and not n.endswith("/")]
    names += ["runtime/page_control_trace.py", "tests/test_page_activity_contract_20261020.py",
              "scripts/verify_page_activity_candidate.py"]
    for name in sorted(set(names)):
        source, target = (root / name).resolve(strict=True), (stage / name).resolve()
        assert source.is_relative_to(root) and target.is_relative_to(stage)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    package = root / "dist" / ("hsr_companion-0.9.6-" + revision + ".neko-plugin")
    assert not package.exists(), "never overwrite a frozen package"
    build_plugin(stage, package)
    inspected = inspect_package(package)
    assert inspected.package_id == "hsr_companion" and inspected.package_type == "plugin" and inspected.payload_hash_verified is True
    report.update(package=str(package), package_sha256=digest(package), backup=str(backup))
    event("package_verified")
    boundary = "hsr-page-activity-local-upload"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{package.name}\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n").encode() + package.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    upload = request("/plugin-cli/upload", body, content_type="multipart/form-data; boundary=" + boundary)
    payload = {"package": upload["path"]}
    checked = request("/plugin-cli/inspect", payload)
    save("host-inspect.json", checked)
    plan = request("/plugin-cli/install-plan", payload)
    save("install-plan.json", {"upload": upload, "plan": plan})
    assert plan["plugin_id"] == plan["directory_name"] == "hsr_companion"
    assert plan["action"] in {"upgrade", "reinstall"} and plan["target_version"] == "0.9.6" and plan["confirmation_token"]
    assert not plan.get("legacy_plugin_ids"), "unexpected additional install targets"
    event("install_plan_verified", action=plan["action"])
    # The standard host transaction stops/restarts ONLY this plugin and rolls back on failure.
    try:
        result = request("/plugin-cli/install", {**payload, "confirm_upgrade": True,
            "confirmation_token": plan["confirmation_token"], "on_conflict": "fail"}, timeout=55)
    except Exception as exc:
        event("install_outcome_unknown", error_type=type(exc).__name__)
        raise  # Never blindly repeat a possibly successful install.
    save("host-install-result.json", result)
    targets = result["installed_plugins"]
    assert len(targets) == 1 and Path(targets[0]["target_dir"]).resolve() == installed.resolve()
    assert result["payload_hash_verified"] is True and result["restarted"] is True
    expected = tree(stage)
    actual = tree(installed)
    assert actual == expected, "installed file hashes differ from frozen source payload"
    after = {"profiles": tree(profile), "legacy_config": tree(legacy),
        "helper_pointer": digest(helper_pointer), "helper_executable": digest(helper),
        "game_pointer": digest(game_pointer), "game_database": digest(game), "player_records": player_records(store)}
    preserved = {name: after[name] == value for name, value in protected.items()}
    assert all(preserved.values()), "protected data changed"
    assert core_before == {n: digest(workspace / n) for n in core_names}
    report.update(installed=True, files_verified=len(actual), preserved=preserved,
                  host_core_changed=False, helper_updated=False, market_uploaded=False,
                  real_chat_acceptance="pending", host_install=result)
    event("installation_files_verified", files=len(actual))
    # Bounded read-only live handshake; do not issue a test chat or trigger OCR.
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
        raise RuntimeError("Files installed; live revision not yet confirmed. Do not reinstall blindly.")
    print(str(run), flush=True)


if __name__ == "__main__":
    main()
