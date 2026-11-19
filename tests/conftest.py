"""Plugin test collection policy: local live artifacts and host-only guards."""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def pytest_configure(config):
    config.addinivalue_line("markers", "live_artifact(*paths): requires a local dist live capture")
    config.addinivalue_line("markers", "host_clock_guard: skip when the host forbids stdlib clock patching")


def pytest_collection_modifyitems(config, items):
    host_root = Path(config.rootpath)
    host_clock_guard = (host_root / "tests" / "clock_guard.py").is_file() and host_root != ROOT
    for item in items:
        marker = item.get_closest_marker("live_artifact")
        if marker is not None:
            paths = list(marker.args or getattr(item.module, "LIVE_ARTIFACTS", ()) or ())
            missing = [path for path in paths if not (ROOT / path).exists()]
            if missing:
                item.add_marker(pytest.mark.skip(
                    reason="local live artifact missing: " + ", ".join(missing)))
        if host_clock_guard and item.get_closest_marker("host_clock_guard"):
            item.add_marker(pytest.mark.skip(
                reason="host clock guard forbids stdlib time monkeypatch; use tests.fake_clock.patch_module_clock"))
