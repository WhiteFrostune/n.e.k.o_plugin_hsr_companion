"""The next development build must not ship replay scripts or private reports."""
import tomllib
from pathlib import Path

import pytest

from plugin.neko_plugin_cli.core.build_rules import load_build_rules, should_skip_path

ROOT = Path(__file__).resolve().parents[1]


def rules():
    return load_build_rules(tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8")))


@pytest.mark.parametrize("name", [
    "tests/test_smoke.py", "scripts/publish_public_release_096.py", "experiments/regional_reader.py",
    "docs/TEST_RUN_RELIC_REPAIR_20261017.md", "docs/RECORDING_STAGE1_20261021.json",
    "README_v0.9.6_PREVIEW.md", "store.db", "example.sqlite3", ".env",
])
def test_development_files_and_player_stores_are_not_install_payload(name):
    assert should_skip_path(Path(name), is_dir=False, rules=rules())


def test_all_runtime_sources_and_declared_surfaces_remain_included():
    selected = rules()
    paths = [*ROOT.glob("*.py")]
    for folder in ("runtime", "adapters", "detectors", "data", "assistant_runtime"):
        paths.extend((ROOT / folder).glob("*.py"))
    manifest = tomllib.loads((ROOT / "plugin.toml").read_text(encoding="utf-8"))
    for kind in ("panel", "guide", "docs"):
        paths.extend(ROOT / row["entry"] for row in manifest["plugin"]["ui"].get(kind, []))
    paths.extend((ROOT / "resources").rglob("*.json"))
    paths.extend((ROOT / "i18n").rglob("*.json"))
    for path in paths:
        assert path.exists(), path
        assert not should_skip_path(path.relative_to(ROOT), is_dir=False, rules=selected), path


def test_helper_download_points_to_the_published_frozen_release_without_account_binding():
    from urllib.parse import urlsplit

    from plugin.plugins.hsr_companion.assistant import DEFAULT_COMPONENT_MANIFEST_URL

    url = urlsplit(DEFAULT_COMPONENT_MANIFEST_URL)
    assert url.scheme == "https" and url.netloc == "github.com"
    assert url.path.endswith("/releases/download/v0.9.6/hsr-vision-assistant-manifest.json")
    assert not url.username and not url.password and not url.query and not url.fragment
