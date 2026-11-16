"""Collector privacy and correlation only; no runtime calls or model trials."""
from plugin.plugins.hsr_companion.scripts.record_recording_trial import (
    diagnostic_slice,
    epoch,
    matching_messages,
    new_request_ids,
)


def test_old_or_undated_requests_cannot_be_relabelled_as_new():
    start = epoch("2026-10-21T22:00:00+08:00")
    diag = {"service_request": {"request_id": "old", "started_at": "2026-10-21T22:01:00+08:00"},
            "answer_deliveries": [{"request_id": "older", "at": "2026-10-21T21:59:00+08:00"},
                                  {"request_id": "undated"},
                                  {"request_id": "new", "at": "2026-10-21T22:01:00+08:00"}]}
    assert new_request_ids(diag, {"old"}, start) == {"new"}


def test_only_matching_explicit_plugin_followup_content_is_saved():
    row = {"plugin_id": "hsr_companion", "message_id": "m", "content": "防御3335",
           "metadata": {"request_id": "new", "kind": "query_followup"}}
    result = matching_messages({"messages": [row, {**row, "plugin_id": "web_search"},
        {**row, "metadata": {"request_id": "old", "kind": "query_followup"}},
        {**row, "metadata": {"request_id": "new", "kind": "proactive"}}]}, {"new"})
    assert len(result) == 1 and result[0]["content"] == "防御3335"
    assert result[0]["exact_text_available"]
    assert result[0]["ack_scope"] == "public_message_record_not_model_consumption"


def test_oversized_or_non_text_body_is_not_reported_as_exact():
    for content in ("x" * 6001, {"not": "text"}, None):
        result = matching_messages({"messages": [{"plugin_id": "hsr_companion", "content": content,
            "metadata": {"request_id": "new", "kind": "query_followup"}}]}, {"new"})[0]
        assert result["content"] is None and not result["exact_text_available"]


def test_previous_delivery_body_is_not_copied_into_current_trial():
    selected = diagnostic_slice({"answer_deliveries": [{"request_id": "old"}, {"request_id": "new"}],
                                 "unrelated_chat": "not saved"}, {"new"})
    assert selected["answer_deliveries"] == [{"request_id": "new"}]
    assert "unrelated_chat" not in selected


def test_unzoned_or_invalid_timestamp_is_not_an_assumed_match():
    assert epoch("invalid") is None
    assert epoch("2026-10-21T22:00:00") is None
    assert epoch("2026-10-21T22:00:00+08:00") == epoch("2026-10-21T14:00:00Z")
