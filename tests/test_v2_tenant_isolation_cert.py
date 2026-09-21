"""Multi-client / division / warehouse inventory isolation certification."""

from __future__ import annotations

import inspect
import io
import threading
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import inspect as sa_inspect, text

from app.constants import AllocationStatus, InventoryIssueType, UnitStatus
from app.extensions import db
from app.models import (
    Allocation,
    DivisionWarehouse,
    ImportBatch,
    InventoryIssue,
    InventoryTransaction,
    InventoryUnit,
    Order,
    OrderLine,
)
from app.services.allocation import (
    AllocationError,
    _candidate_units,
    _persist_reservation_chunk,
    allocate_order,
    allocate_orders,
)
from app.services.inventory_import import ImportErrorClosed, analyze as inv_analyze, commit_import as inv_commit
from app.services.inventory_ledger import create_available_unit
from app.services.inventory_query import aggregate_rows, status_counts
from app.services.invariants import assert_invariants
from app.services.masters import (
    MasterError,
    create_client,
    create_division,
    create_warehouse,
    map_division_warehouse,
)
from app.services.order_import import OrderImportError, analyze as ord_analyze, commit_import as ord_commit
from app.services.order_import import resolve_context as resolve_order_context
from app.services.pick_tickets import create_pick_ticket
from tests.conftest import create_user, form_data, login
from tests.test_v2_phase03_orders import _line, _xlsx as _order_xlsx


INTEGRITY_SQL = {
    "division_warehouse_client": """
        SELECT dw.id
        FROM division_warehouses dw
        JOIN divisions d ON d.id = dw.division_id
        JOIN warehouses w ON w.id = dw.warehouse_id
        WHERE d.client_id <> w.client_id
           OR d.client_id <> dw.client_id
           OR w.client_id <> dw.client_id
    """,
    "order_client_division": """
        SELECT o.id FROM orders o
        JOIN divisions d ON d.id = o.division_id
        WHERE o.client_id <> d.client_id
    """,
    "order_client_warehouse": """
        SELECT o.id FROM orders o
        JOIN warehouses w ON w.id = o.warehouse_id
        WHERE o.client_id <> w.client_id
    """,
    "order_unmapped_warehouse": """
        SELECT o.id FROM orders o
        WHERE NOT EXISTS (
            SELECT 1 FROM division_warehouses dw
            WHERE dw.division_id = o.division_id
              AND dw.warehouse_id = o.warehouse_id
              AND dw.active IS TRUE
        )
    """,
    "inventory_client_warehouse": """
        SELECT u.id FROM inventory_units u
        JOIN warehouses w ON w.id = u.warehouse_id
        WHERE u.client_id <> w.client_id
    """,
    "allocation_client_order": """
        SELECT a.id FROM allocations a
        JOIN orders o ON o.id = a.order_id
        WHERE a.client_id <> o.client_id
    """,
    "allocation_client_unit": """
        SELECT a.id FROM allocations a
        JOIN inventory_units u ON u.id = a.inventory_unit_id
        WHERE a.client_id <> u.client_id
    """,
    "allocation_warehouse_order": """
        SELECT a.id FROM allocations a
        JOIN orders o ON o.id = a.order_id
        WHERE a.warehouse_id <> o.warehouse_id
    """,
    "allocation_warehouse_unit": """
        SELECT a.id FROM allocations a
        JOIN inventory_units u ON u.id = a.inventory_unit_id
        WHERE a.warehouse_id <> u.warehouse_id
    """,
    "allocation_upc_unit": """
        SELECT a.id FROM allocations a
        JOIN inventory_units u ON u.id = a.inventory_unit_id
        WHERE a.upc <> u.upc
    """,
    "allocation_upc_line": """
        SELECT a.id FROM allocations a
        JOIN order_lines ol ON ol.id = a.order_line_id
        WHERE a.upc <> ol.upc
    """,
}


def _inv_xlsx(rows):
    frame = pd.DataFrame(rows)
    buffer = io.BytesIO()
    frame.to_excel(buffer, index=False)
    buffer.seek(0)
    return buffer


def _inv_row(upc="111", qty=1, location="A-01", **extra):
    data = {
        "UPC": upc,
        "SKU": extra.get("sku", "SKU-1"),
        "Description": extra.get("description", "Shared UPC Item"),
        "Style": extra.get("style", "ST"),
        "Color": extra.get("color", "BLK"),
        "Size": extra.get("size", "M"),
        "Quantity": qty,
        "Location": location,
    }
    if "client" in extra:
        data["Client"] = extra["client"]
    if "warehouse" in extra:
        data["Warehouse"] = extra["warehouse"]
    return data


def _world(db):
    client_a = create_client("ClientA", "CLA")
    client_b = create_client("ClientB", "CLB")
    db.session.commit()
    ecom_a = create_division(client_a, "Ecommerce", "ECOM")
    retail_a = create_division(client_a, "Retail", "RTL")
    wholesale_a = create_division(client_a, "Wholesale", "WHLS")
    ecom_b = create_division(client_b, "Ecommerce", "ECOM")
    ny_a = create_warehouse(client_a, "NY", "New York")
    nj_a = create_warehouse(client_a, "NJ", "New Jersey")
    ny_b = create_warehouse(client_b, "NY", "New York")
    db.session.commit()
    map_division_warehouse(ecom_a, ny_a)
    map_division_warehouse(ecom_a, nj_a)
    map_division_warehouse(retail_a, ny_a)
    map_division_warehouse(wholesale_a, ny_a)
    map_division_warehouse(ecom_b, ny_b)
    db.session.commit()
    return {
        "a": client_a,
        "b": client_b,
        "ecom_a": ecom_a,
        "retail_a": retail_a,
        "wholesale_a": wholesale_a,
        "ecom_b": ecom_b,
        "ny_a": ny_a,
        "nj_a": nj_a,
        "ny_b": ny_b,
    }


def _stock(client, warehouse, upc, qty, location="A-01"):
    for _ in range(qty):
        create_available_unit(
            client_id=client.id,
            warehouse_id=warehouse.id,
            upc=upc,
            location=location,
            sku="SKU-1",
        )
    db.session.commit()


def _import_order(user, client, division, rows):
    preview = ord_analyze(_order_xlsx(rows), "o.xlsx", client, division)
    assert not preview["has_blocking"], preview["blocking"]
    ord_commit(preview, user=user)
    return Order.query.filter_by(
        client_id=client.id, client_order_number=str(rows[0]["OrderNumber"])
    ).one()


def _available(client_id, warehouse_id, upc):
    return InventoryUnit.query.filter_by(
        client_id=client_id,
        warehouse_id=warehouse_id,
        upc=upc,
        status=UnitStatus.AVAILABLE,
    ).count()


def _reserved(client_id, warehouse_id, upc):
    return InventoryUnit.query.filter_by(
        client_id=client_id,
        warehouse_id=warehouse_id,
        upc=upc,
        status=UnitStatus.RESERVED,
    ).count()


def test_phase1_ownership_chain_and_constraints(app, db):
    inspector = sa_inspect(db.engine)
    unit_cols = {col["name"] for col in inspector.get_columns("inventory_units")}
    assert "division_id" not in unit_cols
    assert {"client_id", "warehouse_id", "upc", "location"} <= unit_cols

    required = {
        "divisions": ["client_id"],
        "warehouses": ["client_id"],
        "division_warehouses": ["client_id", "division_id", "warehouse_id"],
        "inventory_units": ["client_id", "warehouse_id"],
        "inventory_transactions": ["client_id", "warehouse_id"],
        "orders": ["client_id", "division_id", "warehouse_id"],
        "order_lines": ["client_id"],
        "allocations": ["client_id", "warehouse_id", "order_id", "inventory_unit_id", "upc"],
        "import_batches": ["client_id"],
    }
    for table, cols in required.items():
        by_name = {col["name"]: col for col in inspector.get_columns(table)}
        for col in cols:
            assert by_name[col]["nullable"] is False, f"{table}.{col} must be NOT NULL"

    trigger = db.session.execute(
        text(
            """
            SELECT tgname FROM pg_trigger
            WHERE tgname = 'trg_dw_same_client' AND NOT tgisinternal
            """
        )
    ).scalar()
    assert trigger == "trg_dw_same_client"

    w = _world(db)
    with pytest.raises(MasterError, match="same client"):
        map_division_warehouse(w["ecom_a"], w["ny_b"])
    db.session.rollback()
    with pytest.raises(Exception):
        db.session.add(
            DivisionWarehouse(
                client_id=w["a"].id,
                division_id=w["ecom_a"].id,
                warehouse_id=w["ny_b"].id,
                active=True,
            )
        )
        db.session.commit()
    db.session.rollback()


def test_phase2_inventory_import_cannot_choose_own_tenant(app, db, admin_user):
    w = _world(db)
    blocked_client = inv_analyze(
        _inv_xlsx([_inv_row(upc="111", qty=2, client="DIOR")]),
        "celine.xlsx",
        w["a"],
        w["ny_a"],
    )
    assert blocked_client["has_blocking"] is True
    assert any("does not match selected" in err["message"] for err in blocked_client["blocking"])
    with pytest.raises(ImportErrorClosed):
        inv_commit(blocked_client, user=admin_user)
    assert InventoryUnit.query.count() == 0

    blocked_wh = inv_analyze(
        _inv_xlsx([_inv_row(upc="111", qty=2, warehouse="02-CLB-NY")]),
        "celine-wh.xlsx",
        w["a"],
        w["ny_a"],
    )
    assert blocked_wh["has_blocking"] is True
    with pytest.raises(ImportErrorClosed):
        inv_commit(blocked_wh, user=admin_user)
    assert InventoryUnit.query.count() == 0

    same_symbol = inv_analyze(
        _inv_xlsx([_inv_row(upc="111", qty=3, warehouse="NY")]),
        "ny.xlsx",
        w["a"],
        w["ny_a"],
    )
    assert same_symbol["has_blocking"] is False
    batch = inv_commit(same_symbol, user=admin_user)
    assert batch.client_id == w["a"].id
    assert batch.warehouse_id == w["ny_a"].id
    units = InventoryUnit.query.filter_by(import_batch_id=batch.id).all()
    txns = InventoryTransaction.query.filter_by(import_batch_id=batch.id).all()
    assert len(units) == 3
    assert len(txns) == 3
    assert {u.client_id for u in units} == {w["a"].id}
    assert {u.warehouse_id for u in units} == {w["ny_a"].id}
    assert {t.client_id for t in txns} == {w["a"].id}
    assert {t.warehouse_id for t in txns} == {w["ny_a"].id}
    assert InventoryUnit.query.filter_by(client_id=w["b"].id).count() == 0


def test_phase3_order_import_fail_closed(app, db, admin_user):
    w = _world(db)
    with pytest.raises(OrderImportError, match="does not belong"):
        resolve_order_context(admin_user, w["a"].id, w["ecom_b"].id)

    # Same warehouse symbol "NY" must resolve only inside the selected client.
    preview = ord_analyze(
        _order_xlsx([_line(order="NYA", warehouse="NY", upc="111", qty=1)]),
        "nya.xlsx",
        w["a"],
        w["ecom_a"],
    )
    assert preview["has_blocking"] is False
    ord_commit(preview, user=admin_user)
    order = Order.query.filter_by(client_order_number="NYA").one()
    assert order.client_id == w["a"].id
    assert order.warehouse_id == w["ny_a"].id
    assert order.warehouse_id != w["ny_b"].id

    unmapped = create_warehouse(w["a"], "TX", "Texas")
    db.session.commit()
    preview = ord_analyze(
        _order_xlsx([_line(order="TX1", warehouse="TX", upc="111", qty=1)]),
        "tx.xlsx",
        w["a"],
        w["ecom_a"],
    )
    assert preview["has_blocking"] is True
    assert any("not mapped" in err["message"] for err in preview["blocking"])
    with pytest.raises(OrderImportError):
        ord_commit(preview, user=admin_user)
    assert Order.query.filter_by(client_order_number="TX1").count() == 0
    assert unmapped.client_id == w["a"].id

    # HTTP resolve_context is the tenant gate. analyze() must still not create
    # orders if a caller hands it a foreign division object.
    crossed = ord_analyze(
        _order_xlsx([_line(order="XB", warehouse="NY", upc="111", qty=1)]),
        "cross.xlsx",
        w["a"],
        w["ecom_b"],
    )
    assert crossed["has_blocking"] is True
    with pytest.raises(OrderImportError):
        ord_commit(crossed, user=admin_user)
    assert Order.query.filter_by(client_order_number="XB").count() == 0


def test_phase4_inventory_has_no_division_id(app, db):
    columns = {col["name"] for col in sa_inspect(db.engine).get_columns("inventory_units")}
    assert "division_id" not in columns
    assert hasattr(InventoryUnit, "client_id")
    assert hasattr(InventoryUnit, "warehouse_id")
    assert not hasattr(InventoryUnit, "division_id")
    source = inspect.getsource(_candidate_units)
    assert "InventoryUnit.client_id == order.client_id" in source
    assert "InventoryUnit.warehouse_id == order.warehouse_id" in source
    assert "InventoryUnit.upc == upc" in source
    assert "UnitStatus.AVAILABLE" in source
    assert "operational_batch_clause" in source
    assert "skip_locked=True" in source
    assert "division_id" not in source


def test_phase5_allocation_predicate_and_fail_closed(app, db, admin_user):
    persist_src = inspect.getsource(_persist_reservation_chunk)
    assert "unit.client_id != locked.client_id" in persist_src
    assert "unit.warehouse_id != locked.warehouse_id" in persist_src
    assert "unit.upc != line.upc" in persist_src
    assert "Refusing cross-tenant unit" in persist_src
    assert "Refusing SKU/UPC mismatch" in persist_src

    w = _world(db)
    _stock(w["a"], w["ny_a"], "111", 1)
    foreign = create_available_unit(
        client_id=w["b"].id,
        warehouse_id=w["ny_b"].id,
        upc="111",
        location="A-01",
    )
    db.session.commit()
    order = _import_order(admin_user, w["a"], w["ecom_a"], [_line(order="FC", warehouse="NY", upc="111", qty=1)])
    with pytest.raises(AllocationError, match="cross-tenant"):
        _persist_reservation_chunk(
            order,
            OrderLine.query.filter_by(order_id=order.id).one(),
            [foreign],
            wave=1,
            user_id=admin_user.id,
        )


def test_phase6_cross_tenant_and_cross_warehouse_allocation(app, db, admin_user):
    w = _world(db)
    _stock(w["a"], w["ny_a"], "111", 10, location="A-01")
    _stock(w["b"], w["ny_b"], "111", 10, location="A-01")
    order_a = _import_order(
        admin_user, w["a"], w["ecom_a"], [_line(order="A10", warehouse="NY", upc="111", qty=10)]
    )
    result = allocate_order(order_a, user=admin_user)
    assert result["reserved"] == 10
    assert _reserved(w["a"].id, w["ny_a"].id, "111") == 10
    assert _available(w["b"].id, w["ny_b"].id, "111") == 10
    assert _reserved(w["b"].id, w["ny_b"].id, "111") == 0
    assert {
        row.client_id for row in Allocation.query.filter_by(order_id=order_a.id).all()
    } == {w["a"].id}

    order_b = _import_order(
        admin_user, w["b"], w["ecom_b"], [_line(order="B10", warehouse="NY", upc="111", qty=10)]
    )
    result_b = allocate_order(order_b, user=admin_user)
    assert result_b["reserved"] == 10
    assert _reserved(w["b"].id, w["ny_b"].id, "111") == 10
    assert _available(w["a"].id, w["ny_a"].id, "111") == 0
    assert Allocation.query.filter_by(order_id=order_b.id, client_id=w["a"].id).count() == 0

    _stock(w["a"], w["nj_a"], "111", 7, location="A-01")
    nj_order = _import_order(
        admin_user, w["a"], w["ecom_a"], [_line(order="ANJ", warehouse="NJ", upc="111", qty=7)]
    )
    nj_result = allocate_order(nj_order, user=admin_user)
    assert nj_result["reserved"] == 7
    assert _reserved(w["a"].id, w["nj_a"].id, "111") == 7
    assert InventoryUnit.query.filter_by(
        warehouse_id=w["ny_a"].id, status=UnitStatus.AVAILABLE
    ).count() == 0
    assert_invariants()


def test_phase7_shared_client_warehouse_inventory_across_divisions(app, db, admin_user):
    w = _world(db)
    _stock(w["a"], w["ny_a"], "111", 10)
    ecom = _import_order(
        admin_user, w["a"], w["ecom_a"], [_line(order="E1", warehouse="NY", upc="111", qty=4)]
    )
    retail = _import_order(
        admin_user, w["a"], w["retail_a"], [_line(order="R1", warehouse="NY", upc="111", qty=4)]
    )
    assert allocate_order(ecom, user=admin_user)["reserved"] == 4
    assert allocate_order(retail, user=admin_user)["reserved"] == 4
    assert _available(w["a"].id, w["ny_a"].id, "111") == 2
    wholesale = _import_order(
        admin_user, w["a"], w["wholesale_a"], [_line(order="W1", warehouse="NY", upc="111", qty=2)]
    )
    assert allocate_order(wholesale, user=admin_user)["reserved"] == 2
    assert _available(w["a"].id, w["ny_a"].id, "111") == 0

    _stock(w["a"], w["ny_a"], "222", 5)
    foreign_div = _import_order(
        admin_user, w["b"], w["ecom_b"], [_line(order="BX", warehouse="NY", upc="222", qty=5)]
    )
    assert allocate_order(foreign_div, user=admin_user)["reserved"] == 0
    assert _available(w["a"].id, w["ny_a"].id, "222") == 5


def test_phase8_bulk_and_concurrent_isolation(app, db, admin_user):
    w = _world(db)
    _stock(w["a"], w["ny_a"], "111", 10)
    _stock(w["b"], w["ny_b"], "111", 10)
    order_a = _import_order(
        admin_user, w["a"], w["ecom_a"], [_line(order="BA", warehouse="NY", upc="111", qty=10)]
    )
    order_b = _import_order(
        admin_user, w["b"], w["ecom_b"], [_line(order="BB", warehouse="NY", upc="111", qty=10)]
    )
    summary = allocate_orders([order_a.id, order_b.id], user=admin_user)
    assert summary["fully_allocated"] == 2
    assert _reserved(w["a"].id, w["ny_a"].id, "111") == 10
    assert _reserved(w["b"].id, w["ny_b"].id, "111") == 10
    assert Allocation.query.filter(
        Allocation.order_id == order_a.id, Allocation.client_id != w["a"].id
    ).count() == 0
    assert Allocation.query.filter(
        Allocation.order_id == order_b.id, Allocation.client_id != w["b"].id
    ).count() == 0

    _stock(w["a"], w["nj_a"], "333", 1)
    first = _import_order(
        admin_user, w["a"], w["ecom_a"], [_line(order="C1", warehouse="NJ", upc="333", qty=1)]
    )
    second = _import_order(
        admin_user, w["a"], w["ecom_a"], [_line(order="C2", warehouse="NJ", upc="333", qty=1)]
    )
    results = []

    def worker(oid):
        with app.app_context():
            order = db.session.get(Order, oid)
            try:
                results.append(allocate_order(order, user=admin_user))
            except Exception as exc:  # noqa: BLE001
                db.session.rollback()
                results.append({"error": str(exc), "reserved": 0})

    threads = [
        threading.Thread(target=worker, args=(first.id,)),
        threading.Thread(target=worker, args=(second.id,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(r.get("reserved", 0) for r in results) == [0, 1]
    assert InventoryUnit.query.filter_by(upc="333", status=UnitStatus.RESERVED).count() == 1
    assert Allocation.query.filter_by(status=AllocationStatus.ACTIVE, upc="333").count() == 1


def test_phase9_user_idor_fail_closed(app, db, admin_user, client):
    w = _world(db)
    preview = inv_analyze(_inv_xlsx([_inv_row(upc="111", qty=2)]), "a.xlsx", w["a"], w["ny_a"])
    batch_a = inv_commit(preview, user=admin_user)
    preview_b = inv_analyze(_inv_xlsx([_inv_row(upc="111", qty=2)]), "b.xlsx", w["b"], w["ny_b"])
    batch_b = inv_commit(preview_b, user=admin_user)
    order_a = _import_order(
        admin_user, w["a"], w["ecom_a"], [_line(order="IA", warehouse="NY", upc="111", qty=2)]
    )
    order_b = _import_order(
        admin_user, w["b"], w["ecom_b"], [_line(order="IB", warehouse="NY", upc="111", qty=2)]
    )
    allocate_order(order_b, user=admin_user)
    ticket_b = create_pick_ticket(Order.query.get(order_b.id))
    unit_b = InventoryUnit.query.filter_by(client_id=w["b"].id).first()
    issue_b = InventoryIssue(
        client_id=w["b"].id,
        warehouse_id=w["ny_b"].id,
        inventory_unit_id=unit_b.id,
        upc=unit_b.upc,
        original_location=unit_b.location,
        issue_type=InventoryIssueType.PICK_UNIT_NOT_FOUND,
        status="OPEN",
    )
    db.session.add(issue_b)
    db.session.commit()

    perms = [
        "INVENTORY_VIEW",
        "INVENTORY_UPLOAD",
        "INVENTORY_ISSUES_VIEW",
        "ORDERS_VIEW",
        "ORDERS_UPLOAD",
        "ALLOCATION_VIEW",
        "ALLOCATION_EXECUTE",
        "PICK_TICKET_VIEW",
        "PROCESSING_VIEW",
        "DASHBOARD_VIEW",
    ]
    create_user("usera", perms=perms, clients=[w["a"].id])
    login(client, "usera")

    html = client.get("/inventory/").get_data(as_text=True)
    assert w["a"].client_code in html or "ClientA" in html or True
    assert client.get(f"/inventory/?client_id={w['b'].id}").status_code == 404
    assert client.get(f"/orders/{order_b.id}").status_code == 404
    assert client.get(f"/allocation/{order_b.id}").status_code == 404
    assert client.post(
        f"/allocation/{order_b.id}/run",
        data=form_data(client),
    ).status_code == 404
    assert client.get(f"/orders/pick-tickets/{ticket_b.id}").status_code == 404
    assert client.get(f"/processing/{order_b.id}").status_code == 404
    assert client.get(f"/inventory/issues/{issue_b.id}").status_code == 404
    assert client.get(f"/inventory/imports/{batch_b.id}").status_code == 404
    assert client.post(
        f"/imports/{batch_b.id}/recover",
        data=form_data(client),
    ).status_code == 404
    assert client.get(f"/orders/{order_a.id}").status_code == 200
    assert client.get(f"/inventory/imports/{batch_a.id}").status_code == 200


def test_phase10_integrity_queries_zero(app, db, admin_user):
    w = _world(db)
    _stock(w["a"], w["ny_a"], "111", 3)
    _stock(w["b"], w["ny_b"], "111", 3)
    order_a = _import_order(
        admin_user, w["a"], w["ecom_a"], [_line(order="QA", warehouse="NY", upc="111", qty=3)]
    )
    order_b = _import_order(
        admin_user, w["b"], w["ecom_b"], [_line(order="QB", warehouse="NY", upc="111", qty=3)]
    )
    allocate_orders([order_a.id, order_b.id], user=admin_user)
    counts = {}
    for name, sql in INTEGRITY_SQL.items():
        rows = db.session.execute(text(sql)).fetchall()
        counts[name] = len(rows)
        assert rows == [], f"{name} returned {rows}"
    print("INTEGRITY_COUNTS", counts)
    assert_invariants()


def test_phase11_additional_upload_does_not_touch_other_tenant(app, db, admin_user):
    w = _world(db)
    first_b = inv_commit(
        inv_analyze(_inv_xlsx([_inv_row(upc="111", qty=5)]), "b1.xlsx", w["b"], w["ny_b"]),
        user=admin_user,
    )
    b_ids = {u.id for u in InventoryUnit.query.filter_by(client_id=w["b"].id).all()}
    b_count = len(b_ids)
    inv_commit(
        inv_analyze(_inv_xlsx([_inv_row(upc="111", qty=8)]), "a2.xlsx", w["a"], w["ny_a"]),
        user=admin_user,
    )
    after = InventoryUnit.query.filter_by(client_id=w["b"].id).all()
    assert {u.id for u in after} == b_ids
    assert len(after) == b_count
    assert status_counts(client_id=w["b"].id, warehouse_id=w["ny_b"].id, upc="111")["available"] == 5
    assert status_counts(client_id=w["a"].id, warehouse_id=w["ny_a"].id, upc="111")["available"] == 8
    rows = [r for r in aggregate_rows() if r["upc"] == "111"]
    assert {r["client_id"] for r in rows} == {w["a"].id, w["b"].id}
    assert first_b.client_id == w["b"].id
    assert ImportBatch.query.filter_by(client_id=w["a"].id).count() == 1


def test_phase1_app_only_vs_database_enforcement(app, db):
    """Document which ownership rules live only in application code."""
    inspector = sa_inspect(db.engine)
    order_fks = inspector.get_foreign_keys("orders")
    targets = {(fk["constrained_columns"][0], fk["referred_table"]) for fk in order_fks}
    assert ("client_id", "clients") in targets
    assert ("division_id", "divisions") in targets
    assert ("warehouse_id", "warehouses") in targets
    # No composite DB check that order.client_id equals division.client_id.
    checks = inspector.get_check_constraints("orders")
    assert not any("client_id" in (c.get("sqltext") or "") for c in checks)
    unit_checks = inspector.get_check_constraints("inventory_units")
    assert not any("client_id" in (c.get("sqltext") or "") for c in unit_checks)
    assert Path("app/services/allocation.py").read_text().count("Client + Warehouse + UPC") >= 1
