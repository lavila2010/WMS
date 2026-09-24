from app.constants import LedgerType, UnitStatus
from app.models import Allocation, InventoryTransaction, InventoryUnit, Order
from app.services.allocation import allocate_order
from app.services.inventory_ledger import create_available_unit
from app.services.invariants import assert_invariants
from app.services.masters import create_client, create_division, create_warehouse, map_division_warehouse
from app.services.order_import import analyze, commit_import
from app.services.pick_tickets import create_pick_ticket
from app.services.processing import (
    acquire_lock,
    close_order,
    ensure_open_carton,
    request_close,
    scan_upc,
    set_dimensions,
    set_weight,
)
from tests.test_v2_phase03_orders import _line, _xlsx


def test_p10_certification_dataset(app, db, admin_user):
    brands = []
    for name, initials in (("Celine", "CEL"), ("Dior", "DIO"), ("Valentino", "VAL")):
        client = create_client(name, initials)
        db.session.commit()
        divs = [create_division(client, op, op) for op in ("ECOM", "RTL", "WHLS")]
        ny = create_warehouse(client, "NY")
        nj = create_warehouse(client, "NJ")
        db.session.commit()
        for div in divs:
            map_division_warehouse(div, ny)
            map_division_warehouse(div, nj)
        db.session.commit()
        brands.append((client, divs, ny, nj))

    # shared UPC across clients and locations
    for client, _divs, ny, nj in brands:
        for warehouse, loc in ((ny, "A-01"), (nj, "B-01")):
            for _ in range(4):
                create_available_unit(
                    client_id=client.id,
                    warehouse_id=warehouse.id,
                    upc="SHARED-UPC",
                    location=loc,
                    sku="SHARE",
                )
    db.session.commit()

    # same client order number on two clients
    for client, divs, ny, _nj in brands[:2]:
        preview = analyze(
            _xlsx([_line(order="1251", warehouse="NY", upc="SHARED-UPC", qty=2)]),
            "o.xlsx",
            client,
            divs[0],
        )
        commit_import(preview, user=admin_user)

    cel_order = Order.query.filter_by(client_id=brands[0][0].id, client_order_number="1251").one()
    dio_order = Order.query.filter_by(client_id=brands[1][0].id, client_order_number="1251").one()
    assert cel_order.wms_order_id != dio_order.wms_order_id

    allocate_order(cel_order, user=admin_user)
    create_pick_ticket(cel_order)
    acquire_lock(cel_order, admin_user)
    carton = ensure_open_carton(cel_order, admin_user)
    set_dimensions(carton, 10, 8, 6)
    db.session.commit()
    scan_upc(cel_order, carton, "SHARED-UPC", admin_user)
    scan_upc(cel_order, carton, "SHARED-UPC", admin_user)
    request_close(carton, admin_user)
    set_weight(carton, 3.3, user=admin_user)
    close_order(Order.query.get(cel_order.id), admin_user)
    closed = Order.query.get(cel_order.id)
    ordered = sum(l.qty_ordered for l in closed.lines)
    assert ordered == sum(l.qty_allocated for l in closed.lines) == sum(l.qty_packed for l in closed.lines) == sum(
        l.qty_shipped for l in closed.lines
    )

    imported = InventoryTransaction.query.filter_by(transaction_type=LedgerType.IMPORT).count()
    shipped = InventoryTransaction.query.filter_by(transaction_type=LedgerType.SHIP).count()
    on_hand = InventoryUnit.query.filter(InventoryUnit.status.in_(UnitStatus.ON_HAND)).count()
    assert imported - shipped == on_hand
    assert Allocation.query.filter_by(inventory_unit_id=InventoryUnit.query.first().id, status="ACTIVE").count() <= 1
    assert_invariants()
    assert {u.warehouse.client_id for u in InventoryUnit.query.all()} == {u.client_id for u in InventoryUnit.query.all()}
