"""Bounded, read-only diagnosis independent from OCR and background completion."""
from __future__ import annotations

import asyncio


async def diagnostic_inputs(owner, *, timeout=1.25):
    """Read sources concurrently; failures are explicit, not cached success.

    wait_for bounds each async source. A synchronous implementation blocking
    the process loop or an upstream IPC queue remains outside this guarantee.
    """
    incomplete = []

    async def read(name, callback, fallback):
        try:
            return await asyncio.wait_for(callback(), timeout=timeout)
        except (TimeoutError, RuntimeError, OSError, ValueError):
            incomplete.append(name)
            return fallback

    assistant, audit, evidence = await asyncio.gather(
        read("assistant", owner._assistant_status, {"status_checked": False}),
        read("query_audit", owner._read_tool_audit, []),
        read("observation", owner._current_visual_evidence, {"fresh": False, "status_checked": False}),
    )
    return assistant, audit, evidence, sorted(incomplete)
