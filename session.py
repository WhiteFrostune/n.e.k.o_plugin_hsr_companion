"""Compatibility entrypoint retaining the original session API."""

from .runtime.contracts import visual_fingerprints_match as visual_fingerprints_match
from .session_state import DEFAULT_SESSION_TTL_SECONDS as DEFAULT_SESSION_TTL_SECONDS
from .session_state import SESSION_SCHEMA_VERSION as SESSION_SCHEMA_VERSION
from .session_state import VALID_MODALITIES as VALID_MODALITIES
from .session_state import _correction_scope as _correction_scope
from .session_state import _list_text as _list_text
from .session_state import _parse_time as _parse_time
from .session_state import build_companion_context as build_companion_context
from .session_state import normalize_observed_name as normalize_observed_name
from .session_state import public_correction_summary as public_correction_summary
from .session_state import remember_character_correction as remember_character_correction
from .session_state import resolve_character_candidate as resolve_character_candidate
from .session_state import resolve_character_candidates as resolve_character_candidates
from .session_state import sanitize_character_corrections as sanitize_character_corrections
from .session_state import session_is_active as session_is_active
from .session_state import start_session as start_session
from .session_state import stop_session as stop_session
from .session_state import utc_now_iso as utc_now_iso
from .vision import visual_fingerprint as visual_fingerprint

__all__ = [
    "DEFAULT_SESSION_TTL_SECONDS",
    "build_companion_context",
    "public_correction_summary",
    "remember_character_correction",
    "sanitize_character_corrections",
    "resolve_character_candidate",
    "resolve_character_candidates",
    "session_is_active",
    "start_session",
    "stop_session",
    "utc_now_iso",
    "visual_fingerprint",
    "visual_fingerprints_match",
]
