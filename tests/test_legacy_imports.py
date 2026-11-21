"""Compatibility paths retained while modules move between layers.

Append relocation entries during P2-P8. Each old path must expose the exact
canonical object, so constants and classes cannot diverge through copies.
"""

from importlib import import_module

import pytest

PACKAGE = "plugin.plugins.hsr_companion"

# (legacy module, exported name, canonical module)
LEGACY_EXPORTS = [
    # P1: planner vocabulary and demand declarations moved into contracts.
    ("runtime.query_plan", "TOPIC_WORDS", "runtime.contracts"),
    ("runtime.query_plan", "ENTITY_TOPICS", "runtime.contracts"),
    ("runtime.query_plan", "SCREEN_FIELDS", "runtime.contracts"),
    ("runtime.query_plan", "RESOURCE_LABELS", "runtime.contracts"),
    ("runtime.query_plan", "SEMANTIC_INTENTS", "runtime.contracts"),
    ("runtime.query_plan", "QueryPlan", "runtime.contracts"),
    ("runtime.consultation_contract", "CURRENT_SCOPES", "runtime.contracts"),
    ("runtime.consultation_contract", "CORRECTION_REASONS", "runtime.contracts"),
    # P4: the delivery facade preserves every pending-reply API name.
    ("runtime.pending_reply", "UserActivity", "runtime.pending_contract"),
    ("runtime.pending_reply", "message_digest", "runtime.pending_contract"),
    ("runtime.pending_reply", "activity_stamp", "runtime.pending_contract"),
    ("runtime.pending_reply", "message_allows", "runtime.pending_contract"),
    ("runtime.pending_reply", "relation_contract", "runtime.pending_contract"),
    ("runtime.pending_reply", "handle_pending_reply", "runtime.pending_handler"),
    # P5: observation facade and routing-rule compatibility paths.
    ("runtime.observation", "public_visual_evidence", "runtime.observation_evidence"),
    ("runtime.observation", "VISUAL_TTL_SECONDS", "runtime.observation_evidence"),
    ("runtime.observation", "_UNAVAILABLE", "runtime.observation_evidence"),
    ("runtime.observation", "visual_context_text", "runtime.context_text"),
    ("runtime.observation", "_SCOPE_LABELS", "runtime.context_text"),
    ("runtime.service_contract", "SERVICE_ROUTING_RULE", "runtime.context_text"),
    # P6: native-reader policy and session compatibility facades.
    ("adapters.text_reader", "ObservationSuperseded", "adapters.native_text"),
    ("adapters.text_reader", "QueryPreempted", "adapters.native_text"),
    ("adapters.text_reader", "check_current", "adapters.native_text"),
    ("adapters.text_reader", "_box_value", "adapters.native_text"),
    ("adapters.text_reader", "native_tiles", "adapters.native_text"),
    ("adapters.text_reader", "_deduplicate", "adapters.native_text"),
    ("adapters.text_reader", "split_label_geometry", "adapters.native_text"),
    ("adapters.text_reader", "read_native_text", "adapters.text_policy"),
    ("adapters.text_reader", "read_region", "adapters.native_text"),
    ("adapters.text_reader", "verify_name_regions", "adapters.text_verification"),
    ("session", "utc_now_iso", "session_state"),
    ("session", "_parse_time", "session_state"),
    ("session", "normalize_observed_name", "session_state"),
    ("session", "visual_fingerprint", "vision"),
    ("session", "visual_fingerprints_match", "runtime.contracts"),
    ("session", "session_is_active", "session_state"),
    ("session", "start_session", "session_state"),
    ("session", "stop_session", "session_state"),
    ("session", "_correction_scope", "session_state"),
    ("session", "sanitize_character_corrections", "session_state"),
    ("session", "remember_character_correction", "session_state"),
    ("session", "resolve_character_candidate", "session_state"),
    ("session", "resolve_character_candidates", "session_state"),
    ("session", "_list_text", "session_state"),
    ("session", "build_companion_context", "session_state"),
    ("session", "public_correction_summary", "session_state"),
    ("session", "SESSION_SCHEMA_VERSION", "session_state"),
    ("session", "DEFAULT_SESSION_TTL_SECONDS", "session_state"),
    ("session", "VALID_MODALITIES", "session_state"),
]


@pytest.mark.parametrize(("legacy_module", "name", "canonical_module"), LEGACY_EXPORTS)
def test_legacy_import_exposes_canonical_object(legacy_module, name, canonical_module):
    legacy = import_module(f"{PACKAGE}.{legacy_module}")
    canonical = import_module(f"{PACKAGE}.{canonical_module}")
    assert getattr(legacy, name) is getattr(canonical, name)
