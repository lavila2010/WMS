"""Order workflow state machine.

Enforces the approved lifecycle:

    NEW -> VALIDATED -> ALLOCATING -> ALLOCATED -> READY_TO_PICK
        -> PROCESSING -> PROCESSED -> READY_TO_CLOSE -> CLOSED
"""

from __future__ import annotations

from .constants import ORDER_TRANSITIONS, OrderStatus
from .extensions import db
from .models import Order, Transaction


class WorkflowError(Exception):
    """Raised when an invalid order status transition is attempted."""


def can_transition(current: str, target: str) -> bool:
    return target in ORDER_TRANSITIONS.get(current, set())


def transition(order: Order, target: str, detail: str | None = None) -> Order:
    """Move ``order`` to ``target`` status if the transition is allowed."""
    if target not in OrderStatus.ORDER:
        raise WorkflowError(f"Unknown order status: {target}")
    if not can_transition(order.status, target):
        raise WorkflowError(
            f"Illegal transition for order {order.order_number}: "
            f"{order.status} -> {target}"
        )
    previous = order.status
    order.status = target
    db.session.add(
        Transaction(
            type="ORDER_STATUS_CHANGE",
            order_id=order.id,
            detail=detail or f"{previous} -> {target}",
        )
    )
    db.session.flush()
    return order
