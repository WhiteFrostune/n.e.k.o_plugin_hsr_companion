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
]


@pytest.mark.parametrize(("legacy_module", "name", "canonical_module"), LEGACY_EXPORTS)
def test_legacy_import_exposes_canonical_object(legacy_module, name, canonical_module):
    legacy = import_module(f"{PACKAGE}.{legacy_module}")
    canonical = import_module(f"{PACKAGE}.{canonical_module}")
    assert getattr(legacy, name) is getattr(canonical, name)
