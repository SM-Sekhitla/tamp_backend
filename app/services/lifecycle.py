"""Authoritative domain lifecycle policies.

Controllers remain responsible for identity, ownership and business evidence;
this module answers the narrower question: may an entity move from its current
state to the requested next state?
"""

from collections.abc import Mapping

from app.core.errors import require


LOAD_TRANSITIONS = {
    "DRAFT": {"POSTED", "CANCELLED"},
    "POSTED": {"DRAFT", "MATCHED", "CANCELLED", "EXPIRED"},
    "MATCHED": {"POSTED", "CONFIRMED", "CANCELLED"},
    "CONFIRMED": {"COMPLETED", "CANCELLED"},
    "COMPLETED": {"CLOSED"},
    "CANCELLED": {"DRAFT", "POSTED"},
    "EXPIRED": {"DRAFT", "POSTED", "CANCELLED"},
    "CLOSED": set(),
}

MATCH_TRANSITIONS = {
    "SUGGESTED": {"OFFERED", "ACCEPTED", "REJECTED", "WITHDRAWN", "EXPIRED"},
    "OFFERED": {"ACCEPTED", "REJECTED", "WITHDRAWN", "EXPIRED"},
    "ACCEPTED": {"CONFIRMED", "REJECTED", "WITHDRAWN", "EXPIRED"},
    "CONFIRMED": {"WITHDRAWN"},
    "REJECTED": set(),
    "WITHDRAWN": set(),
    "EXPIRED": set(),
}

TRIP_TRANSITIONS = {
    "SCHEDULED": {"EN_ROUTE_PICKUP", "CANCELLED"},
    "EN_ROUTE_PICKUP": {"AT_PICKUP", "CANCELLED"},
    "AT_PICKUP": {"LOADED", "CANCELLED"},
    "LOADED": {"EN_ROUTE_PICKUP", "IN_TRANSIT", "COMPLETED", "CANCELLED"},
    "IN_TRANSIT": {"AT_DROPOFF", "CANCELLED"},
    "AT_DROPOFF": {"DELIVERED", "CANCELLED"},
    "DELIVERED": {"EN_ROUTE_PICKUP", "IN_TRANSIT", "COMPLETED", "CANCELLED"},
    "COMPLETED": set(),
    "CANCELLED": set(),
}

DISPATCH_TRANSITIONS = {
    "OFFERED": {"ACCEPTED", "DECLINED", "CANCELLED"},
    "ACCEPTED": {"CANCELLED"},
    "DECLINED": set(),
    "CANCELLED": set(),
}

POLICIES: Mapping[str, Mapping[str, set[str]]] = {
    "load": LOAD_TRANSITIONS,
    "match": MATCH_TRANSITIONS,
    "trip": TRIP_TRANSITIONS,
    "dispatch": DISPATCH_TRANSITIONS,
}


def require_transition(entity: str, current: str, target: str) -> None:
    """Reject an undeclared state change with a stable conflict response."""

    policy = POLICIES[entity]
    require(
        target in policy.get(current, set()),
        f"{entity.title()} cannot change from {current} to {target}",
        409,
    )
