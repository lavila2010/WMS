import io

from app.models import InventoryUnit, Order
from app.services.imports import build_inventory_workbook, build_orders_workbook


def test_anonymous_redirects_to_login(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_health_routes_public(client):
    assert client.get("/health").status_code == 200
    assert client.get("/health/db").status_code in (200, 503)


def test_authenticated_module_indexes_render(admin_client):
    for path in [
        "/",
        "/inventory/",
        "/inventory/overview",
        "/inventory/upload",
        "/inventory/search",
        "/inventory/transactions",
        "/inventory/import-history",
        "/inventory/exceptions",
        "/orders/",
        "/orders/upload",
        "/orders/import",
        "/orders/allocation-report",
        "/orders/pick-tickets",
        "/allocation/",
        "/processing/",
        "/reports/",
        "/kpi/orders",
        "/admin/users",
    ]:
        resp = admin_client.get(path, follow_redirects=True)
        assert resp.status_code == 200, path


def test_templates_download(admin_client):
    for path in ["/inventory/template", "/orders/template"]:
        resp = admin_client.get(path)
        assert resp.status_code == 200
        assert resp.data[:2] == b"PK"  # xlsx is a zip archive


def test_inventory_two_step_import_via_http(admin_client, db):
    wb = build_inventory_workbook(
        [{"client": "ACME", "warehouse": "WH1", "upc": "U1", "sku": "SKU-A", "barcode": "R-1", "location": "A-01"}]
    )
    data = {"file": (io.BytesIO(wb.read()), "Inventory.xlsx")}
    resp = admin_client.post(
        "/inventory/import/preview", data=data, content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert InventoryUnit.query.filter_by(barcode="R-1").count() == 0
    resp = admin_client.post("/inventory/import/confirm", follow_redirects=True)
    assert resp.status_code == 200
    assert InventoryUnit.query.filter_by(barcode="R-1").count() == 1


def test_import_orders_via_http(admin_client, db):
    wb = build_orders_workbook(
        [{"client": "ACME", "warehouse": "WH1", "order_type": "B2C", "order_number": "SO-HTTP", "sku": "SKU-A", "quantity": 3}]
    )
    data = {"file": (io.BytesIO(wb.read()), "Orders.xlsx")}
    resp = admin_client.post(
        "/orders/import", data=data, content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert Order.query.filter_by(order_number="SO-HTTP").count() == 1


def test_reports_search_by_invoice_number_route(admin_client, db):
    resp = admin_client.get("/reports/?invoice_number=INV-")
    assert resp.status_code == 200
