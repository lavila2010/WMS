"""CELINE real-file order import + destination identity + capacity."""

from __future__ import annotations

import pytest

from app.constants import ImportBatchStatus
from app.models import ImportBatch, Order, OrderLine
from app.services.allocation import AllocationError, allocate_order
from app.services.inventory_ledger import create_available_unit
from app.services.order_import import (
    OrderImportError,
    analyze,
    begin_processing,
    commit_import,
    process_import_batch,
)
from app.services.order_visibility import apply_operational_order_visibility
from app.services.pick_tickets import create_pick_ticket
from app.workers.inventory_import_worker import process_due_batches
from tests.conftest import form_data
from tests.test_v2_phase03_orders import _line, _setup, _xlsx
from tests.test_v2_phase04_allocation import _world

CELINE_DESTINATIONS = {
    "19": [
        ("CB US SAKS FIFTH AVENUE NY MEN", "1 Fifth Ave New York NY"),
        ("CB US SEATTLE NORDSTROM", "500 Pine St Seattle WA"),
    ],
    "24": [
        ("CB US BEVERLY HILLS NEIMAN MARCUS RTW", "9700 Wilshire Blvd Beverly Hills CA"),
        ("CB US BOCA RATON SAKS FIFTH AVENUE LG", "6000 Glades Rd Boca Raton FL"),
        ("CB US NEW YORK SAKS FIFTH AVENUE RTW", "611 Fifth Ave New York NY"),
    ],
    "25": [
        ("CB US BEVERLY HILL SAKS LG WOMAN", "9600 Wilshire Blvd Beverly Hills CA"),
        ("CB US BEVERLY HILLS SAKS RTW WOMAN", "9602 Wilshire Blvd Beverly Hills CA"),
        ("CB US BOSTON COPLEY PLACE WOMEN", "100 Huntington Ave Boston MA"),
    ],
}


def _celine_line(raw, customer, address, upc, qty):
    return {
        "Warehouse": "NY",
        "Order Number": raw,
        "Customer": customer,
        "CustomerPhone": "",
        "CustomerAddress": address,
        "UPC": upc,
        "QTY": qty,
        "carrier": "",
        "Shipping Service": "",
    }


def _celine_destinations():
    dests = []
    for number in range(1, 32):
        raw = str(number)
        if raw in CELINE_DESTINATIONS:
            dests.extend((raw, customer, address) for customer, address in CELINE_DESTINATIONS[raw])
        else:
            dests.append((raw, f"CUSTOMER {number:02d}", f"ADDRESS {number:02d}"))
    return dests


def build_celine_profile_rows():
    dests = _celine_destinations()
    assert len(dests) == 36
    upcs = [f"{index:012d}" for index in range(1, 473)]
    rows = []

    raw1 = dests[0]
    for index in range(122):
        qty = 2 if index < 29 else 1
        rows.append(_celine_line(*raw1, upcs[index % len(upcs)], qty))

    leftover = dests[1:]
    remaining_rows = 1075 - 122
    remaining_units = 1268 - 151
    extra_units = remaining_units - remaining_rows
    base = remaining_rows // len(leftover)
    remainder = remaining_rows % len(leftover)
    upc_index = 0
    for dest_index, dest in enumerate(leftover):
        count = base + (1 if dest_index < remainder else 0)
        first_upc = None
        for row_index in range(count):
            qty = 2 if extra_units > 0 else 1
            if qty == 2:
                extra_units -= 1
            if dest[0] == "24" and dest[1].startswith("CB US BEVERLY HILLS") and row_index == 1:
                upc = first_upc or upcs[upc_index % len(upcs)]
            else:
                upc = upcs[upc_index % len(upcs)]
                upc_index += 1
                if first_upc is None:
                    first_upc = upc
            rows.append(_celine_line(*dest, upc, qty))

    assert len(rows) == 1075
    assert sum(int(row["QTY"]) for row in rows) == 1268
    assert len({row["UPC"] for row in rows}) == 472
    assert len({row["Order Number"] for row in rows}) == 31
    return rows


def _import_rows(user, client, division, rows, filename="orders.xlsx"):
    preview = analyze(_xlsx(rows), filename, client, division, user=user)
    assert preview["has_blocking"] is False, preview["blocking"]
    batch = commit_import(preview, user=user)
    return preview, batch


def test_a_celine_equivalent_file_imports(app, db, admin_user):
    celine, _, cel_ecom, _, _, _ = _setup(db)
    rows = build_celine_profile_rows()
    preview, batch = _import_rows(admin_user, celine, cel_ecom, rows, "celine.xlsx")
    assert batch.status == ImportBatchStatus.COMPLETED
    assert preview["total_rows"] == 1075
    assert preview["raw_order_count"] == 31
    assert preview["unique_upcs"] == 472
    assert preview["total_units"] == 1268
    assert Order.query.filter_by(import_batch_id=batch.id).count() == preview["order_count"]
    assert preview["order_count"] > 31


def test_b_raw_orders_produce_more_wms_orders(app, db, admin_user):
    celine, _, cel_ecom, _, _, _ = _setup(db)
    preview, _ = _import_rows(admin_user, celine, cel_ecom, build_celine_profile_rows(), "b.xlsx")
    assert preview["raw_order_count"] == 31
    assert preview["wms_order_count"] > 31
    assert preview["wms_order_count"] == 36


def test_cde_orders_19_24_25_split(app, db, admin_user):
    celine, _, cel_ecom, _, _, _ = _setup(db)
    _import_rows(admin_user, celine, cel_ecom, build_celine_profile_rows(), "split.xlsx")
    nineteen = Order.query.filter_by(client_order_number="19").order_by(Order.destination_sequence).all()
    twenty_four = Order.query.filter_by(client_order_number="24").order_by(Order.destination_sequence).all()
    twenty_five = Order.query.filter_by(client_order_number="25").order_by(Order.destination_sequence).all()
    assert [order.wms_order_id for order in nineteen] == ["01-CEL-19-01", "01-CEL-19-02"]
    assert {order.customer for order in nineteen} == {
        "CB US SAKS FIFTH AVENUE NY MEN",
        "CB US SEATTLE NORDSTROM",
    }
    assert [order.wms_order_id for order in twenty_four] == [
        "01-CEL-24-01",
        "01-CEL-24-02",
        "01-CEL-24-03",
    ]
    assert {order.client_order_number for order in twenty_four} == {"24"}
    assert [order.destination_sequence for order in twenty_four] == [1, 2, 3]
    assert [order.wms_order_id for order in twenty_five] == [
        "01-CEL-25-01",
        "01-CEL-25-02",
        "01-CEL-25-03",
    ]
    assert len({order.wms_order_id for order in twenty_four}) == 3


def test_fgh_blank_optional_fields_accepted(app, db, admin_user):
    celine, _, cel_ecom, _, _, _ = _setup(db)
    preview, _ = _import_rows(admin_user, celine, cel_ecom, build_celine_profile_rows(), "blank.xlsx")
    assert preview["has_blocking"] is False
    sample = Order.query.filter_by(client_order_number="24").first()
    assert sample.customer_phone in (None, "")
    assert sample.carrier in (None, "")
    assert sample.shipping_service in (None, "")


def test_i_upc_text_preserved(app, db, admin_user):
    celine, _, cel_ecom, _, _, _ = _setup(db)
    _import_rows(admin_user, celine, cel_ecom, build_celine_profile_rows(), "upc.xlsx")
    assert OrderLine.query.filter_by(upc="000000000001").count() >= 1
    assert all(line.upc == str(line.upc) for line in OrderLine.query.limit(20))


def test_j_same_upc_aggregates(app, db, admin_user):
    celine, _, cel_ecom, _, _, _ = _setup(db)
    rows = [
        _line(order="24", upc="000111", qty=2, customer="Ada", address="1 Main"),
        _line(order="24", upc="000111", qty=3, customer="Ada", address="1 Main"),
    ]
    preview, _ = _import_rows(admin_user, celine, cel_ecom, rows, "agg.xlsx")
    assert preview["order_count"] == 1
    assert preview["order_lines_expected"] == 1
    assert preview["total_units"] == 5
    line = OrderLine.query.one()
    assert line.upc == "000111"
    assert line.qty_ordered == 5


def test_k_wrong_warehouse_rejected(app, db, admin_user):
    celine, _, cel_ecom, _, _, _ = _setup(db)
    preview = analyze(_xlsx([_line(warehouse="XX", qty=1)]), "badwh.xlsx", celine, cel_ecom)
    assert preview["has_blocking"] is True
    with pytest.raises(OrderImportError):
        commit_import(preview, user=admin_user)
    assert Order.query.count() == 0


def test_l_cross_client_isolation(app, db, admin_user):
    celine, dior, cel_ecom, dio_ecom, _, _ = _setup(db)
    for client, division in ((celine, cel_ecom), (dior, dio_ecom)):
        _import_rows(admin_user, client, division, [_line(order="24", qty=1)], f"{client.client_code}.xlsx")
    ids = {order.wms_order_id for order in Order.query.all()}
    assert ids == {"01-CEL-24-01", "02-DIO-24-01"}
    assert Order.query.filter_by(client_id=celine.id).count() == 1
    assert Order.query.filter_by(client_id=dior.id).count() == 1


def test_m_division_warehouse_enforcement(app, db, admin_user):
    celine, _, cel_ecom, _, _, _ = _setup(db)
    from app.services.masters import create_warehouse

    create_warehouse(celine, "NJ")
    db.session.commit()
    preview = analyze(_xlsx([_line(warehouse="NJ", qty=1)]), "map.xlsx", celine, cel_ecom)
    assert preview["has_blocking"] is True
    assert any("not mapped" in error["message"] for error in preview["blocking"])


def test_n_duplicate_confirm_no_duplicate_orders(app, db, admin_user):
    celine, _, cel_ecom, _, _, _ = _setup(db)
    preview = analyze(_xlsx([_line(qty=1)]), "dup.xlsx", celine, cel_ecom)
    first = commit_import(preview, user=admin_user)
    second = commit_import(preview, user=admin_user)
    third = begin_processing(preview["batch_id"], user=admin_user, run="async")
    assert first.id == second.id == third.id
    assert Order.query.count() == 1
    again = analyze(_xlsx([_line(qty=2, upc="UPC-Z")]), "dup2.xlsx", celine, cel_ecom)
    assert again["has_blocking"] is True
    with pytest.raises(OrderImportError):
        commit_import(again, user=admin_user)
    assert Order.query.count() == 1


def test_o_failed_batch_not_allocatable(app, db, admin_user):
    celine, _, cel_ecom, _, _, _ = _setup(db)
    preview = analyze(
        _xlsx([_line(order="A", qty=1), _line(order="B", qty=1)]),
        "fail.xlsx",
        celine,
        cel_ecom,
    )
    with pytest.raises(OrderImportError, match="injected"):
        commit_import(preview, user=admin_user, chunk_size=1, _fail_after=1)
    batch = db.session.get(ImportBatch, preview["batch_id"])
    assert batch.status == ImportBatchStatus.FAILED
    order = Order.query.filter_by(import_batch_id=batch.id).one()
    operational = apply_operational_order_visibility(Order.query).all()
    assert order not in operational
    with pytest.raises(AllocationError, match="not operational"):
        allocate_order(order, user=admin_user)


def test_p_worker_resume_no_duplication(app, db, admin_user):
    celine, _, cel_ecom, _, _, _ = _setup(db)
    preview = analyze(
        _xlsx(
            [
                _line(order="R1", qty=1, upc="U1"),
                _line(order="R2", qty=1, upc="U2"),
                _line(order="R3", qty=1, upc="U3"),
            ]
        ),
        "resume.xlsx",
        celine,
        cel_ecom,
    )
    begin_processing(preview["batch_id"], user=admin_user, run="async")
    process_import_batch(preview["batch_id"], chunk_size=1, max_chunks=1)
    mid = db.session.get(ImportBatch, preview["batch_id"])
    assert mid.status == ImportBatchStatus.PROCESSING
    assert mid.orders_created == 1
    process_due_batches()
    done = db.session.get(ImportBatch, preview["batch_id"])
    assert done.status == ImportBatchStatus.COMPLETED
    assert Order.query.filter_by(import_batch_id=done.id).count() == 3
    assert OrderLine.query.count() == 3
    assert {order.wms_order_id for order in Order.query.all()} == {
        "01-CEL-R1-01",
        "01-CEL-R2-01",
        "01-CEL-R3-01",
    }


def test_q_capacity_25000_source_lines(app, db, admin_user):
    celine, _, cel_ecom, _, _, _ = _setup(db)
    rows = []
    for index in range(25000):
        raw = str((index // 5) + 1)
        dest_extra = index % 10 == 0
        customer = f"DEST-B {raw}" if dest_extra and int(raw) <= 1000 else f"DEST-A {raw}"
        address = f"{customer} ADDR"
        rows.append(
            {
                "Warehouse": "NY",
                "Order Number": raw,
                "Customer": customer,
                "CustomerPhone": "",
                "CustomerAddress": address,
                "UPC": f"{index + 1:012d}",
                "QTY": 2,
                "carrier": "",
                "Shipping Service": "",
            }
        )
    preview = analyze(_xlsx(rows), "capacity.xlsx", celine, cel_ecom, user=admin_user)
    assert preview["has_blocking"] is False, preview["blocking"][:5]
    assert preview["total_rows"] == 25000
    assert preview["wms_order_count"] >= 5000
    batch = commit_import(preview, user=admin_user)
    assert batch.status == ImportBatchStatus.COMPLETED
    assert batch.orders_created == preview["wms_order_count"]
    assert batch.order_lines_created == preview["order_lines_expected"]
    assert batch.units_expected == 50000
    assert Order.query.filter_by(import_batch_id=batch.id).count() == batch.orders_created
    assert (
        OrderLine.query.join(Order, Order.id == OrderLine.order_id)
        .filter(Order.import_batch_id == batch.id)
        .count()
        == batch.order_lines_created
    )


def test_order_24_pick_ticket_uses_wms_identity(app, db, admin_user):
    w = _world(db)
    rows = [
        _line(order="24", upc="UPC-A", qty=1, customer="CB US BEVERLY HILLS NEIMAN MARCUS RTW", address="A"),
        _line(order="24", upc="UPC-B", qty=1, customer="CB US BOCA RATON SAKS FIFTH AVENUE LG", address="B"),
        _line(order="24", upc="UPC-C", qty=1, customer="CB US NEW YORK SAKS FIFTH AVENUE RTW", address="C"),
    ]
    preview, _ = _import_rows(admin_user, w["celine"], w["cel_ecom"], rows, "pt.xlsx")
    assert [item["wms_order_id"] for item in preview["orders"]] == [
        "01-CEL-24-01",
        "01-CEL-24-02",
        "01-CEL-24-03",
    ]
    target = Order.query.filter_by(wms_order_id="01-CEL-24-02").one()
    create_available_unit(
        client_id=w["celine"].id,
        warehouse_id=w["cel_ny"].id,
        upc="UPC-B",
        location="A-01",
        sku="SKU",
    )
    db.session.commit()
    allocate_order(target, user=admin_user)
    ticket = create_pick_ticket(Order.query.get(target.id))
    assert ticket.pick_ticket_number == "01-CEL-24-02-01"
    assert target.client_order_number == "24"


def test_http_confirm_enqueues_only(app, db, admin_client):
    celine, _, cel_ecom, _, _, _ = _setup(db)
    preview = admin_client.post(
        "/orders/upload/preview",
        data=form_data(
            admin_client,
            {
                "client_id": celine.id,
                "division_id": cel_ecom.id,
                "file": (_xlsx([_line(qty=1)]), "http.xlsx"),
            },
        ),
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert preview.status_code == 200
    confirm = admin_client.post(
        "/orders/upload/confirm",
        data=form_data(admin_client),
        follow_redirects=True,
    )
    assert confirm.status_code == 200
    batch = ImportBatch.query.filter_by(type="ORDERS").order_by(ImportBatch.id.desc()).first()
    assert batch.status == ImportBatchStatus.PROCESSING
    assert Order.query.count() == 0
    process_due_batches()
    assert db.session.get(ImportBatch, batch.id).status == ImportBatchStatus.COMPLETED
    assert Order.query.count() == 1
