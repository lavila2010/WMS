import threading

import pytest

from app.constants import AllocationStatus, LedgerType, OrderStatus, UnitStatus
from app.models import Allocation, InventoryTransaction, InventoryUnit, Order, OrderLine
from app.services.allocation import AllocationError, allocate_order
from app.services.inventory_ledger import create_available_unit
from app.services.invariants import assert_invariants
from app.services.masters import create_client, create_division, create_warehouse, map_division_warehouse
from app.services.order_import import analyze, commit_import
from tests.test_v2_phase03_orders import _line, _xlsx


def _world(db):
    celine = create_client("Celine", "CEL")
    dior = create_client("Dior", "DIO")
    db.session.commit()
    cel_ecom = create_division(celine, "Ecom", "ECOM")
    dio_ecom = create_division(dior, "Ecom", "ECOM")
    cel_ny = create_warehouse(celine, "NY")
    cel_nj = create_warehouse(celine, "NJ")
    dio_ny = create_warehouse(dior, "NY")
    db.session.commit()
    map_division_warehouse(cel_ecom, cel_ny)
    map_division_warehouse(cel_ecom, cel_nj)
    map_division_warehouse(dio_ecom, dio_ny)
    db.session.commit()
    return {
        "celine": celine,
        "dior": dior,
        "cel_ecom": cel_ecom,
        "dio_ecom": dio_ecom,
        "cel_ny": cel_ny,
        "cel_nj": cel_nj,
        "dio_ny": dio_ny,
    }


def _import_order(user, client, division, rows):
    preview = analyze(_xlsx(rows), "o.xlsx", client, division)
    assert not preview["has_blocking"], preview["blocking"]
    commit_import(preview, user=user)
    return Order.query.filter_by(client_id=client.id, client_order_number=rows[0]["OrderNumber"]).one()


def _stock(client, warehouse, upc, qty, location="A-01", sku="SKU"):
    for _ in range(qty):
        create_available_unit(
            client_id=client.id,
            warehouse_id=warehouse.id,
            upc=upc,
            location=location,
            sku=sku,
        )
    db_commit()


def db_commit():
    from app.extensions import db

    db.session.commit()


def test_p4_01_same_client_only(app, db, admin_user):
    w = _world(db)
    _stock(w["dior"], w["dio_ny"], "UPC-A", 3)
    order = _import_order(admin_user, w["celine"], w["cel_ecom"], [_line(upc="UPC-A", qty=2)])
    result = allocate_order(order, user=admin_user)
    assert result["reserved"] == 0
    assert Order.query.get(order.id).status == OrderStatus.UNALLOCATED
    assert InventoryUnit.query.filter_by(status=UnitStatus.RESERVED).count() == 0


def test_p4_02_same_warehouse_only(app, db, admin_user):
    w = _world(db)
    _stock(w["celine"], w["cel_nj"], "UPC-A", 3)
    order = _import_order(admin_user, w["celine"], w["cel_ecom"], [_line(upc="UPC-A", qty=2)])
    result = allocate_order(order, user=admin_user)
    assert result["reserved"] == 0
    assert InventoryUnit.query.filter_by(warehouse_id=w["cel_nj"].id, status=UnitStatus.AVAILABLE).count() == 3


def test_p4_03_upc_match_ignores_sku_difference(app, db, admin_user):
    w = _world(db)
    _stock(w["celine"], w["cel_ny"], "UPC-A", 2, sku="OTHER")
    order = _import_order(
        admin_user,
        w["celine"],
        w["cel_ecom"],
        [_line(upc="UPC-A", qty=2)],
    )
    # line sku defaults empty; inventory sku OTHER — still allocates on UPC
    result = allocate_order(order, user=admin_user)
    assert result["reserved"] == 2
    assert Order.query.get(order.id).status == OrderStatus.ALLOCATED


def test_p4_04_never_match_sku_when_upc_differs(app, db, admin_user):
    w = _world(db)
    _stock(w["celine"], w["cel_ny"], "UPC-B", 5, sku="SKU-1")
    order = _import_order(admin_user, w["celine"], w["cel_ecom"], [_line(upc="UPC-A", qty=2)])
    result = allocate_order(order, user=admin_user)
    assert result["reserved"] == 0
    assert InventoryUnit.query.filter_by(upc="UPC-B", status=UnitStatus.AVAILABLE).count() == 5


def test_p4_05_partial_full_zero(app, db, admin_user):
    w = _world(db)
    _stock(w["celine"], w["cel_ny"], "UPC-A", 1)
    zero = _import_order(admin_user, w["celine"], w["cel_ecom"], [_line(order="Z", upc="NONE", qty=1)])
    assert allocate_order(zero, user=admin_user)["status"] == OrderStatus.UNALLOCATED
    partial = _import_order(admin_user, w["celine"], w["cel_ecom"], [_line(order="P", upc="UPC-A", qty=3)])
    assert allocate_order(partial, user=admin_user)["status"] == OrderStatus.PARTIALLY_ALLOCATED
    _stock(w["celine"], w["cel_ny"], "UPC-A", 2)
    full = _import_order(admin_user, w["celine"], w["cel_ecom"], [_line(order="F", upc="UPC-A", qty=2)])
    assert allocate_order(full, user=admin_user)["status"] == OrderStatus.ALLOCATED
    assert_invariants()


def test_p4_06_concurrent_orders_cannot_share_unit(app, db, admin_user):
    w = _world(db)
    _stock(w["celine"], w["cel_ny"], "UPC-A", 1)
    a = _import_order(admin_user, w["celine"], w["cel_ecom"], [_line(order="C1", upc="UPC-A", qty=1)])
    b = _import_order(admin_user, w["celine"], w["cel_ecom"], [_line(order="C2", upc="UPC-A", qty=1)])
    results = []

    def worker(oid):
        with app.app_context():
            order = db.session.get(Order, oid)
            try:
                results.append(allocate_order(order, user=admin_user))
            except Exception as exc:  # noqa: BLE001
                db.session.rollback()
                results.append({"error": str(exc), "reserved": 0})

    threads = [threading.Thread(target=worker, args=(a.id,)), threading.Thread(target=worker, args=(b.id,))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    reserved = [r.get("reserved", 0) for r in results]
    assert sorted(reserved) == [0, 1]
    assert InventoryUnit.query.filter_by(status=UnitStatus.RESERVED).count() == 1
    assert Allocation.query.filter_by(status=AllocationStatus.ACTIVE).count() == 1


def test_p4_07_reserve_ledger_and_qty_in_one_commit(app, db, admin_user):
    w = _world(db)
    _stock(w["celine"], w["cel_ny"], "UPC-A", 2)
    order = _import_order(admin_user, w["celine"], w["cel_ecom"], [_line(upc="UPC-A", qty=2)])
    allocate_order(order, user=admin_user)
    line = OrderLine.query.filter_by(order_id=order.id).one()
    assert line.qty_allocated == 2
    assert InventoryTransaction.query.filter_by(transaction_type=LedgerType.RESERVE).count() == 2
    assert Allocation.query.filter_by(order_id=order.id, status=AllocationStatus.ACTIVE).count() == 2
    assert_invariants()


def test_p4_08_exception_rolls_back_reservations(app, db, admin_user):
    w = _world(db)
    _stock(w["celine"], w["cel_ny"], "UPC-A", 3)
    order = _import_order(admin_user, w["celine"], w["cel_ecom"], [_line(upc="UPC-A", qty=3)])
    with pytest.raises(Exception, match="injected"):
        allocate_order(order, user=admin_user, _fail_after=1)
    assert InventoryUnit.query.filter_by(status=UnitStatus.RESERVED).count() == 0
    assert Allocation.query.count() == 0
    assert InventoryTransaction.query.filter_by(transaction_type=LedgerType.RESERVE).count() == 0
    assert Order.query.get(order.id).status == OrderStatus.UNALLOCATED
    assert OrderLine.query.filter_by(order_id=order.id).one().qty_allocated == 0
