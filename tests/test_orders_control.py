"""Orders Control: client-gated UI, two-step import, allocation report, RBAC."""

import io

from app.constants import OrderStatus
from app.models import ImportBatch, Order
from app.services.imports import build_orders_workbook
from app.services.order_import import analyze, confirm_import, import_orders
from app.workflow import transition
from tests.conftest import create_user, login, make_order, make_unit


def test_orders_index_hides_data_until_client_selected(admin_client, db):
    order = make_order("SO-HIDDEN", lines=[("SKU-A", 1)])
    resp = admin_client.get("/orders/")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Select a client to view orders" in html
    assert "SO-HIDDEN" not in html
    resp = admin_client.get(f"/orders/?client_id={order.client_id}")
    html = resp.get_data(as_text=True)
    assert "SO-HIDDEN" in html
    assert "Select a client to view orders" not in html


def test_orders_open_default_excludes_closed(admin_client, db):
    open_order = make_order("SO-OPEN", lines=[("SKU-A", 1)])
    closed = make_order("SO-CLOSED", lines=[("SKU-A", 1)])
    closed.status = OrderStatus.CLOSED
    db.session.commit()
    html = admin_client.get(f"/orders/?client_id={open_order.client_id}").get_data(as_text=True)
    assert "SO-OPEN" in html
    assert "SO-CLOSED" not in html
    html = admin_client.get(
        f"/orders/?client_id={open_order.client_id}&status=closed"
    ).get_data(as_text=True)
    assert "SO-CLOSED" in html


def test_order_import_preview_writes_nothing(db):
    wb = build_orders_workbook(
        [{"client": "ACME", "warehouse": "WH1", "order_type": "B2C", "order_number": "SO-PV", "sku": "SKU-A", "quantity": 2}]
    )
    preview = analyze(wb, "Orders.xlsx")
    assert preview.has_blocking is False
    assert preview.valid_orders == 1
    assert Order.query.count() == 0
    assert ImportBatch.query.count() == 0


def test_order_import_confirm_writes(db):
    wb = build_orders_workbook(
        [{"client": "ACME", "warehouse": "WH1", "order_type": "B2C", "order_number": "SO-CF", "sku": "SKU-A", "quantity": 2, "carrier": "UPS", "shipping_service": "Ground"}]
    )
    preview = analyze(wb, "Orders.xlsx")
    batch = confirm_import(preview)
    assert Order.query.filter_by(order_number="SO-CF").count() == 1
    assert batch.rows_imported == 1


def test_order_import_blocking_rejects_confirm(db):
    wb = build_orders_workbook(
        [
            {"client": "ACME", "warehouse": "WH1", "order_type": "B2C", "order_number": "SO-BAD", "sku": "SKU-A", "quantity": 1, "carrier": "UPS", "shipping_service": "Ground"},
            {"client": "ACME", "warehouse": "WH1", "order_type": "B2C", "order_number": "SO-BAD", "sku": "SKU-B", "quantity": 1, "carrier": "FedEx", "shipping_service": "Ground"},
        ]
    )
    preview = analyze(wb, "Orders.xlsx")
    assert preview.has_blocking
    try:
        confirm_import(preview)
        raise AssertionError("confirm should reject blocking preview")
    except Exception as exc:
        assert "Carrier" in str(exc)
    assert Order.query.count() == 0


def test_order_import_does_not_require_optional_customer(db):
    wb = build_orders_workbook(
        [{"client": "ACME", "warehouse": "WH1", "order_type": "B2C", "order_number": "SO-NC", "sku": "SKU-A", "quantity": 1}]
    )
    import_orders(wb, "Orders.xlsx")
    assert Order.query.filter_by(order_number="SO-NC").count() == 1


def test_order_two_step_import_via_http(admin_client, db):
    wb = build_orders_workbook(
        [{"client": "ACME", "warehouse": "WH1", "order_type": "B2C", "order_number": "SO-HTTP2", "sku": "SKU-A", "quantity": 1}]
    )
    data = {"file": (io.BytesIO(wb.read()), "Orders.xlsx")}
    resp = admin_client.post(
        "/orders/import/preview", data=data, content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert Order.query.filter_by(order_number="SO-HTTP2").count() == 0
    html = resp.get_data(as_text=True)
    assert "Confirm Import" in html
    resp = admin_client.post("/orders/import/confirm", follow_redirects=True)
    assert resp.status_code == 200
    assert Order.query.filter_by(order_number="SO-HTTP2").count() == 1


def test_regular_user_cannot_upload_or_run_allocation_without_rights(app, db):
    create_user("viewer", role="USER", perms=["ORDERS_VIEW", "DASHBOARD_VIEW"])
    c = app.test_client()
    login(c, "viewer")
    assert c.get("/orders/import").status_code == 403
    assert c.post("/orders/import/preview").status_code == 403
    order = make_order("SO-NA", lines=[("SKU-A", 1)])
    resp = c.post(f"/orders/allocation-report?client_id={order.client_id}")
    assert resp.status_code == 403


def test_allocation_report_requires_client(admin_client):
    html = admin_client.get("/orders/allocation-report").get_data(as_text=True)
    assert "Select a client to view the allocation report" in html


def test_run_allocation_uses_client_warehouse_not_order_type(admin_client, db):
    from app.services.allocation import auto_allocate_order

    order = make_order("SO-ECOM", lines=[("SKU-A", 1)], order_type="ECOM")
    transition(order, OrderStatus.VALIDATED)
    db.session.commit()
    make_unit("U-ECOM-1", "SKU-A", client="ACME", warehouse="WH1")
    result = auto_allocate_order(order)
    db.session.commit()
    assert result["result"] == "FULL"
    assert order.pick_ticket is not None
    assert order.pick_ticket.pick_ticket_number.startswith("PT-")
