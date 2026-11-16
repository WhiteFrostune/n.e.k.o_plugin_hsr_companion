"""Production progress regressions; synthetic facts, no game or host messages."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from plugin.plugins.hsr_companion.runtime.fact_lifecycle import incomplete_same_page_read


def fact(label, value, **extra):
    return {"scope": "equipment", "kind": "sub_stat", "label": label, "value": value, **extra}


def candidate(rows):
    return {"page": {"page_type": "relic_detail"}, "screen_facts": {"facts": rows}, "characters": []}


def owner_for(rows):
    return SimpleNamespace(_committed_observation=candidate(rows), _assistant_client=object(),
                           _accepted_frame_delivery_guard=object())


@pytest.mark.parametrize("rows", [
    [fact("防御力", "9.7%"), fact("防御力", "69")],
    [fact("防御力", "4.8%"), fact("防御力", "74")],
])
def test_a_missing_row_cannot_hide_another_changed_same_label_stat(rows):
    owner = owner_for([fact("防御力", "4.8%"), fact("防御力", "69"), fact("速度", "4")])
    assert not incomplete_same_page_read(owner, candidate(rows))


def test_same_label_row_order_is_not_a_new_value_and_missing_rows_are_not_merged():
    original = [fact("防御力", "4.8%"), fact("防御力", "69"), fact("速度", "4")]
    owner = owner_for(original)
    incoming = candidate([original[1], original[0]])
    before = deepcopy(incoming)
    assert incomplete_same_page_read(owner, incoming)
    assert incoming == before
    assert owner._committed_observation == candidate(original)


@pytest.mark.parametrize("attribute,value", [("unit", "%"), ("property", "different"),
                                             ("subject_entity_id", "character.other")])
def test_fact_semantics_are_not_reduced_to_display_label_and_value(attribute, value):
    owner = owner_for([fact("防御力", "69"), fact("速度", "4")])
    assert not incomplete_same_page_read(owner, candidate([fact("防御力", "69", **{attribute: value})]))


def test_duplicate_rows_are_not_collapsed_into_one_dependency():
    owner = owner_for([fact("防御力", "69"), fact("速度", "4")])
    assert not incomplete_same_page_read(owner, candidate([fact("防御力", "69"), fact("防御力", "69")]))


def test_page_or_character_change_is_not_a_weak_same_object_update():
    owner = owner_for([fact("防御力", "69"), fact("速度", "4")])
    incoming = candidate([fact("防御力", "69")])
    incoming["page"] = {"page_type": "character_attributes"}
    assert not incomplete_same_page_read(owner, incoming)
    incoming["page"] = {"page_type": "relic_detail"}
    incoming["characters"] = [{"entity_id": "character.other", "identity_scope": "detail_subject"}]
    assert not incomplete_same_page_read(owner, incoming)


def test_changed_same_label_stat_reaches_the_real_commit_boundary(tmp_path):
    from plugin.plugins.hsr_companion.tests.test_chain_audit_20261017 import audit_owner, commit_raw, raw_page

    async def run():
        owner, _, sent = audit_owner(tmp_path)
        original = raw_page(subs=(("防御力", "4.8%"), ("防御力", "69"), ("速度", "4"), ("效果命中", "3.8%")))
        await commit_raw(owner, original)
        serial = owner._observation_serial
        changed = raw_page(subs=(("防御力", "9.7%"), ("防御力", "69")))
        await commit_raw(owner, changed)
        rows = owner._committed_observation["screen_facts"]["facts"]
        assert owner._observation_serial > serial
        assert any(row.get("value") == "9.7%" for row in rows)
        assert not any(row.get("value") in {"4.8%", "3.8%"} for row in rows)
        assert not any(message.get("ai_behavior") == "respond" for message in sent)

    asyncio.run(run())


def test_cache_review_reuses_frame_dimensions_without_decoding_on_event_loop(tmp_path, monkeypatch):
    import sys

    from PIL import Image
    from plugin.plugins.hsr_companion.tests import test_cached_page_controls_20261020 as cached_cases

    original = Image.open
    direct_decodes = []

    def inspected_open(*args, **kwargs):
        caller = sys._getframe(1)
        if caller.f_code.co_name == "review_snapshot":
            direct_decodes.append(caller.f_lineno)
        return original(*args, **kwargs)

    monkeypatch.setattr(Image, "open", inspected_open)
    cached_cases.test_cache_uses_same_proved_page_classifier_as_producer(
        tmp_path, "review", {"角色详情", "3335"}, True)
    assert not direct_decodes
