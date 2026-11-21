"""Read-side adapters used by the follow-up delivery loop (L2b)."""
from __future__ import annotations

from .live_refresh import validated_delivery
from .observation_store import ObservationSnapshot


def snapshot(owner, value: dict, *, fields=frozenset()):
    return ObservationSnapshot.select(owner, value, fields=fields)


async def recover(owner, **kwargs):
    # The original loop resolved this function at the call site as well.
    from .query_reuse import try_reuse
    return await try_reuse(owner, **kwargs)


async def validate(owner, evidence: dict, *, plan=None):
    return await validated_delivery(owner, evidence, plan=plan)


class FollowupReader:
    """Stateless callable bindings; no request, owner, timeout, or loop state."""
    recover = staticmethod(recover)
    validate = staticmethod(validate)
    snapshot = staticmethod(snapshot)


reader = FollowupReader()
