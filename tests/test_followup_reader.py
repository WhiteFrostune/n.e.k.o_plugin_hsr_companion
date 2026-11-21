"""Reader injection preserves the follow-up loop's original boundaries."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from plugin.plugins.hsr_companion.runtime import followup_delivery as delivery
from plugin.plugins.hsr_companion.runtime import followup_read, query_reuse
from plugin.plugins.hsr_companion.tests.test_page_followup import followup_owner, request


@pytest.mark.parametrize("raises", [False, True])
def test_recover_forwards_original_kwargs_and_failure(monkeypatch, raises):
    owner, result, plan = object(), object(), object()
    expected = {"navigation": True, "full": False, "plan": plan}
    calls = []

    async def original(value, **kwargs):
        calls.append((value, kwargs))
        if raises:
            raise RuntimeError("reader failure")
        return result

    monkeypatch.setattr(query_reuse, "try_reuse", original)
    if raises:
        with pytest.raises(RuntimeError, match="reader failure"):
            asyncio.run(followup_read.recover(owner, **expected))
    else:
        assert asyncio.run(followup_read.recover(owner, **expected)) is result
    assert calls == [(owner, expected)]


@pytest.mark.parametrize("raises", [False, True])
def test_validate_forwards_original_packet_and_failure(monkeypatch, raises):
    owner, packet, plan = object(), {}, object()
    calls = []

    async def original(value, evidence, **kwargs):
        calls.append((value, evidence, kwargs))
        if raises:
            raise RuntimeError("validation failure")
        return evidence

    monkeypatch.setattr(followup_read, "validated_delivery", original)
    if raises:
        with pytest.raises(RuntimeError, match="validation failure"):
            asyncio.run(followup_read.validate(owner, packet, plan=plan))
    else:
        assert asyncio.run(followup_read.validate(owner, packet, plan=plan)) is packet
    assert calls == [(owner, packet, {"plan": plan})]


def test_snapshot_forwards_the_same_fields(monkeypatch):
    owner, packet, result = object(), {}, object()
    fields = frozenset({"equipment"})
    calls = []

    def select(cls, value, evidence, **kwargs):
        calls.append((value, evidence, kwargs))
        return result

    monkeypatch.setattr(followup_read.ObservationSnapshot, "select", classmethod(select))
    assert followup_read.snapshot(owner, packet, fields=fields) is result
    assert calls == [(owner, packet, {"fields": fields})]


def test_explicit_reader_is_used_instead_of_legacy_default(monkeypatch):
    owner, sent = followup_owner(monkeypatch)
    calls = []
    original = followup_read.validated_delivery

    async def validate(*args, **kwargs):
        calls.append("validate")
        return await original(*args, **kwargs)

    def snapshot(*args, **kwargs):
        calls.append("snapshot")
        return followup_read.snapshot(*args, **kwargs)

    async def forbidden(*args, **kwargs):
        raise AssertionError("explicit reader must not use the default")

    monkeypatch.setattr(delivery, "DEFAULT_READER", SimpleNamespace(validate=forbidden))
    selected = SimpleNamespace(validate=validate, snapshot=snapshot)
    pending = replace(request(), reader=selected)
    before = pending.expires_at
    owner._pending_answer = pending
    asyncio.run(delivery.deliver_followup(owner, pending))
    assert calls == ["validate", "snapshot"]
    assert len(sent) == 1 and owner._followup_status["answer_complete"]
    assert pending.expires_at == before


def test_legacy_request_keeps_none_default_and_same_stateless_reader():
    assert request().reader is None
    assert delivery.DEFAULT_READER is followup_read.reader
