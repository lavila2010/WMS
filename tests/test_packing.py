import pytest

from app.constants import ExceptionType, OrderStatus, UnitStatus
from app.services.allocation import BarcodeError, allocate_barcode, mark_allocated
from app.services.packing import (
    close_box,
    close_order,
    create_box,
    mark_processed,
    mark_ready_to_close,
    reconcile,
    scan_into_box,
)
from app.workflow import transition
from tests.conftest import make_order, make_unit


@pytest.fixture()
def ready_order(db):
    """An order allocated and released to picking, with 2 SKU-A units."""
    order = make_order("SO-PACK", lines=[("SKU-A", 2)])
    transition(order, OrderStatus.VALIDATED)
    db.session.commit()
    make_unit("PK-1", "SKU-A")
    make_unit("PK-2", "SKU-A")
    make_unit("PK-3", "SKU-A")  # spare, not on demand after 2
    allocate_barcode(order, "PK-1")
    allocate_barcode(order, "PK-2")
    db.session.commit()
    mark_allocated(order)
    transition(order, OrderStatus.READY_TO_PICK)
    db.session.commit()
    return order


def test_full_packing_and_close(db, ready_order):
    order = ready_order
    box = create_box(order, "BOX-1", 30, 20, 15)
    db.session.commit()
    assert order.status == OrderStatus.PROCESSING

    scan_into_box(box, "PK-1")
    scan_into_box(box, "PK-2")
    db.session.commit()
    assert reconcile(order)["packed"] == 2

    close_box(box, 4.5)
    db.session.commit()
    assert box.status == "CLOSED" and box.weight_kg == 4.5

    mark_processed(order)
    db.session.commit()
    assert order.status == OrderStatus.PROCESSED

    mark_ready_to_close(order)
    db.session.commit()
    assert order.status == OrderStatus.READY_TO_CLOSE

    close_order(order)
    db.session.commit()
    assert order.status == OrderStatus.CLOSED
    assert all(a.unit.status == UnitStatus.SHIPPED for a in order.allocations)


def test_duplicate_scan_into_same_box_rejected(db, ready_order):
    order = ready_order
    box = create_box(order, "BOX-1")
    db.session.commit()
    scan_into_box(box, "PK-1")
    db.session.commit()
    with pytest.raises(BarcodeError) as exc:
        scan_into_box(box, "PK-1")
    assert exc.value.exc_type == ExceptionType.DUPLICATE_SCAN


def test_unit_cannot_be_in_two_boxes(db, ready_order):
    order = ready_order
    box1 = create_box(order, "BOX-1")
    box2 = create_box(order, "BOX-2")
    db.session.commit()
    scan_into_box(box1, "PK-1")
    db.session.commit()
    with pytest.raises(BarcodeError) as exc:
        scan_into_box(box2, "PK-1")
    assert exc.value.exc_type == ExceptionType.UNIT_ALREADY_BOXED


def test_scan_unallocated_unit_rejected(db, ready_order):
    order = ready_order
    box = create_box(order, "BOX-1")
    db.session.commit()
    with pytest.raises(BarcodeError) as exc:
        scan_into_box(box, "PK-3")  # never allocated to this order
    assert exc.value.exc_type == ExceptionType.WRONG_ORDER


def test_close_box_requires_weight_and_contents(db, ready_order):
    order = ready_order
    box = create_box(order, "BOX-1")
    db.session.commit()
    with pytest.raises(ValueError):
        close_box(box, 5)  # empty box
    scan_into_box(box, "PK-1")
    db.session.commit()
    with pytest.raises(ValueError):
        close_box(box, 0)  # non-positive weight


def test_close_blocked_until_reconciled(db, ready_order):
    order = ready_order
    box = create_box(order, "BOX-1")
    db.session.commit()
    scan_into_box(box, "PK-1")  # only 1 of 2 packed
    close_box(box, 2.0)
    db.session.commit()
    with pytest.raises(ValueError):
        mark_processed(order)
    assert not reconcile(order)["ok"]
