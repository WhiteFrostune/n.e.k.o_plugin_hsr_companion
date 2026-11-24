"""Bounded, read-only diagnosis independent from OCR and background completion."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone


async def diagnostic_inputs(owner, *, timeout=1.25, samples=None):
    """Read sources concurrently; failures are explicit, not cached success.

    wait_for bounds each async source. A synchronous implementation blocking
    the process loop or an upstream IPC queue remains outside this guarantee.
    """
    incomplete = []

    async def read(name, callback, fallback):
        started = time.perf_counter()
        sample = {"started_at": datetime.now(timezone.utc).isoformat(), "outcome": "returned"}
        try:
            return await asyncio.wait_for(callback(), timeout=timeout)
        except (TimeoutError, RuntimeError, OSError, ValueError) as exc:
            incomplete.append(name)
            sample.update(
                outcome="timeout" if isinstance(exc, TimeoutError) else "error", error_type=type(exc).__name__
            )
            return fallback
        finally:
            if isinstance(samples, dict):
                try:
                    samples[name] = {
                        **sample,
                        "finished_at": datetime.now(timezone.utc).isoformat(),
                        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                    }
                except Exception:
                    pass  # Diagnostic bookkeeping never replaces a source result/exception.

    assistant, audit, evidence = await asyncio.gather(
        read("assistant", owner._assistant_status, {"status_checked": False}),
        read("query_audit", owner._read_tool_audit, []),
        read("observation", owner._current_visual_evidence, {"fresh": False, "status_checked": False}),
    )
    return assistant, audit, evidence, sorted(incomplete)
