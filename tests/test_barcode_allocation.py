import pytest

from app.constants import ExceptionType, OrderStatus, UnitStatus
from app.models import OrderException
from app.services.allocation import (
    BarcodeError,
    allocate_barcode,
    is_fully_allocated,
    mark_allocated,
)
from app.workflow import transition
from tests.conftest import make_order, make_unit


@pytest.fixture()
def scenario(db):
    order = make_order("SO-100", lines=[("SKU-A", 2)])
    transition(order, OrderStatus.VALIDATED)
    db.session.commit()
    u1 = make_unit("BC-A1", "SKU-A")
    u2 = make_unit("BC-A2", "SKU-A")
    u3 = make_unit("BC-A3", "SKU-A")
    b1 = make_unit("BC-B1", "SKU-B")
    return {"order": order, "units": [u1, u2, u3], "b1": b1}


def test_allocate_moves_order_to_allocating(db, scenario):
    order = scenario["order"]
    alloc = allocate_barcode(order, "BC-A1")
    db.session.commit()
    assert alloc.status == "ACTIVE"
    assert order.status == OrderStatus.ALLOCATING
    assert scenario["units"][0].status == UnitStatus.ALLOCATED


def test_duplicate_scan_rejected(db, scenario):
    order = scenario["order"]
    allocate_barcode(order, "BC-A1")
    db.session.commit()
    with pytest.raises(BarcodeError) as exc:
        allocate_barcode(order, "BC-A1")
    assert exc.value.exc_type == ExceptionType.DUPLICATE_SCAN
    db.session.commit()
    assert OrderException.query.filter_by(type=ExceptionType.DUPLICATE_SCAN).count() == 1


def test_wrong_order_sku_rejected(db, scenario):
    order = scenario["order"]
    with pytest.raises(BarcodeError) as exc:
        allocate_barcode(order, "BC-B1")  # SKU-B not on order
    assert exc.value.exc_type == ExceptionType.WRONG_ORDER


def test_unknown_barcode_rejected(db, scenario):
    order = scenario["order"]
    with pytest.raises(BarcodeError) as exc:
        allocate_barcode(order, "DOES-NOT-EXIST")
    assert exc.value.exc_type == ExceptionType.UNKNOWN_BARCODE


def test_no_demand_when_line_full(db, scenario):
    order = scenario["order"]
    allocate_barcode(order, "BC-A1")
    allocate_barcode(order, "BC-A2")
    db.session.commit()
    assert is_fully_allocated(order)
    with pytest.raises(BarcodeError) as exc:
        allocate_barcode(order, "BC-A3")  # qty 2 already met
    assert exc.value.exc_type == ExceptionType.NO_DEMAND


def test_barcode_cannot_belong_to_two_active_orders(db, scenario):
    order = scenario["order"]
    allocate_barcode(order, "BC-A1")
    db.session.commit()

    order2 = make_order("SO-200", lines=[("SKU-A", 1)])
    transition(order2, OrderStatus.VALIDATED)
    db.session.commit()
    with pytest.raises(BarcodeError) as exc:
        allocate_barcode(order2, "BC-A1")
    assert exc.value.exc_type == ExceptionType.UNIT_IN_OTHER_ACTIVE_ORDER


def test_mark_allocated_requires_full(db, scenario):
    order = scenario["order"]
    allocate_barcode(order, "BC-A1")
    db.session.commit()
    with pytest.raises(BarcodeError):
        mark_allocated(order)  # only 1 of 2 allocated
    allocate_barcode(order, "BC-A2")
    db.session.commit()
    mark_allocated(order)
    db.session.commit()
    assert order.status == OrderStatus.ALLOCATED
