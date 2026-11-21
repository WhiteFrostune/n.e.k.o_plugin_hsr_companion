"""Layer dependency audit for the v2 architecture.

Run `python tests/test_layering.py` from the plugin root to rewrite
layers-baseline.toml. The baseline may only shrink: a stale entry is a
test failure, and a new cross-layer import is a failure.
"""
from __future__ import annotations

import ast
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "plugin.plugins.hsr_companion"
SKIP_PARTS = {
    "tests", "scripts", "dev", "experiments", "build", "dist", "vendor",
    "plugin", "__pycache__", ".venv", ".pytest_cache",
}
SIDEBARS = {"sidebar:contracts", "sidebar:knowledge", "sidebar:trace"}
ALLOWED = {
    "L1": {"L1", *SIDEBARS},
    "L2a": {"L1", "L2a", *SIDEBARS},
    "L2b": {"L1", "L2a", "L2b", *SIDEBARS},
    "L3": {"L3", *SIDEBARS},
    "L4": {"L4", *SIDEBARS},
    "L5": {"L3", "L4", "L5", *SIDEBARS},
    "entrypoint": {"L1", "L2a", "L2b", "L3", "L4", "L5", "entrypoint", *SIDEBARS},
    "sidebar:contracts": {"sidebar:contracts"},
    "sidebar:knowledge": {"sidebar:contracts", "sidebar:knowledge"},
    "sidebar:trace": {"sidebar:contracts", "sidebar:knowledge", "sidebar:trace"},
}


def load_layers() -> dict[str, str]:
    data = tomllib.loads((ROOT / "layers.toml").read_text(encoding="utf-8"))
    mapping: dict[str, str] = {}

    def add(pattern: str, layer: str) -> None:
        for path in ROOT.glob(pattern):
            if not path.is_file() or path.suffix != ".py":
                continue
            relative = path.relative_to(ROOT).as_posix()
            if relative in mapping and mapping[relative] != layer:
                raise AssertionError(f"{relative} mapped to both {mapping[relative]} and {layer}")
            mapping[relative] = layer

    for layer, patterns in data["layers"].items():
        for pattern in patterns:
            add(pattern, layer)
    for sidebar, patterns in data["sidebars"].items():
        for pattern in patterns:
            add(pattern, f"sidebar:{sidebar}")
    for pattern in data["entrypoints"]["paths"]:
        add(pattern, "entrypoint")
    return mapping


def module_name(relative: str) -> str:
    parts = list(Path(relative).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join([PACKAGE, *parts])


def module_map(mapping: dict[str, str]) -> dict[str, str]:
    return {module_name(relative): relative for relative in mapping}


def production_files() -> list[str]:
    result = []
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT)
        if any(part in SKIP_PARTS or part.startswith(".pytest_tmp") for part in relative.parts):
            continue
        result.append(relative.as_posix())
    return sorted(result)


def resolve_from(source: str, node: ast.ImportFrom) -> list[str]:
    parent = Path(source).parent
    package = ".".join([PACKAGE, *parent.parts]) if parent.parts else PACKAGE
    if node.level:
        parts = package.split(".")
        base = ".".join(parts[: len(parts) - (node.level - 1)]) if node.level > 1 else package
        module = base + (("." + node.module) if node.module else "")
    else:
        module = node.module or ""
    if not module:
        return []
    if node.module is None:
        # ``from . import name`` names sibling submodules, not the package
        # object. Counting the bare package flagged L2b -> runtime/__init__.py
        # for a plain ``from . import vision_scheduler``.
        return [f"{module}.{alias.name}" for alias in node.names if alias.name != "*"]
    return [module, *(f"{module}.{alias.name}" for alias in node.names if alias.name != "*")]


def imported_modules(source: str) -> list[str]:
    tree = ast.parse((ROOT / source).read_text(encoding="utf-8"))
    modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            modules.extend(resolve_from(source, node))
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
    return modules


def target_module(imported: str, modules: dict[str, str]) -> str | None:
    candidates = [name for name in modules if imported == name or imported.startswith(name + ".")]
    return max(candidates, key=len) if candidates else None


def collect_violations(mapping: dict[str, str]) -> list[str]:
    modules = module_map(mapping)
    violations = set()
    for source, source_layer in mapping.items():
        for imported in imported_modules(source):
            target = target_module(imported, modules)
            if target is None:
                continue
            target_path = modules[target]
            if target_path == source:
                continue
            if mapping[target_path] not in ALLOWED[source_layer]:
                violations.add(f"{source} -> {target_path}")
    return sorted(violations)


def _resolved(snippet: str, source: str) -> list[str]:
    return resolve_from(source, ast.parse(snippet).body[0])


def test_from_dot_import_resolves_submodule_not_package() -> None:
    """`from . import x` is a sibling module, not the runtime package itself."""
    assert _resolved("from . import vision_scheduler", "runtime/live_refresh.py") == [
        "plugin.plugins.hsr_companion.runtime.vision_scheduler"
    ]


def test_from_relative_module_keeps_module_and_members() -> None:
    resolved = _resolved("from .query_plan import QueryPlan", "runtime/grounding.py")
    assert "plugin.plugins.hsr_companion.runtime.query_plan" in resolved
    assert "plugin.plugins.hsr_companion.runtime.query_plan.QueryPlan" in resolved


def test_target_module_uses_longest_registered_prefix() -> None:
    """A submodule must not be attributed to its package's __init__.py."""
    modules = {
        "plugin.plugins.hsr_companion.runtime": "runtime/__init__.py",
        "plugin.plugins.hsr_companion.runtime.vision_scheduler": "runtime/vision_scheduler.py",
    }
    assert target_module("plugin.plugins.hsr_companion.runtime.vision_scheduler", modules) == (
        "plugin.plugins.hsr_companion.runtime.vision_scheduler"
    )


def write_baseline() -> None:
    mapping = load_layers()
    lines = [
        "# Current layering exceptions. Only shrink; delete an entry when fixed.",
        "violations = [",
        *(f'  "{item}",' for item in collect_violations(mapping)),
        "]",
        "",
    ]
    (ROOT / "layers-baseline.toml").write_text("\n".join(lines), encoding="utf-8")


def test_every_production_module_is_mapped() -> None:
    mapping = load_layers()
    missing = sorted(set(production_files()) - set(mapping))
    assert not missing, "Unmapped production modules:\n" + "\n".join(missing)


def test_layering_baseline_matches_current_violations() -> None:
    mapping = load_layers()
    current = set(collect_violations(mapping))
    baseline_path = ROOT / "layers-baseline.toml"
    baseline = set(tomllib.loads(baseline_path.read_text(encoding="utf-8"))["violations"])
    new = sorted(current - baseline)
    stale = sorted(baseline - current)
    assert not new, "New cross-layer imports; keep the boundary or add a justified exception:\n" + "\n".join(new)
    assert not stale, "Baseline entries no longer occur; delete them so the whitelist can only shrink:\n" + "\n".join(stale)


if __name__ == "__main__":
    write_baseline()
    print(f"wrote {ROOT / 'layers-baseline.toml'}")
