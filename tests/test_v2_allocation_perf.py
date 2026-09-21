"""Allocation performance: quantity/status semantics, query counts, scale, concurrency."""

from __future__ import annotations

import threading
from time import perf_counter

from flask import render_template
from sqlalchemy import text

from app.constants import AllocationStatus, LedgerType, OrderStatus, UnitStatus
from app.extensions import db
from app.models import Allocation, InventoryTransaction, InventoryUnit, Order, OrderLine
from app.schema import ensure_v2_schema
from app.services.allocation import ALLOC_CHUNK_SIZE, allocate_order, allocate_orders
from app.services.allocation_exceptions import list_daily_exceptions
from app.services.allocation_overview import allocation_overview_page, bulk_order_quantity_rows
from app.services.fulfillment import order_quantities, pick_ticket_eligible
from tests.conftest import create_user, form_data, login
from tests.sql_probe import count_sql
from tests.test_v2_phase03_orders import _line
from tests.test_v2_phase04_allocation import _import_order, _world
from tests.test_v2_phase05_pick_tickets import _ready_order

BASELINE_PAGE_SQL = 105
BASELINE_100_SQL = 419
BASELINE_BULK_50_SQL = 1401


def _bulk_stock(client_id, warehouse_id, upc, qty, location="A-01"):
    units = [
        InventoryUnit(
            client_id=client_id,
            warehouse_id=warehouse_id,
            upc=upc,
            sku="SKU",
            location=location,
            status=UnitStatus.AVAILABLE,
        )
        for _ in range(qty)
    ]
    db.session.add_all(units)
    db.session.flush()
    db.session.add_all(
        [
            InventoryTransaction(
                client_id=client_id,
                warehouse_id=warehouse_id,
                inventory_unit_id=unit.id,
                upc=upc,
                location=location,
                transaction_type=LedgerType.IMPORT,
                from_status=None,
                to_status=UnitStatus.AVAILABLE,
            )
            for unit in units
        ]
    )
    db.session.commit()


def _make_order(w, number, qty, upc="UPC-A"):
    order = Order(
        client_id=w["celine"].id,
        division_id=w["cel_ecom"].id,
        warehouse_id=w["cel_ny"].id,
        client_order_number=str(number),
        wms_order_id=f"01-CEL-{number}-01",
        customer="Cust",
        status=OrderStatus.UNALLOCATED,
    )
    db.session.add(order)
    db.session.flush()
    db.session.add(
        OrderLine(
            order_id=order.id,
            client_id=w["celine"].id,
            upc=upc,
            qty_ordered=qty,
        )
    )
    db.session.commit()
    return order


def _assert_formula(qty):
    short = int(qty.get("short") or 0)
    assert qty["ordered"] == qty["shipped"] + short + qty["currently_allocated"] + qty["remaining"]


def _table_counts():
    names = (
        "orders",
        "order_lines",
        "inventory_units",
        "allocations",
        "inventory_transactions",
        "pick_tickets",
        "documents",
        "cartons",
    )
    out = {}
    for name in names:
        exists = db.session.execute(text("SELECT to_regclass(:n)"), {"n": f"public.{name}"}).scalar()
        out[name] = (
            int(db.session.execute(text(f"SELECT COUNT(*) FROM {name}")).scalar() or 0) if exists else 0
        )
    out["indexes"] = int(
        db.session.execute(text("SELECT COUNT(*) FROM pg_indexes WHERE schemaname = 'public'")).scalar() or 0
    )
    return out


def test_a_b_quantities_and_status_match_order_quantities(app, db, admin_user):
    w = _world(db)
    _bulk_stock(w["celine"].id, w["cel_ny"].id, "UPC-A", 3)
    unalloc = _make_order(w, 6101, 2, upc="UPC-NONE")
    partial = _make_order(w, 6102, 4)
    allocate_order(partial, user=admin_user)
    page = allocation_overview_page(admin=True, page=1, per_page=50)
    by_id = {row["order_id"]: row for row in page["rows"]}
    for order in (unalloc, partial):
        qty = order_quantities(Order.query.get(order.id))
        _assert_formula(qty)
        row = by_id[order.id]
        assert row["ordered"] == qty["ordered"]
        assert row["shipped"] == qty["shipped"]
        assert row["currently_allocated"] == qty["currently_allocated"]
        assert row["remaining"] == qty["remaining"]
        assert row["status"] == Order.query.get(order.id).status
    assert by_id[unalloc.id]["status"] == OrderStatus.UNALLOCATED
    assert by_id[partial.id]["status"] == OrderStatus.PARTIALLY_ALLOCATED
    assert by_id[partial.id]["partial_approval"] == "PENDING"
    assert by_id[partial.id]["currently_allocated"] == 3
    assert by_id[partial.id]["remaining"] == 1


def test_c_d_partial_and_full_states_unchanged(app, db, admin_user):
    w = _world(db)
    _bulk_stock(w["celine"].id, w["cel_ny"].id, "UPC-A", 3)
    partial = _make_order(w, 6103, 2, upc="UPC-A")
    full = _make_order(w, 6104, 1, upc="UPC-A")
    assert allocate_order(partial, user=admin_user)["status"] == OrderStatus.ALLOCATED
    assert allocate_order(full, user=admin_user)["status"] == OrderStatus.ALLOCATED
    missing = _make_order(w, 6105, 2, upc="UPC-NONE")
    none = allocate_order(missing, user=admin_user)
    assert none["reserved"] == 0
    assert none["status"] == OrderStatus.UNALLOCATED
    _bulk_stock(w["celine"].id, w["cel_ny"].id, "UPC-B", 1)
    half = _make_order(w, 6106, 3, upc="UPC-B")
    result = allocate_order(half, user=admin_user)
    assert result["reserved"] == 1
    assert result["status"] == OrderStatus.PARTIALLY_ALLOCATED
    _assert_formula(order_quantities(Order.query.get(half.id)))


def test_e_daily_exceptions_unchanged(app, db, admin_user):
    w = _world(db)
    _bulk_stock(w["celine"].id, w["cel_ny"].id, "UPC-A", 1)
    open_order = _make_order(w, 6107, 2)
    payload = list_daily_exceptions(admin=True)
    ids = {row["order"].id for row in payload["rows"]}
    assert open_order.id in ids
    assert payload["kpis"]["outstanding_units"] == sum(r["remaining"] for r in payload["rows"])


def test_f_tenant_isolation_unchanged(app, db, admin_user, client):
    w = _world(db)
    _bulk_stock(w["celine"].id, w["cel_ny"].id, "UPC-A", 1)
    order = _make_order(w, 6108, 1)
    create_user("dio", perms=["ALLOCATION_VIEW"], clients=[w["dior"].id])
    login(client, "dio")
    assert client.get("/allocation/").status_code == 200
    html = client.get("/allocation/").get_data(as_text=True)
    assert order.wms_order_id not in html
    assert client.get(f"/allocation/{order.id}").status_code == 404


def test_g_bulk_allocation_summary_unchanged(app, db, admin_user, admin_client):
    w = _world(db)
    _bulk_stock(w["celine"].id, w["cel_ny"].id, "UPC-A", 4)
    ok = _make_order(w, 6109, 1)
    other = _make_order(w, 6110, 1)
    none = _make_order(w, 6111, 1, upc="UPC-Z")
    cancelled = _make_order(w, 6112, 1)
    cancelled.status = OrderStatus.CANCELLED
    db.session.commit()
    summary = allocate_orders([ok.id, other.id, none.id, cancelled.id], user=admin_user)
    assert summary["selected"] == 4
    assert summary["fully_allocated"] == 2
    assert summary["no_inventory"] == 1
    assert summary["failed"] == 1
    resp = admin_client.post(
        "/allocation/bulk",
        data=form_data(admin_client, {"order_ids": [str(ok.id)]}),
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Selected" in resp.data


def test_h_concurrency_skip_locked(app, db, admin_user):
    w = _world(db)
    _bulk_stock(w["celine"].id, w["cel_ny"].id, "UPC-A", 1)
    a = _make_order(w, 6113, 1)
    b = _make_order(w, 6114, 1)
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
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    reserved = sorted(r.get("reserved", 0) for r in results)
    assert reserved == [0, 1]
    units = InventoryUnit.query.filter_by(status=UnitStatus.RESERVED).all()
    assert len(units) == 1
    allocs = Allocation.query.filter_by(status=AllocationStatus.ACTIVE).all()
    assert len(allocs) == 1
    assert allocs[0].inventory_unit_id == units[0].id


def test_i_ledger_matches_reserved_units(app, db, admin_user):
    w = _world(db)
    _bulk_stock(w["celine"].id, w["cel_ny"].id, "UPC-A", 7)
    order = _make_order(w, 6115, 7)
    result = allocate_order(order, user=admin_user)
    assert result["reserved"] == 7
    reserved_units = InventoryUnit.query.filter_by(
        allocated_order_id=order.id, status=UnitStatus.RESERVED
    ).count()
    reserve_txns = InventoryTransaction.query.filter_by(
        order_id=order.id, transaction_type=LedgerType.RESERVE
    ).count()
    allocs = Allocation.query.filter_by(order_id=order.id, status=AllocationStatus.ACTIVE).count()
    assert reserved_units == reserve_txns == allocs == 7


def test_j_k_pick_ticket_and_multi_wave(app, db, admin_user):
    w, order = _ready_order(db, admin_user, order_number="6116", qty=2)
    assert pick_ticket_eligible(order) is True
    _bulk_stock(w["celine"].id, w["cel_ny"].id, "UPC-WAVE", 5)
    wave = _make_order(w, 6117, 5, upc="UPC-WAVE")
    for unit in InventoryUnit.query.filter_by(upc="UPC-WAVE").limit(2):
        unit.status = UnitStatus.SHIPPED
    db.session.commit()
    result = allocate_order(Order.query.get(wave.id), user=admin_user)
    order = Order.query.get(wave.id)
    assert result["reserved"] == 3
    assert pick_ticket_eligible(order) is False
    from app.services.allocation import approve_partial_allocation
    from app.services.pick_tickets import create_pick_ticket

    approve_partial_allocation(order, user=admin_user)
    ticket = create_pick_ticket(Order.query.get(order.id))
    assert ticket.ticket_sequence == 1
    assert pick_ticket_eligible(Order.query.get(order.id)) is False


def test_l_allocation_page_query_count(app, db, admin_user, admin_client):
    w = _world(db)
    _bulk_stock(w["celine"].id, w["cel_ny"].id, "UPC-A", 100)
    orders = [_make_order(w, 7000 + i, 2) for i in range(50)]
    db.session.commit()
    admin_client.get("/allocation/?per_page=50")
    with count_sql() as stats:
        resp = admin_client.get("/allocation/?per_page=50")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert html.count('name="order_ids"') == 50
    assert "25" in html and "50" in html and "100" in html
    assert stats["count"] <= 10, stats["count"]
    page = allocation_overview_page(admin=True, page=1, per_page=50)
    assert page["per_page"] == 50
    assert len(page["rows"]) == 50
    for row in page["rows"]:
        qty = order_quantities(Order.query.get(row["order_id"]))
        assert row["ordered"] == qty["ordered"]
        assert row["remaining"] == qty["remaining"]
        _assert_formula(row)
    with app.test_request_context("/allocation/?per_page=50"):
        from flask_login import login_user

        login_user(admin_user)
        with count_sql() as render_stats:
            render_template(
                "allocation/index.html",
                rows=page["rows"],
                clients=[],
                client_id=None,
                summary=None,
                page=page["page"],
                pages=page["pages"],
                per_page=page["per_page"],
                total=page["total"],
                per_page_options=page["per_page_options"],
            )
    assert render_stats["count"] == 0, render_stats["count"]
    _ = orders


def test_pagination_select_all_visible_scoped(app, db, admin_user, admin_client):
    w = _world(db)
    _bulk_stock(w["celine"].id, w["cel_ny"].id, "UPC-A", 70)
    [_make_order(w, 7200 + i, 1) for i in range(60)]
    page1 = admin_client.get("/allocation/?per_page=50&page=1").get_data(as_text=True)
    page2 = admin_client.get("/allocation/?per_page=50&page=2").get_data(as_text=True)
    assert page1.count('name="order_ids"') == 50
    assert page2.count('name="order_ids"') == 10
    assert "Select All Visible" in page1
    twenty_five = admin_client.get("/allocation/?per_page=25").get_data(as_text=True)
    assert twenty_five.count('name="order_ids"') == 25
    hundred = admin_client.get("/allocation/?per_page=100").get_data(as_text=True)
    assert hundred.count('name="order_ids"') == 60


def test_m_scale_50x500_and_50x5000(app, db, admin_user):
    w = _world(db)
    _bulk_stock(w["celine"].id, w["cel_ny"].id, "UPC-S1", 500)
    small = [_make_order(w, 8000 + i, 10, upc="UPC-S1") for i in range(50)]
    t0 = perf_counter()
    summary = allocate_orders([o.id for o in small], user=admin_user)
    small_ms = (perf_counter() - t0) * 1000
    assert summary["fully_allocated"] == 50
    assert Allocation.query.filter_by(status=AllocationStatus.ACTIVE).count() == 500
    assert InventoryTransaction.query.filter_by(transaction_type=LedgerType.RESERVE).count() == 500
    print("SCALE_50x500", f"ms={small_ms:.1f}", f"chunk={ALLOC_CHUNK_SIZE}")

    _bulk_stock(w["celine"].id, w["cel_ny"].id, "UPC-S2", 5000, location="C-01")
    large = [_make_order(w, 8100 + i, 100, upc="UPC-S2") for i in range(50)]
    t0 = perf_counter()
    summary = allocate_orders([o.id for o in large], user=admin_user)
    large_ms = (perf_counter() - t0) * 1000
    assert summary["fully_allocated"] == 50
    assert InventoryTransaction.query.filter_by(
        transaction_type=LedgerType.RESERVE, upc="UPC-S2"
    ).count() == 5000
    print("SCALE_50x5000", f"ms={large_ms:.1f}")


def test_after_metrics_and_indexes(app, db, admin_user, admin_client):
    pre = _table_counts()
    db.session.commit()
    db.session.remove()
    ensure_v2_schema()
    post = _table_counts()
    for key, value in pre.items():
        if key == "indexes":
            assert post[key] >= value
        else:
            assert post[key] == value
    names = {
        row[0]
        for row in db.session.execute(
            text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
        )
    }
    for required in (
        "ix_orders_client_status_created",
        "ix_alloc_order_status",
        "ix_alloc_line_status",
        "ix_alloc_order_wave_ticket",
        "ix_units_allocation",
        "ix_units_alloc_order_status",
        "ix_units_cwus",
        "ix_units_cwus_loc_id",
        "ix_pick_tickets_order_status",
    ):
        assert required in names

    w = _world(db)
    _bulk_stock(w["celine"].id, w["cel_ny"].id, "UPC-A", 200)
    [_make_order(w, 9000 + i, 2) for i in range(50)]
    with count_sql() as page:
        t0 = perf_counter()
        resp = admin_client.get("/allocation/?per_page=50")
        page_ms = (perf_counter() - t0) * 1000
    assert resp.status_code == 200
    print(
        "AFTER_PAGE",
        f"sql={page['count']}",
        f"db_ms={page['db_ms']:.1f}",
        f"total_ms={page_ms:.1f}",
        f"baseline_sql={BASELINE_PAGE_SQL}",
    )
    assert page["count"] < BASELINE_PAGE_SQL
    assert page["count"] <= 10

    hundred = _make_order(w, 9900, 100, upc="UPC-B")
    _bulk_stock(w["celine"].id, w["cel_ny"].id, "UPC-B", 100, location="B-01")
    with count_sql() as one:
        t0 = perf_counter()
        result = allocate_order(hundred, user=admin_user)
        one_ms = (perf_counter() - t0) * 1000
    assert result["reserved"] == 100
    print(
        "AFTER_100",
        f"sql={one['count']}",
        f"db_ms={one['db_ms']:.1f}",
        f"total_ms={one_ms:.1f}",
        f"baseline_sql={BASELINE_100_SQL}",
    )
    assert one["count"] < BASELINE_100_SQL / 2

    batch = [_make_order(w, 9300 + i, 2, upc="UPC-BULK") for i in range(50)]
    _bulk_stock(w["celine"].id, w["cel_ny"].id, "UPC-BULK", 100)
    with count_sql() as bulk:
        t0 = perf_counter()
        summary = allocate_orders([o.id for o in batch], user=admin_user)
        bulk_ms = (perf_counter() - t0) * 1000
    assert summary["fully_allocated"] == 50
    print(
        "AFTER_BULK_50",
        f"sql={bulk['count']}",
        f"db_ms={bulk['db_ms']:.1f}",
        f"total_ms={bulk_ms:.1f}",
        f"baseline_sql={BASELINE_BULK_50_SQL}",
    )
    assert bulk["count"] < BASELINE_BULK_50_SQL

    plan = "\n".join(
        row[0]
        for row in db.session.execute(
            text(
                """
                EXPLAIN (FORMAT TEXT)
                SELECT u.id
                FROM inventory_units u
                LEFT JOIN import_batches b ON u.import_batch_id = b.id
                WHERE u.client_id = :cid
                  AND u.warehouse_id = :wid
                  AND u.upc = :upc
                  AND u.status = 'AVAILABLE'
                  AND (u.import_batch_id IS NULL OR b.status = 'COMPLETED')
                ORDER BY u.location ASC, u.id ASC
                LIMIT 100
                """
            ),
            {
                "cid": w["celine"].id,
                "wid": w["cel_ny"].id,
                "upc": "UPC-A",
            },
        )
    )
    print("CANDIDATE_EXPLAIN", plan.replace("\n", " | "))
    assert "inventory_units" in plan.lower()


def test_multi_line_partial_and_no_inventory(app, db, admin_user):
    w = _world(db)
    _bulk_stock(w["celine"].id, w["cel_ny"].id, "UPC-M1", 2)
    _bulk_stock(w["celine"].id, w["cel_ny"].id, "UPC-M2", 0)
    order = Order(
        client_id=w["celine"].id,
        division_id=w["cel_ecom"].id,
        warehouse_id=w["cel_ny"].id,
        client_order_number="6118",
        wms_order_id="01-CEL-6118-01",
        customer="Cust",
        status=OrderStatus.UNALLOCATED,
    )
    db.session.add(order)
    db.session.flush()
    db.session.add_all(
        [
            OrderLine(order_id=order.id, client_id=w["celine"].id, upc="UPC-M1", qty_ordered=2),
            OrderLine(order_id=order.id, client_id=w["celine"].id, upc="UPC-M2", qty_ordered=3),
        ]
    )
    db.session.commit()
    result = allocate_order(order, user=admin_user)
    assert result["reserved"] == 2
    assert result["status"] == OrderStatus.PARTIALLY_ALLOCATED
    assert {s["upc"] for s in result["shortages"]} == {"UPC-M2"}
    qty = order_quantities(Order.query.get(order.id))
    _assert_formula(qty)
    mapped = bulk_order_quantity_rows([order.id])[order.id]
    assert mapped["currently_allocated"] == 2
    assert mapped["remaining"] == 3


def test_imported_order_still_allocates(app, db, admin_user):
    w = _world(db)
    from tests.test_v2_phase04_allocation import _stock

    _stock(w["celine"], w["cel_ny"], "UPC-A", 2)
    order = _import_order(admin_user, w["celine"], w["cel_ecom"], [_line(upc="UPC-A", qty=2)])
    result = allocate_order(order, user=admin_user)
    assert result["reserved"] == 2
    assert result["status"] == OrderStatus.ALLOCATED
