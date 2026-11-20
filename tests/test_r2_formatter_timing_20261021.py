"""Diagnostic only: preserve timing evidence for the old two-second fixture."""
import json
import time

from plugin.plugins.hsr_companion.runtime import answer_message
from plugin.plugins.hsr_companion.tests import test_delivery_chain_20261017 as original


def test_old_scoped_followup_records_cold_formatter_time(tmp_path, monkeypatch, record_property):
    calls = []
    preview = answer_message.bridge_preview

    def measured(text):
        started = time.monotonic()
        try:
            return preview(text)
        finally:
            calls.append(round(time.monotonic() - started, 3))

    monkeypatch.setattr(answer_message, "bridge_preview", measured)
    try:
        original.test_followup_uses_scoped_guard_and_only_submits_requested_facts(tmp_path, monkeypatch)
    finally:
        record_property("formatter_seconds", json.dumps(calls))
