"""2a: query priority yields only cross-scope passes; reader yields at tile edges."""
import time
from types import SimpleNamespace

import pytest
from PIL import Image

from plugin.plugins.hsr_companion.adapters.text_reader import QueryPreempted, read_native_text
from plugin.plugins.hsr_companion.runtime.vision_scheduler import query_yield_requested


def owner(**overrides):
    values = {
        "_visual_query_waiters": 1,
        "_scene_query_waiters": 0,
        "_navigation_query_waiters": 0,
        "_content_query_waiters": 0,
        "_query_refresh_generation": 2,
        "_query_refresh_deadline": time.monotonic() + 8,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_full_pass_is_not_aborted_for_a_full_query():
    assert query_yield_requested(owner(), 1, "full") is False


def test_scene_pass_yields_to_a_waiting_full_query():
    assert query_yield_requested(owner(), 1, "scene") is True


def test_full_pass_is_not_aborted_for_a_content_query():
    assert query_yield_requested(owner(_content_query_waiters=1), 1, "full") is False


def test_navigation_pass_does_not_yield_to_a_scene_query():
    assert query_yield_requested(owner(_scene_query_waiters=1), 1, "navigation") is False


def test_scene_pass_yields_to_a_navigation_query():
    assert query_yield_requested(owner(_navigation_query_waiters=1), 1, "scene") is True


def test_expired_query_does_not_yield():
    assert query_yield_requested(owner(_query_refresh_deadline=time.monotonic() - 1), 1, "scene") is False


def test_reader_stops_between_native_tiles():
    class Backend:
        def __init__(self):
            self.calls = 0

        def extract_text_with_boxes(self, image):
            self.calls += 1
            return "", []

    backend = Backend()
    image = Image.new("RGB", (2560, 1600))
    checks = {"count": 0}

    def yield_requested():
        checks["count"] += 1
        return checks["count"] >= 2

    with pytest.raises(QueryPreempted):
        read_native_text(backend, image, yield_requested=yield_requested)
    assert backend.calls == 1
