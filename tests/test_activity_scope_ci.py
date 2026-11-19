"""Activity reads are scoped to one bus; a timed-out read can be released."""
import asyncio
import threading
import time
from types import SimpleNamespace

from plugin.plugins.hsr_companion.runtime.chat_activity import latest_user_activity


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
