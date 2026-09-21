import io

import pandas as pd
import pytest

from app.constants import OrderStatus
from app.models import Order, OrderLine
from app.services.invariants import assert_invariants
from app.services.masters import create_client, create_division, create_warehouse, map_division_warehouse
from app.services.order_import import OrderImportError, analyze, commit_import


def _xlsx(rows):
    frame = pd.DataFrame(rows)
    buffer = io.BytesIO()
    frame.to_excel(buffer, index=False)
    buffer.seek(0)
    return buffer


def _line(order="1251", warehouse="NY", upc="UPC-A", qty=2, **extra):
    return {
        "Warehouse": warehouse,
        "OrderNumber": order,
        "Customer": extra.get("customer", "Ada"),
        "CustomerAddress": extra.get("address", "1 Main"),
        "CustomerPhone": extra.get("phone", "555"),
        "UPC": upc,
        "Qty": qty,
        "Carrier": extra.get("carrier", "UPS"),
        "ShippingService": extra.get("service", "Ground"),
    }


def _setup(db):
    celine = create_client("Celine", "CEL")
    dior = create_client("Dior", "DIO")
    db.session.commit()
    cel_ecom = create_division(celine, "Ecommerce", "ECOM")
    dio_ecom = create_division(dior, "Ecommerce", "ECOM")
    cel_ny = create_warehouse(celine, "NY")
    dio_ny = create_warehouse(dior, "NY")
    db.session.commit()
    map_division_warehouse(cel_ecom, cel_ny)
    map_division_warehouse(dio_ecom, dio_ny)
    db.session.commit()
    return celine, dior, cel_ecom, dio_ecom, cel_ny, dio_ny


def test_p3_01_multiline_consolidation(app, db, admin_user):
    celine, _, cel_ecom, _, _, _ = _setup(db)
    preview = analyze(
        _xlsx(
            [
                _line(upc="UPC-A", qty=2),
                _line(upc="UPC-B", qty=3),
                _line(upc="UPC-C", qty=1),
            ]
        ),
        "orders.xlsx",
        celine,
        cel_ecom,
    )
    assert preview["has_blocking"] is False
    assert preview["order_count"] == 1
    assert preview["total_units"] == 6
    commit_import(preview, user=admin_user)
    order = Order.query.one()
    assert order.wms_order_id == "01-CEL-1251-01"
    assert order.client_order_number == "1251"
    assert order.destination_sequence == 1
    assert order.status == OrderStatus.UNALLOCATED
    assert OrderLine.query.count() == 3
    assert sum(l.qty_ordered for l in order.lines) == 6
    assert_invariants()


def test_p3_02_same_raw_order_splits_by_destination(app, db, admin_user):
    celine, _, cel_ecom, _, _, _ = _setup(db)
    preview = analyze(
        _xlsx(
            [
                _line(upc="UPC-A", qty=1, customer="Ada", address="1 Main"),
                _line(upc="UPC-B", qty=1, customer="Other", address="2 Oak"),
            ]
        ),
        "split.xlsx",
        celine,
        cel_ecom,
    )
    assert preview["has_blocking"] is False
    assert preview["raw_order_count"] == 1
    assert preview["order_count"] == 2
    commit_import(preview, user=admin_user)
    orders = Order.query.order_by(Order.destination_sequence).all()
    assert [o.wms_order_id for o in orders] == ["01-CEL-1251-01", "01-CEL-1251-02"]
    assert {o.client_order_number for o in orders} == {"1251"}
    assert {o.customer for o in orders} == {"Ada", "Other"}


def test_p3_03_division_must_belong_to_client(app, db, admin_user):
    celine, dior, _, dio_ecom, _, _ = _setup(db)
    with pytest.raises(OrderImportError, match="does not belong"):
        from app.services.order_import import resolve_context

        resolve_context(admin_user, celine.id, dio_ecom.id)


def test_p3_04_warehouse_must_be_mapped(app, db, admin_user):
    celine, _, cel_ecom, _, _, _ = _setup(db)
    create_warehouse(celine, "NJ")
    db.session.commit()
    preview = analyze(_xlsx([_line(warehouse="NJ", qty=1)]), "nj.xlsx", celine, cel_ecom)
    assert preview["has_blocking"] is True
    assert any("not mapped" in e["message"] for e in preview["blocking"])
    with pytest.raises(OrderImportError):
        commit_import(preview, user=admin_user)
    assert Order.query.count() == 0


def test_p3_05_upc_mandatory(app, db, admin_user):
    celine, _, cel_ecom, _, _, _ = _setup(db)
    preview = analyze(_xlsx([_line(upc="", qty=1)]), "upc.xlsx", celine, cel_ecom)
    assert preview["has_blocking"] is True
    with pytest.raises(OrderImportError):
        commit_import(preview, user=admin_user)
    assert Order.query.count() == 0


def test_p3_06_duplicate_client_order_rejected(app, db, admin_user):
    celine, _, cel_ecom, _, _, _ = _setup(db)
    first = analyze(_xlsx([_line(qty=1)]), "a.xlsx", celine, cel_ecom)
    commit_import(first, user=admin_user)
    second = analyze(_xlsx([_line(qty=2, upc="UPC-Z")]), "b.xlsx", celine, cel_ecom)
    assert second["has_blocking"] is True
    with pytest.raises(OrderImportError):
        commit_import(second, user=admin_user)
    assert Order.query.count() == 1


def test_p3_07_same_number_two_clients(app, db, admin_user):
    celine, dior, cel_ecom, dio_ecom, _, _ = _setup(db)
    for client, division in ((celine, cel_ecom), (dior, dio_ecom)):
        preview = analyze(_xlsx([_line(qty=1)]), "x.xlsx", client, division)
        commit_import(preview, user=admin_user)
    ids = {o.wms_order_id for o in Order.query.all()}
    assert ids == {"01-CEL-1251-01", "02-DIO-1251-01"}
    assert Order.query.count() == 2
    assert_invariants()


def test_p3_08_injected_failure_leaves_batch_non_operational(app, db, admin_user):
    from app.constants import ImportBatchStatus
    from app.models import ImportBatch
    from app.services.allocation import AllocationError, allocate_order

    celine, _, cel_ecom, _, _, _ = _setup(db)
    preview = analyze(
        _xlsx([_line(order="100", upc="A", qty=1), _line(order="101", upc="B", qty=1)]),
        "two.xlsx",
        celine,
        cel_ecom,
    )
    with pytest.raises(OrderImportError, match="injected"):
        commit_import(preview, user=admin_user, chunk_size=1, _fail_after=1)
    batch = db.session.get(ImportBatch, preview["batch_id"])
    assert batch.status == ImportBatchStatus.FAILED
    assert Order.query.filter_by(import_batch_id=batch.id).count() == 1
    failed_order = Order.query.filter_by(import_batch_id=batch.id).one()
    with pytest.raises(AllocationError, match="not operational"):
        allocate_order(failed_order, user=admin_user)


def test_p3_09_wms_order_id_unique_derived(app, db, admin_user):
    celine, _, cel_ecom, _, _, _ = _setup(db)
    preview = analyze(_xlsx([_line(order="2001", qty=1)]), "id.xlsx", celine, cel_ecom)
    assert preview["orders"][0]["wms_order_id"] == "01-CEL-2001-01"
    commit_import(preview, user=admin_user)
    order = Order.query.one()
    assert order.wms_order_id == "01-CEL-2001-01"
    assert order.client_order_number == "2001"
    assert order.destination_sequence == 1
    assert Order.query.filter_by(wms_order_id="01-CEL-2001-01").count() == 1
