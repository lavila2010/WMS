import pytest

from app.constants import OrderStatus, UnitStatus
from app.models import Invoice
from app.services import invoices as invoices_service
from app.services.allocation import allocate_barcode, mark_allocated
from app.services.invoices import create_invoice_for_order, next_invoice_number
from app.services.packing import (
    close_box,
    close_order,
    create_box,
    mark_processed,
    mark_ready_to_close,
    scan_into_box,
)
from app.workflow import transition
from tests.conftest import make_order, make_unit


def _packed_ready_to_close(db, order_number="SO-INV"):
    order = make_order(order_number, lines=[("SKU-A", 2)])
    transition(order, OrderStatus.VALIDATED)
    db.session.commit()
    make_unit(f"{order_number}-1", "SKU-A")
    make_unit(f"{order_number}-2", "SKU-A")
    allocate_barcode(order, f"{order_number}-1")
    allocate_barcode(order, f"{order_number}-2")
    db.session.commit()
    mark_allocated(order)
    transition(order, OrderStatus.READY_TO_PICK)
    db.session.commit()
    box = create_box(order, "BOX-1", 30, 20, 10)
    scan_into_box(box, f"{order_number}-1")
    scan_into_box(box, f"{order_number}-2")
    close_box(box, 5.0)
    db.session.commit()
    mark_processed(order)
    mark_ready_to_close(order)
    db.session.commit()
    return order


def test_invoice_created_at_order_close(db):
    order = _packed_ready_to_close(db)
    _, invoice = close_order(order)
    db.session.commit()
    assert order.status == OrderStatus.CLOSED
    assert invoice.invoice_number.startswith("INV-")
    assert invoice.total_units == 2
    assert invoice.total_boxes == 1
    assert invoice.total_weight == 5.0
    assert Invoice.query.filter_by(order_id=order.id).count() == 1
    assert all(a.unit.status == UnitStatus.SHIPPED for a in order.allocations)


def test_only_one_invoice_per_order(db):
    order = _packed_ready_to_close(db)
    close_order(order)
    db.session.commit()
    with pytest.raises(ValueError):
        create_invoice_for_order(order)


def test_invoice_number_sequence_format(db):
    assert next_invoice_number(2026) == "INV-2026-000001"


def test_order_close_rolls_back_if_invoice_fails(db, monkeypatch):
    order = _packed_ready_to_close(db)

    def boom(*args, **kwargs):
        raise RuntimeError("invoice service failure")

    # close_order imports create_invoice_for_order from the invoices module.
    monkeypatch.setattr(invoices_service, "create_invoice_for_order", boom)

    with pytest.raises(RuntimeError):
        close_order(order)
    db.session.rollback()

    reloaded = db.session.get(type(order), order.id)
    assert reloaded.status == OrderStatus.READY_TO_CLOSE  # not CLOSED
    assert Invoice.query.filter_by(order_id=order.id).count() == 0
    assert all(u.status != UnitStatus.SHIPPED for u in reloaded.units)
