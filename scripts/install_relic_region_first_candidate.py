"""Install the relic-region-first candidate through the running host.

Backs up the current installed plugin and protected player data, uploads the
already-built package, verifies the host plan and installed tree, and confirms
the live build revision. Refuses to change anything when the host is closed.
"""
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

HOST = "http://127.0.0.1:48916"
REVISION = "relic-region-first-20261115.1"
CURRENT_REVISION = "request-routing-20261114.1"
APP = Path("C:/Users/cai/AppData/Local/N.E.K.O")


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
    req = urllib.request.Request(HOST + route, data=data,
                                 headers={"Content-Type": content_type} if data is not None else {})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def current_revision(installed):
    for line in (installed / "runtime/build_info.py").read_text(encoding="utf-8").splitlines():
        if line.startswith("BUILD_REVISION"):
            return line.split("=")[-1].strip().strip('"')
    raise RuntimeError("installed build revision missing")


def main():
    root = Path(__file__).resolve().parents[1]
    workspace = root.parents[2]
    installed = APP / ".neko-plugin-installations/plugins/hsr_companion"
    profile = APP / ".neko-package-profiles/hsr_companion"
    legacy = APP / "plugins/hsr_companion/config"
    resources = APP / "data-packs/hsr_companion"
    helper_pointer = resources / "assistant/current.json"
    game_pointer = resources / "current.json"
    store = resources / "player-state/store.db"
    helper = (helper_pointer.parent / json.loads(helper_pointer.read_text(encoding="utf-8"))["executable"]).resolve(strict=True)
    game = (resources / json.loads(game_pointer.read_text(encoding="utf-8"))["database"]).resolve(strict=True)
    assert helper.is_relative_to(resources / "assistant") and game.is_relative_to(resources)

    candidate = json.loads((root / "dist/relic-region-first-20261115/candidate.json").read_text(encoding="utf-8"))
    package = root / "dist/relic-region-first-20261115" / candidate["package"]
    assert candidate["revision"] == REVISION and candidate["source_matches"] is True
    assert package.is_file() and digest(package) == candidate["sha256"], "candidate package changed"
    assert current_revision(installed) == CURRENT_REVISION, "unexpected installed baseline"

    try:
        request("/plugin/status?plugin_id=hsr_companion", timeout=8)
    except Exception as exc:
        print(json.dumps({"stage": "host_unavailable", "error_type": type(exc).__name__,
                          "hint": "open N.E.K.O so 127.0.0.1:48916 is listening; no files changed"},
                         ensure_ascii=True), flush=True)
        return 2

    run = root / "dist/relic-region-first-20261115" / ("install-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    run.mkdir()
    report = {"build_revision": REVISION, "installed": False, "run": str(run), "events": []}

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
    core_names = ["main_logic/core/__init__.py", "plugin/server/application/plugin_cli/service.py",
                  "plugin/server/routes/plugin_cli.py", "plugin/sdk/plugin/__init__.py"]
    core_before = {name: digest(workspace / name) for name in core_names}
    try:
        context = request("/plugin/hsr_companion/hosted-ui/context?kind=panel&id=demo").get("state") or {}
        report["observer_enabled_before"] = context.get("observer", {}).get("enabled")
    except Exception:
        report["observer_enabled_before"] = None

    backup = run / "backup"
    shutil.copytree(installed, backup / "plugin", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(profile, backup / "profiles")
    shutil.copytree(legacy, backup / "legacy-config")
    shutil.copy2(helper_pointer, backup / "assistant-current.json")
    with sqlite3.connect(store.as_uri() + "?mode=ro", uri=True) as src, sqlite3.connect(backup / "player-state.db") as dst:
        src.backup(dst)
    event("backup_ready", backup=str(backup), old_files=len(tree(installed)))

    with zipfile.ZipFile(package) as archive:
        names = [n for n in archive.namelist()
                 if n.startswith("payload/plugins/hsr_companion/") and not n.endswith("/")]
        stage_files = {n[len("payload/plugins/hsr_companion/"):]: hashlib.sha256(archive.read(n)).hexdigest()
                       for n in names}
        archive.extractall(run / "stage")
    stage = run / "stage/payload/plugins/hsr_companion"
    assert len(stage_files) == candidate["payload_files"] == 110
    assert tree(stage) == stage_files, "staged payload differs from verified archive"
    assert all(digest(root / name) == value for name, value in stage_files.items()), "source changed after packaging"
    event("stage_verified", files=len(stage_files))

    boundary = "hsr-relic-region-first-local-upload"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{package.name}\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n").encode() + package.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    upload = request("/plugin-cli/upload", body, content_type="multipart/form-data; boundary=" + boundary)
    payload = {"package": upload["path"]}
    save("host-inspect.json", request("/plugin-cli/inspect", payload))
    plan = request("/plugin-cli/install-plan", payload)
    save("install-plan.json", {"upload": upload, "plan": plan})
    assert plan["plugin_id"] == plan["directory_name"] == "hsr_companion"
    assert plan["action"] in {"upgrade", "reinstall"} and plan["target_version"] == "0.9.6"
    assert plan["confirmation_token"] and not plan.get("legacy_plugin_ids")
    event("install_plan_verified", action=plan["action"])

    try:
        result = request("/plugin-cli/install", {**payload, "confirm_upgrade": True,
                         "confirmation_token": plan["confirmation_token"], "on_conflict": "fail"}, timeout=55)
    except Exception as exc:
        event("install_outcome_unknown", error_type=type(exc).__name__)
        raise RuntimeError("inspect installed state; do not repeat a possibly successful install") from exc
    save("host-install-result.json", result)
    targets = result["installed_plugins"]
    assert len(targets) == 1 and Path(targets[0]["target_dir"]).resolve() == installed.resolve()
    assert result["payload_hash_verified"] is True
    assert tree(installed) == stage_files, "installed files differ from verified payload"
    after = {"profiles": tree(profile), "legacy_config": tree(legacy),
             "helper_pointer": digest(helper_pointer), "helper_executable": digest(helper),
             "game_pointer": digest(game_pointer), "game_database": digest(game),
             "player_records": player_records(store)}
    preserved = {name: after[name] == value for name, value in protected.items()}
    assert all(preserved.values()), "protected player data changed"
    assert core_before == {name: digest(workspace / name) for name in core_names}
    report.update(installed=True, files_verified=len(tree(installed)), preserved=preserved,
                  host_core_changed=False, helper_updated=False, host_install=result)
    event("installation_files_verified", files=report["files_verified"])

    for attempt in range(3):
        try:
            live = request("/plugin/hsr_companion/hosted-ui/action/diagnose_companion",
                           {"args": {}, "kind": "panel", "surface_id": "demo"}, timeout=20)
            save("running-diagnostic.json", live)
            if REVISION in json.dumps(live) and live.get("ok", True):
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
    raise SystemExit(main())
