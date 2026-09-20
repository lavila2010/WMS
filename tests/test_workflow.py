import pytest

from app.constants import OrderStatus
from app.workflow import WorkflowError, can_transition, transition
from tests.conftest import make_order


def test_full_forward_path(db):
    order = make_order("SO-WF", lines=[("SKU-A", 1)])
    path = OrderStatus.ORDER
    for target in path[1:]:
        transition(order, target)
        db.session.commit()
    assert order.status == OrderStatus.CLOSED


def test_illegal_skip_transition_raises(db):
    order = make_order("SO-WF2", lines=[("SKU-A", 1)])
    with pytest.raises(WorkflowError):
        transition(order, OrderStatus.ALLOCATED)  # NEW -> ALLOCATED is illegal


def test_unknown_status_raises(db):
    order = make_order("SO-WF3", lines=[("SKU-A", 1)])
    with pytest.raises(WorkflowError):
        transition(order, "BOGUS")


def test_can_transition_matrix():
    assert can_transition(OrderStatus.NEW, OrderStatus.VALIDATED)
    assert not can_transition(OrderStatus.NEW, OrderStatus.PROCESSING)
    assert not can_transition(OrderStatus.CLOSED, OrderStatus.NEW)
