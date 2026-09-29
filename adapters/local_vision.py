"""Local, privacy-bounded OCR adapter backed by N.E.K.O's shared RapidOCR."""

from __future__ import annotations

from io import BytesIO
from typing import Any

from ..data import StructuredCatalog
from ..detectors import analyze_layout
from ..session import visual_fingerprint


class LocalVisionAdapter:
    def __init__(self, catalog: StructuredCatalog, *, logger: Any | None = None) -> None:
        self.catalog = catalog
        self.logger = logger
        self._backend: Any | None = None
        self._last_error = ""

    def _get_backend(self) -> Any:
        if self._backend is not None:
            return self._backend
        from plugin.plugins._shared.rapidocr import (
            DEFAULT_RAPIDOCR_ENGINE_TYPE,
            DEFAULT_RAPIDOCR_LANG_TYPE,
            DEFAULT_RAPIDOCR_MODEL_TYPE,
            DEFAULT_RAPIDOCR_OCR_VERSION,
            RapidOcrBackend,
        )

        self._backend = RapidOcrBackend(
            install_target_dir_raw="",
            engine_type=DEFAULT_RAPIDOCR_ENGINE_TYPE,
            lang_type=DEFAULT_RAPIDOCR_LANG_TYPE,
            model_type=DEFAULT_RAPIDOCR_MODEL_TYPE,
            ocr_version=DEFAULT_RAPIDOCR_OCR_VERSION,
            plugin_id="hsr_companion",
        )
        return self._backend

    def status(self) -> dict[str, Any]:
        try:
            backend = self._get_backend()
            available = bool(backend.is_available())
            return {
                "backend": "neko_shared_rapidocr",
                "available": available,
                "last_error": self._last_error,
            }
        except Exception as exc:  # noqa: BLE001 - optional local runtime
            self._last_error = f"{type(exc).__name__}: {exc}"
            return {
                "backend": "neko_shared_rapidocr",
                "available": False,
                "last_error": self._last_error,
            }

    def analyze(self, image_bytes: bytes) -> dict[str, Any]:
        try:
            from PIL import Image, ImageOps

            with Image.open(BytesIO(image_bytes)) as opened:
                image = ImageOps.exif_transpose(opened).convert("RGB")
            backend = self._get_backend()
            text, boxes = backend.extract_text_with_boxes(image)
            layout = analyze_layout(
                text,
                boxes,
                self.catalog,
                width=image.width,
                height=image.height,
            )
            for character in layout["characters"]:
                region = character.get("region") or {}
                try:
                    left = max(0, int(float(region.get("left") or 0.0) * image.width) - 12)
                    top = max(0, int(float(region.get("top") or 0.0) * image.height) - 8)
                    right = min(image.width, int(float(region.get("right") or 0.0) * image.width) + 12)
                    bottom = min(image.height, int(float(region.get("bottom") or 0.0) * image.height) + 8)
                    if right > left and bottom > top:
                        cropped = image.crop((left, top, right, bottom))
                        payload = BytesIO()
                        cropped.save(payload, format="PNG")
                        character["visual_fingerprint"] = visual_fingerprint(payload.getvalue())
                except (TypeError, ValueError, OSError):
                    pass
            self._last_error = ""
            return {
                "ok": True,
                "ocr": {
                    "backend": "neko_shared_rapidocr",
                    "text": text,
                    "line_count": len(boxes),
                    "text_preview": " ".join(text.split())[:300],
                },
                "page": layout["page"],
                "scene": layout["scene"],
                "characters": layout["characters"],
                "image_stored": False,
                "catalog_revision": self.catalog.revision,
            }
        except Exception as exc:  # noqa: BLE001 - degrade safely to unknown
            self._last_error = f"{type(exc).__name__}: {exc}"
            if self.logger is not None:
                try:
                    self.logger.warning("HSR local OCR unavailable: %s", self._last_error)
                except Exception:
                    pass
            return {
                "ok": False,
                "ocr": {
                    "backend": "neko_shared_rapidocr",
                    "text": "",
                    "line_count": 0,
                    "text_preview": "",
                    "error": self._last_error,
                },
                "scene": {
                    "primary_state": "unknown",
                    "substate": "unknown",
                    "confidence": "low",
                    "evidence": [],
                    "classifier": "hsr_layout_ocr_v1",
                    "verified": False,
                },
                "page": {
                    "page_type": "unknown",
                    "label": "暂不支持的画面",
                    "supported": False,
                },
                "characters": [],
                "image_stored": False,
                "catalog_revision": self.catalog.revision,
            }

    def close(self) -> None:
        backend = self._backend
        self._backend = None
        if backend is not None:
            close = getattr(backend, "close", None)
            if callable(close):
                close()


__all__ = ["LocalVisionAdapter"]
