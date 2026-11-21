"""Game-specific reread policy supplied to the native OCR reader."""
from __future__ import annotations

from typing import Any

from ..data import normalize_lookup_text
from ..detectors.layout import PAGE_RULES
from ..detectors.names import read_name_label
from .native_text import read_native_text as _read_native_text
from .text_verification import verify_name_regions


def text_reading_callbacks(catalog):
    def name_labels(text):
        return read_name_label(text, catalog)[0]

    def skip_name_reread(box):
        if float(box.get("score") or 0) >= .95:
            if catalog.resolve_character(str(box["text"])) is not None:
                return True
        ui_terms = {normalize_lookup_text(term) for rule in PAGE_RULES for term in (*rule["strong"], *rule["anchors"])}
        return normalize_lookup_text(box["text"]) in ui_terms

    return name_labels, skip_name_reread, verify_name_regions


def read_native_text(
    backend: Any, image: Any, *, catalog: Any = None, cancelled=None, on_progress=None,
    verify_names: bool = False, accept_native_relic=None, relic_tiles=None, yield_requested=None,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """Preserve the legacy reader signature and its existing game policy."""
    name_labels, skip_name_reread, name_verifier = text_reading_callbacks(catalog)
    return _read_native_text(
        backend, image, catalog=catalog, cancelled=cancelled, on_progress=on_progress,
        verify_names=verify_names, accept_native_relic=accept_native_relic,
        relic_tiles=relic_tiles, yield_requested=yield_requested,
        name_labels=name_labels, skip_name_reread=skip_name_reread, name_verifier=name_verifier,
    )
