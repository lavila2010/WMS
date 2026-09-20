import io

import pytest
from openpyxl import Workbook

from app.models import Client, InventoryUnit, Order, OrderLine, Warehouse
from app.services.imports import (
    ImportError_,
    build_inventory_workbook,
    build_orders_workbook,
    import_inventory,
    import_orders,
)


def test_import_inventory_creates_scoped_units(db):
    wb = build_inventory_workbook(
        [
            {"client": "ACME", "warehouse": "WH1", "order_type": "B2C", "barcode": "IB-1", "sku": "SKU-A", "location": "A-01"},
            {"client": "ACME", "warehouse": "WH1", "order_type": "B2C", "barcode": "IB-2", "sku": "SKU-B"},
            {"client": "ACME", "warehouse": "WH1", "order_type": "B2C", "barcode": "IB-1", "sku": "SKU-A"},  # dup barcode
        ]
    )
    batch = import_inventory(wb, "Inventory.xlsx")
    assert batch.row_count == 2
    assert InventoryUnit.query.count() == 2
    u = InventoryUnit.query.filter_by(barcode="IB-1").first()
    assert u.client.code == "ACME" and u.warehouse.code == "WH1" and u.order_type.code == "B2C"


def test_import_inventory_multiple_clients_one_upload(db):
    wb = build_inventory_workbook(
        [
            {"client": "ACME", "warehouse": "WH1", "order_type": "B2C", "barcode": "M-1", "sku": "SKU-A"},
            {"client": "GLOBEX", "warehouse": "WHX", "order_type": "B2B", "barcode": "M-2", "sku": "SKU-A"},
        ]
    )
    import_inventory(wb, "Inventory.xlsx")
    assert Client.query.count() == 2
    assert InventoryUnit.query.filter_by(barcode="M-1").first().client.code == "ACME"
    assert InventoryUnit.query.filter_by(barcode="M-2").first().client.code == "GLOBEX"


def test_same_sku_across_clients_is_isolated(db):
    wb = build_inventory_workbook(
        [
            {"client": "ACME", "warehouse": "WH1", "order_type": "B2C", "barcode": "S-1", "sku": "SHARED"},
            {"client": "GLOBEX", "warehouse": "WHX", "order_type": "B2C", "barcode": "S-2", "sku": "SHARED"},
        ]
    )
    import_inventory(wb, "Inventory.xlsx")
    acme = Client.query.filter_by(code="ACME").first()
    globex = Client.query.filter_by(code="GLOBEX").first()
    acme_units = InventoryUnit.query.filter_by(sku="SHARED", client_id=acme.id).all()
    globex_units = InventoryUnit.query.filter_by(sku="SHARED", client_id=globex.id).all()
    assert len(acme_units) == 1 and len(globex_units) == 1
    assert acme_units[0].id != globex_units[0].id


def test_import_inventory_missing_column(db):
    wb = Workbook()
    wb.active.append(["Barcode", "SKU"])  # missing Client/Warehouse/OrderType
    wb.active.append(["X-1", "SKU-A"])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    with pytest.raises(ImportError_):
        import_inventory(buf, "bad.xlsx")


def test_import_orders_groups_lines_with_carrier_and_shipping(db):
    wb = build_orders_workbook(
        [
            {"client": "ACME", "warehouse": "WH1", "order_type": "B2C", "order_number": "SO-1", "customer": "Acme", "sku": "SKU-A", "quantity": 2, "carrier": "UPS", "shipping_service": "Ground"},
            {"client": "ACME", "warehouse": "WH1", "order_type": "B2C", "order_number": "SO-1", "customer": "Acme", "sku": "SKU-B", "quantity": 1, "carrier": "UPS", "shipping_service": "Ground"},
        ]
    )
    import_orders(wb, "Orders.xlsx")
    so1 = Order.query.filter_by(order_number="SO-1").first()
    assert so1.ordered_quantity == 3
    assert so1.carrier == "UPS"
    assert so1.shipping_service == "Ground"
    assert OrderLine.query.count() == 2


def test_import_orders_multiple_clients_one_upload(db):
    wb = build_orders_workbook(
        [
            {"client": "ACME", "warehouse": "WH1", "order_type": "B2C", "order_number": "SO-1", "sku": "SKU-A", "quantity": 1, "carrier": "UPS", "shipping_service": "Ground"},
            {"client": "GLOBEX", "warehouse": "WHX", "order_type": "B2B", "order_number": "SO-1", "sku": "SKU-A", "quantity": 1, "carrier": "FedEx", "shipping_service": "2Day"},
        ]
    )
    import_orders(wb, "Orders.xlsx")
    # same order number allowed for different clients
    assert Order.query.filter_by(order_number="SO-1").count() == 2


def test_import_orders_rejects_conflicting_carrier(db):
    wb = build_orders_workbook(
        [
            {"client": "ACME", "warehouse": "WH1", "order_type": "B2C", "order_number": "SO-9", "sku": "SKU-A", "quantity": 1, "carrier": "UPS", "shipping_service": "Ground"},
            {"client": "ACME", "warehouse": "WH1", "order_type": "B2C", "order_number": "SO-9", "sku": "SKU-B", "quantity": 1, "carrier": "FedEx", "shipping_service": "Ground"},
        ]
    )
    with pytest.raises(ImportError_):
        import_orders(wb, "Orders.xlsx")


def test_import_orders_rejects_conflicting_shipping_service(db):
    wb = build_orders_workbook(
        [
            {"client": "ACME", "warehouse": "WH1", "order_type": "B2C", "order_number": "SO-9", "sku": "SKU-A", "quantity": 1, "carrier": "UPS", "shipping_service": "Ground"},
            {"client": "ACME", "warehouse": "WH1", "order_type": "B2C", "order_number": "SO-9", "sku": "SKU-B", "quantity": 1, "carrier": "UPS", "shipping_service": "Express"},
        ]
    )
    with pytest.raises(ImportError_):
        import_orders(wb, "Orders.xlsx")
