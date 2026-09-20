import pytest

from app.constants import ExceptionType, OrderStatus
from app.models import Order
from app.services.allocation import BarcodeError, allocate_barcode
from app.workflow import transition
from tests.conftest import make_order, make_unit


def _validated_order(**kw):
    order = make_order(**kw)
    transition(order, OrderStatus.VALIDATED)
    from app.extensions import db

    db.session.commit()
    return order


def test_same_order_number_allowed_for_different_clients(db):
    o1 = make_order("SO-DUP", client="ACME", warehouse="WH1", order_type="B2C", lines=[("SKU-A", 1)])
    o2 = make_order("SO-DUP", client="GLOBEX", warehouse="WHX", order_type="B2C", lines=[("SKU-A", 1)])
    assert o1.id != o2.id
    assert Order.query.filter_by(order_number="SO-DUP").count() == 2


def test_wrong_client_barcode_rejected(db):
    order = _validated_order(order_number="SO-C", client="ACME", warehouse="WH1", order_type="B2C", lines=[("SKU-A", 1)])
    make_unit("WC-1", "SKU-A", client="GLOBEX", warehouse="WH1", order_type="B2C")
    with pytest.raises(BarcodeError) as exc:
        allocate_barcode(order, "WC-1")
    assert exc.value.exc_type == ExceptionType.WRONG_CLIENT


def test_wrong_warehouse_barcode_rejected(db):
    order = _validated_order(order_number="SO-W", client="ACME", warehouse="WH1", order_type="B2C", lines=[("SKU-A", 1)])
    make_unit("WW-1", "SKU-A", client="ACME", warehouse="WH2", order_type="B2C")
    with pytest.raises(BarcodeError) as exc:
        allocate_barcode(order, "WW-1")
    assert exc.value.exc_type == ExceptionType.WRONG_WAREHOUSE


def test_order_type_mismatch_does_not_reject_inventory(db):
    # Inventory has no order-type dimension: an order of ANY type draws from the
    # same client+warehouse pool. This must NOT be rejected.
    order = _validated_order(order_number="SO-T", client="ACME", warehouse="WH1", order_type="WHOLESALE", lines=[("SKU-A", 1)])
    make_unit("WT-1", "SKU-A", client="ACME", warehouse="WH1")
    alloc = allocate_barcode(order, "WT-1")
    db.session.commit()
    assert alloc.status == "ACTIVE"


def test_ecom_and_retail_orders_share_same_inventory_pool(db):
    make_unit("POOL-1", "SKU-A", client="ACME", warehouse="WH1")
    make_unit("POOL-2", "SKU-A", client="ACME", warehouse="WH1")
    ecom = _validated_order(order_number="SO-ECOM", client="ACME", warehouse="WH1", order_type="ECOM", lines=[("SKU-A", 1)])
    retail = _validated_order(order_number="SO-RETAIL", client="ACME", warehouse="WH1", order_type="RETAIL", lines=[("SKU-A", 1)])
    a1 = allocate_barcode(ecom, "POOL-1")
    a2 = allocate_barcode(retail, "POOL-2")
    db.session.commit()
    assert a1.order_id == ecom.id and a2.order_id == retail.id


def test_allocation_only_within_exact_scope(db):
    order = _validated_order(order_number="SO-OK", client="ACME", warehouse="WH1", order_type="B2C", lines=[("SKU-A", 1)])
    # exact-scope unit allocates
    make_unit("OK-1", "SKU-A", client="ACME", warehouse="WH1", order_type="B2C")
    alloc = allocate_barcode(order, "OK-1")
    db.session.commit()
    assert alloc.status == "ACTIVE"
    assert order.status == OrderStatus.ALLOCATING
