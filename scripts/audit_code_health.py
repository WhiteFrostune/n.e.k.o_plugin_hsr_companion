"""Local-only code inventory and recoverable baseline; never imports the plugin.

Generated reports stay under dist. No game, model, account or host IO occurs.
"""
from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "dist/code-health-20261026"
IGNORED = {"dist", "vendor", "build", ".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv"}
SUFFIXES = {".py", ".toml", ".md", ".tsx", ".ts", ".json", ".mjs", ".yml", ".yaml", ".txt"}


def files():
    # Prune directories rather than walking all historical captures/archives.
    pending = [ROOT]
    while pending:
        for path in sorted(pending.pop().iterdir()):
            if path.is_dir():
                if path.name not in IGNORED and not path.name.startswith(".pytest_tmp"):
                    pending.append(path)
            elif path.suffix in SUFFIXES or path.name in {".gitignore"}:
                yield path


def main():
    RUN.mkdir(parents=True, exist_ok=True)
    paths = sorted(files())
    manifest = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    manifest_path = RUN / "baseline-hashes.json"
    if not manifest_path.exists():
        with zipfile.ZipFile(RUN / "baseline-source.zip", "w", zipfile.ZIP_DEFLATED) as archive:
            for path in paths:
                archive.write(path, path.relative_to(ROOT).as_posix())
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    findings = {"syntax_errors": [], "large_files": [], "duplicate_bodies": [], "unreachable": []}
    bodies = defaultdict(list)
    for path in paths:
        if path.suffix != ".py":
            continue
        name = path.relative_to(ROOT).as_posix()
        source = path.read_text(encoding="utf-8-sig")
        try:
            tree = ast.parse(source)
        except SyntaxError as error:
            findings["syntax_errors"].append({"file": name, "line": error.lineno, "error": error.msg})
            continue
        if name.startswith(("tests/", "scripts/", "experiments/", "assistant_runtime/")):
            continue
        findings["large_files"].append({"file": name, "lines": len(source.splitlines())})
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and len(node.body) > 3:
                body = list(node.body)
                if isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                    body = body[1:]
                key = ast.dump(ast.Module(body=body, type_ignores=[]), include_attributes=False)
                bodies[key].append({"file": name, "function": node.name, "line": node.lineno})
            for field in ("body", "orelse", "finalbody"):
                block = getattr(node, field, None)
                if not isinstance(block, list):
                    continue
                for index, statement in enumerate(block[:-1]):
                    if isinstance(statement, (ast.Return, ast.Raise, ast.Break, ast.Continue)):
                        findings["unreachable"].append({"file": name, "after_line": statement.lineno,
                                                        "next_line": block[index + 1].lineno})
    findings["large_files"] = sorted(findings["large_files"], key=lambda p: -p["lines"])[:20]
    findings["duplicate_bodies"] = [locations for locations in bodies.values() if len(locations) > 1]
    selected = [str(p) for p in paths if p.suffix == ".py"]
    ruff = Path(sys.executable).with_name("ruff.exe")
    result = subprocess.run([str(ruff), "check", "--no-cache", "--ignore-noqa", "--isolated",
                             "--target-version", "py311", "--line-length", "120",
                             "--select", "E4,E7,E9,F,I", "--output-format", "json", *selected],
                            capture_output=True, text=True, encoding="utf-8", check=False)
    if result.returncode not in {0, 1}:
        raise RuntimeError(result.stderr)
    lint = json.loads(result.stdout)
    (RUN / "lint-current.json").write_text(json.dumps(lint, ensure_ascii=False, indent=2), encoding="utf-8")
    findings["lint"] = dict(Counter(item["code"] for item in lint))
    findings["source_files"] = len(manifest)
    findings["baseline_files"] = len(json.loads(manifest_path.read_text(encoding="utf-8")))
    (RUN / "inventory-current.json").write_text(json.dumps(findings, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(findings, ensure_ascii=False))


if __name__ == "__main__":
    main()
