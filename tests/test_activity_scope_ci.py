"""Activity reads are scoped to one bus; a timed-out read can be released."""
import asyncio
import threading
import time
from types import SimpleNamespace

from plugin.plugins.hsr_companion.runtime.chat_activity import _state_for, latest_user_activity


def test_timed_out_read_does_not_block_the_next_question():
    release = threading.Event()
    calls = []

    def get(**_kwargs):
        calls.append(1)
        if len(calls) == 1:
            release.wait(2)
            return []
        return [{"type": "user_message", "lanlan": "cat", "_ts": time.time()}]

    bus = SimpleNamespace(memory=SimpleNamespace(get=get))

    async def run():
        first = await latest_user_activity(bus, target="cat", transport_timeout=.1, wait_timeout=.1)
        assert first == ("unavailable", None)
        second = await latest_user_activity(bus, target="cat", transport_timeout=.1, wait_timeout=.5,
                                            include_identity=True)
        assert second[0] == "available"
        assert len(calls) == 2
        release.set()

    asyncio.run(run())


def test_unhashable_bus_keeps_its_state_on_the_object():
    """A recycled object id must not inherit a dead bus's pending read."""
    bus = SimpleNamespace(memory=SimpleNamespace(get=lambda **_kw: []))
    state = _state_for(bus)
    assert _state_for(bus) is state
    assert bus.__dict__.get("_hsr_companion_activity_state") is state


def test_bus_that_cannot_store_state_never_shares_a_pending_read():
    class SlottedBus:
        __slots__ = ("memory",)

        def __init__(self, get):
            self.memory = SimpleNamespace(get=get)

    bus = SlottedBus(get=lambda **_kw: [])
    assert _state_for(bus) is not _state_for(bus)

