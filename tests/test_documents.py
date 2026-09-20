import os

import pytest

from app.constants import OrderStatus
from app.models import Document
from app.services.allocation import allocate_barcode, mark_allocated
from app.services.documents import (
    generate_box_detail,
    generate_order_closure,
    generate_packing_report,
    generate_pick_ticket,
)
from app.services.packing import close_box, create_box, scan_into_box
from app.workflow import transition
from tests.conftest import make_order, make_unit


@pytest.fixture()
def packed_order(db):
    order = make_order("SO-DOC", lines=[("SKU-A", 1)])
    transition(order, OrderStatus.VALIDATED)
    db.session.commit()
    make_unit("DOC-1", "SKU-A")
    allocate_barcode(order, "DOC-1")
    db.session.commit()
    mark_allocated(order)
    transition(order, OrderStatus.READY_TO_PICK)
    db.session.commit()
    box = create_box(order, "BOX-1", 10, 10, 10)
    scan_into_box(box, "DOC-1")
    close_box(box, 1.2)
    db.session.commit()
    return order, box


def _assert_pdf(doc):
    assert os.path.exists(doc.path)
    with open(doc.path, "rb") as fh:
        assert fh.read(5) == b"%PDF-"
    assert Document.query.get(doc.id) is not None


def test_generate_pick_ticket(db, packed_order):
    order, _ = packed_order
    _assert_pdf(generate_pick_ticket(order))


def test_generate_packing_report(db, packed_order):
    order, _ = packed_order
    _assert_pdf(generate_packing_report(order))


def test_generate_box_detail(db, packed_order):
    _, box = packed_order
    _assert_pdf(generate_box_detail(box))


def test_generate_order_closure(db, packed_order):
    order, _ = packed_order
    _assert_pdf(generate_order_closure(order))
