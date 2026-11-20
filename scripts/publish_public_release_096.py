"""Publish only the verified isolated 0.9.6 snapshot, never a market submission.

Uses Git's existing credential helper in memory; credentials are never logged,
written to files, placed in a URL or sent outside GitHub's two API hosts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "dist/release-0.9.6-20261023"
SOURCE = RUN / "source/hsr_companion"
ASSETS = RUN / "assets"
REPO = "WhiteFrostune/n.e.k.o_plugin_hsr_companion"
API = f"https://api.github.com/repos/{REPO}"
NAMES = {"hsr_companion.neko-plugin", "hsr-vision-assistant-win-x64.zip",
         "hsr-vision-assistant-manifest.json", "release-evidence.json", "SHA256SUMS.txt"}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Authenticated GitHub request unexpectedly redirected")


def main(publish, status=False, annotate_ci=False):
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"}
    credential = subprocess.run(["git", "credential", "fill"], cwd=SOURCE, env=env,
        input=f"protocol=https\nhost=github.com\npath={REPO}.git\n\n",
        text=True, capture_output=True, timeout=30)
    if credential.returncode:
        raise RuntimeError("Git credential helper did not provide an existing credential")
    fields = dict(line.split("=", 1) for line in credential.stdout.splitlines() if "=" in line)
    token = fields.get("password")
    if not token:
        raise RuntimeError("GitHub credential unavailable")
    opener = urllib.request.build_opener(NoRedirect())

    def request(url, *, method="GET", data=None, raw=None):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in {"api.github.com", "uploads.github.com"}:
            raise RuntimeError("Refusing non-GitHub credential destination")
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "hsr-companion-release/0.9.6"}
        payload = raw
        if data is not None:
            payload = json.dumps(data, ensure_ascii=False).encode()
            headers["Content-Type"] = "application/json"
        elif raw is not None:
            headers["Content-Type"] = "application/octet-stream"
        try:
            with opener.open(urllib.request.Request(url, data=payload, headers=headers, method=method),
                             timeout=600 if raw is not None else 30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 404 and method == "GET":
                return None
            # Avoid printing arbitrary server/transport text or request headers.
            raise RuntimeError(f"GitHub API failed with HTTP {error.code}") from None

    repo = request(API)
    if not repo or not repo.get("permissions", {}).get("push"):
        raise RuntimeError("Existing Git credential lacks repository write access")
    print(json.dumps({"repository": REPO, "push_permission": True, "publishing": publish}), flush=True)
    def find_release():
        # GitHub's by-tag endpoint can omit drafts. Recover the original draft,
        # rather than creating a second release after an interrupted upload.
        release = request(API + "/releases/tags/v0.9.6")
        if release is not None:
            return release
        matches = [r for r in request(API + "/releases?per_page=100")
                   if r["tag_name"] == "v0.9.6"]
        assert len(matches) <= 1, "Multiple matching drafts; stop instead of guessing"
        return matches[0] if matches else None
    if annotate_ci:
        release = find_release()
        assert release and release["id"] == 421548250 and not release["draft"]
        jobs = request(API + "/actions/runs/39916690508/jobs")["jobs"]
        assert any(s["name"] == "Ruff check" and s["conclusion"] == "failure"
                   for j in jobs for s in j.get("steps", []))
        marker = "## 发布后自动检查"
        if marker not in release["body"]:
            body = release["body"] + (
                "\n\n" + marker + "\n\n"
                "GitHub Actions 的 Ruff 静态检查未通过，后续测试未执行；"
                "本次未为消除静态检查问题修改冻结运行代码，不宣称通过市场验收。"
                "[检查记录](https://github.com/WhiteFrostune/n.e.k.o_plugin_hsr_companion/"
                "actions/runs/39916690508)\n")
            request(API + f"/releases/{release['id']}", method="PATCH", data={"body": body})
        print(json.dumps({"release_check_note_recorded": True}), flush=True)
        return
    if status:
        release = find_release()
        print(json.dumps(None if not release else {
            "id": release["id"], "draft": release["draft"], "prerelease": release["prerelease"],
            "assets": [{k: a.get(k) for k in ("id", "name", "state", "size", "digest")}
                       for a in release.get("assets", [])]}, ensure_ascii=False), flush=True)
        return
    if not publish:
        return
    assert {p.name for p in ASSETS.iterdir() if p.is_file()} == NAMES
    expected = {}
    for row in (ASSETS / "SHA256SUMS.txt").read_text().splitlines():
        digest, name = row.split("  ", 1)
        assert name in NAMES and hashlib.sha256((ASSETS / name).read_bytes()).hexdigest() == digest
        expected[name] = digest
    expected["SHA256SUMS.txt"] = hashlib.sha256((ASSETS / "SHA256SUMS.txt").read_bytes()).hexdigest()
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=SOURCE, capture_output=True,
                          text=True, check=True).stdout.strip()
    evidence = json.loads((ASSETS / "release-evidence.json").read_text())
    assert evidence["source_commit"] == head
    assert subprocess.run(["git", "status", "--porcelain"], cwd=SOURCE,
                          capture_output=True, text=True, check=True).stdout == ""
    remote_commit = request(API + "/commits/v0.9.6")
    assert remote_commit and remote_commit["sha"] == head, "Remote tag must match the verified source"
    release = find_release()
    if release is None:
        release = request(API + "/releases", method="POST", data={
            "tag_name": "v0.9.6", "target_commitish": head,
            "name": "v0.9.6（公开测试快照）", "draft": True, "prerelease": True,
            "make_latest": "false", "body": (SOURCE / "docs/RELEASE_NOTES_v0.9.6.md").read_text(encoding="utf-8")})
    assert release["prerelease"] is True
    upload_url = release["upload_url"].split("{", 1)[0]
    for name in sorted(NAMES):
        existing = next((a for a in release.get("assets", []) if a["name"] == name), None)
        if existing:
            assert existing.get("digest") == "sha256:" + expected[name], "Existing asset differs; never clobber"
            continue
        print(json.dumps({"uploading": name, "bytes": (ASSETS / name).stat().st_size}), flush=True)
        asset = request(upload_url + "?" + urllib.parse.urlencode({"name": name}),
                        method="POST", raw=(ASSETS / name).read_bytes())
        assert asset["state"] == "uploaded" and asset["size"] == (ASSETS / name).stat().st_size
        assert asset.get("digest") == "sha256:" + expected[name], "Remote asset checksum mismatch"
    release = request(API + f"/releases/{release['id']}")
    assert {a["name"] for a in release["assets"]} == NAMES
    assert all(a.get("digest") == "sha256:" + expected[a["name"]] for a in release["assets"])
    if release["draft"]:
        release = request(API + f"/releases/{release['id']}", method="PATCH",
                          data={"draft": False, "prerelease": True, "make_latest": "false"})
    assert release["draft"] is False and release["prerelease"] is True
    receipt = {"url": release["html_url"], "tag": "v0.9.6", "commit": head,
               "prerelease": True, "market_submission": False,
               "assets": [{"name": a["name"], "sha256": expected[a["name"]],
                           "url": a["browser_download_url"]} for a in release["assets"]]}
    (RUN / "published.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--annotate-ci", action="store_true")
    arguments = parser.parse_args()
    main(arguments.publish, arguments.status, arguments.annotate_ci)
