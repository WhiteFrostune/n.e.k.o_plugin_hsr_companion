"""An already detected name still needs an independent enlarged agreement."""
import numpy as np
import pytest
from plugin.plugins.hsr_companion.adapters.text_verification import verify_name_regions
from plugin.plugins.hsr_companion.detectors.character_regions import read_character_detail
from plugin.plugins.hsr_companion.tests.test_current_observation import plugin_fixture
from plugin.plugins.hsr_companion.tests.test_s1_preemption_closeout import PixelBackend, picture


class LineBackend(PixelBackend):
    def __init__(self, *, line="银狼", line_score=.99, score=.99, error=False):
        super().__init__(score=score)
        self.line, self.line_score, self.error = line, line_score, error
        self.line_calls = []

    def extract_text_with_boxes(self, image):
        if self.calls:
            self.calls.append({"size": image.size, "tokens": []})
            return "", []  # The real failure: second detection has no boxes.
        return super().extract_text_with_boxes(image)

    def recognize_line(self, image):
        self.line_calls.append(image.size)
        pixels = np.asarray(image.convert("RGB"))
        assert np.any(np.all(pixels == (220, 180, 30), axis=2))
        if self.error:
            raise RuntimeError("failed recognition")
        return [(self.line, self.line_score)]


def test_detected_name_is_confirmed_even_when_second_detection_would_be_empty(tmp_path):
    owner, _, _ = plugin_fixture(tmp_path)
    backend = LineBackend()
    result = read_character_detail(backend, picture(), owner._catalog, fields={"characters"})
    assert [actor["entity_id"] for actor in result["characters"]] == ["character.silverwolf"]
    assert len(backend.calls) == 1 and len(backend.line_calls) == 1
    assert result["characters"][0]["name_validation"]["method"] == "native_and_enlarged_label_agree"
    assert min(result["characters"][0]["name_validation"]["scores"]) >= .90


@pytest.mark.parametrize("options", [
    {"line": "花火"}, {"line": ""}, {"line_score": .899},
    {"score": .899}, {"line": "银狼 花火"}, {"error": True},
])
def test_line_read_cannot_override_name_quality_or_conflicts(tmp_path, options):
    owner, _, _ = plugin_fixture(tmp_path)
    backend = LineBackend(**options)
    result = read_character_detail(backend, picture(), owner._catalog, fields={"characters"})
    assert not result["characters"]


def test_default_verifier_keeps_original_detector_path(tmp_path):
    from io import BytesIO

    from PIL import Image
    owner, _, _ = plugin_fixture(tmp_path)
    backend = LineBackend()
    image = Image.open(BytesIO(picture())).convert("RGB")
    _, boxes = backend.extract_text_with_boxes(image)
    checked, _ = verify_name_regions(backend, image, boxes, owner._catalog)
    assert not any(b.get("name_validation") for b in checked)
    assert not backend.line_calls


def test_missing_page_controls_cannot_be_replaced_by_good_line_read(tmp_path):
    owner, _, _ = plugin_fixture(tmp_path)
    backend = LineBackend()
    result = read_character_detail(backend, picture(controls=False), owner._catalog, fields={"characters"})
    assert result is None
    assert not backend.line_calls


def test_cancellation_during_line_recognition_never_confirms(tmp_path):
    from plugin.plugins.hsr_companion.adapters.native_text import ObservationSuperseded
    owner, _, _ = plugin_fixture(tmp_path)
    backend = LineBackend()
    stopped = False
    original = backend.recognize_line

    def interrupt(image):
        nonlocal stopped
        result = original(image)
        stopped = True
        return result

    backend.recognize_line = interrupt
    with pytest.raises(ObservationSuperseded):
        read_character_detail(backend, picture(), owner._catalog, fields={"characters"},
                              cancelled=lambda: stopped)


def test_equal_current_frame_does_not_decode_controls_twice(tmp_path, monkeypatch):
    import asyncio
    import time

    from plugin.plugins.hsr_companion.runtime import current_regions
    from plugin.plugins.hsr_companion.runtime.query_plan import build_query_plan
    from plugin.plugins.hsr_companion.tests.test_current_character_regions import owner_at_character
    owner, captures, _ = owner_at_character(tmp_path)

    def redundant_check(*args):
        raise AssertionError("equal fresh bytes need no duplicate control decode")

    monkeypatch.setattr(current_regions, "regions_changed", redundant_check)
    result = asyncio.run(current_regions.read_current_character(
        owner, build_query_plan("", "read_screen", screen_fields=["characters"]), time.monotonic() + 1))
    assert result["characters"] and len(captures) >= 2
    assert all(c.image_bytes == captures[0].image_bytes for c in captures)


def test_multiple_native_name_candidates_keep_multiline_verification(tmp_path):
    owner, _, _ = plugin_fixture(tmp_path)
    backend = LineBackend()
    result = read_character_detail(backend, picture(two_names=True), owner._catalog, fields={"characters"})
    assert not result["characters"]
    assert not backend.line_calls


def test_duplicate_enlarged_name_lines_do_not_become_one_match(tmp_path):
    owner, _, _ = plugin_fixture(tmp_path)
    backend = LineBackend()
    backend.recognize_line = lambda image: [("银狼", .99), ("银狼", .99)]
    result = read_character_detail(backend, picture(), owner._catalog, fields={"characters"})
    assert not result["characters"]
