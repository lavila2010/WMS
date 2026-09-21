"""One-shot baseline runner for current allocation SQL/time. No PII."""

from time import perf_counter

from app.constants import OrderStatus, UnitStatus, LedgerType
from app.extensions import db
from app.models import InventoryTransaction, InventoryUnit, Order, OrderLine
from app.services.allocation import allocate_order, allocate_orders
from tests.conftest import create_user, login
from tests.sql_probe import count_sql
from tests.test_v2_phase04_allocation import _world


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


def test_capture_allocation_baseline(app, db, admin_user, admin_client):
    w = _world(db)
    _bulk_stock(w["celine"].id, w["cel_ny"].id, "UPC-A", 200)
    orders = [_make_order(w, 9000 + i, 2) for i in range(50)]
    db.session.commit()

    with count_sql() as page:
        t0 = perf_counter()
        resp = admin_client.get("/allocation/")
        page_ms = (perf_counter() - t0) * 1000
    html = resp.get_data(as_text=True)
    displayed = html.count("01-CEL-")
    print(
        "BASELINE_PAGE",
        f"sql={page['count']}",
        f"db_ms={page['db_ms']:.1f}",
        f"total_ms={page_ms:.1f}",
        f"displayed={displayed}",
        f"status={resp.status_code}",
    )

    hundred = _make_order(w, 9900, 100, upc="UPC-B")
    _bulk_stock(w["celine"].id, w["cel_ny"].id, "UPC-B", 100, location="B-01")
    lines = OrderLine.query.filter_by(order_id=hundred.id).count()
    with count_sql() as one:
        t0 = perc = perf_counter()
        result = allocate_order(hundred, user=admin_user)
        one_ms = (perf_counter() - perc) * 1000
    print(
        "BASELINE_100",
        f"requested=100",
        f"lines={lines}",
        f"reserved={result['reserved']}",
        f"sql={one['count']}",
        f"db_ms={one['db_ms']:.1f}",
        f"total_ms={one_ms:.1f}",
    )

    for size, start, label in ((10, 9100, "BULK_10"), (25, 9200, "BULK_25"), (50, 9300, "BULK_50")):
        batch = [_make_order(w, start + i, 2, upc=f"UPC-{label}") for i in range(size)]
        _bulk_stock(w["celine"].id, w["cel_ny"].id, f"UPC-{label}", size * 2)
        db.session.commit()
        ids = [o.id for o in batch]
        with count_sql() as bulk:
            t0 = perf_counter()
            summary = allocate_orders(ids, user=admin_user)
            bulk_ms = (perf_counter() - t0) * 1000
        print(
            f"BASELINE_{label}",
            f"selected={summary['selected']}",
            f"full={summary['fully_allocated']}",
            f"sql={bulk['count']}",
            f"db_ms={bulk['db_ms']:.1f}",
            f"total_ms={bulk_ms:.1f}",
        )
