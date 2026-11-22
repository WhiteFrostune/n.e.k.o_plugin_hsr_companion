"""Read-side adapters used by the follow-up delivery loop (L2b)."""
from __future__ import annotations

from contextlib import contextmanager

from .live_refresh import validated_delivery
from .observation_store import ObservationSnapshot
from .vision_scheduler import ReviewDemand, review_demand


def snapshot(owner, value: dict, *, fields=frozenset()):
    return ObservationSnapshot.select(owner, value, fields=fields)


@contextmanager
def _review_scope(owner):
    request = getattr(owner, "_pending_answer", None)
    request_id = getattr(request, "request_id", None)
    if not request_id or not hasattr(request, "expires_at"):
        yield
        return
    demand = getattr(owner, "_followup_review_demand", None)
    if demand is None or demand[0] != request_id:
        demand = (request_id, ReviewDemand(request.expires_at, still_current=lambda:
            getattr(getattr(owner, "_pending_answer", None), "request_id", None) == request_id))
        owner._followup_review_demand = demand
    token = review_demand.set(demand[1])
    try:
        yield
    finally:
        review_demand.reset(token)


async def recover(owner, **kwargs):
    # The original loop resolved this function at the call site as well.
    from .query_reuse import try_reuse
    with _review_scope(owner):
        return await try_reuse(owner, **kwargs)


async def validate(owner, evidence: dict, *, plan=None):
    with _review_scope(owner):
        return await validated_delivery(owner, evidence, plan=plan)


class FollowupReader:
    """Stateless callable bindings; no request, owner, timeout, or loop state."""
    recover = staticmethod(recover)
    validate = staticmethod(validate)
    snapshot = staticmethod(snapshot)


reader = FollowupReader()
