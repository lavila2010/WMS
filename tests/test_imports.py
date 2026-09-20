import pytest

from app.models import InventoryUnit, Order, OrderLine
from app.services.imports import (
    ImportError_,
    build_inventory_workbook,
    build_orders_workbook,
    import_inventory,
    import_orders,
)


def test_import_inventory_creates_units(db):
    wb = build_inventory_workbook(
        [
            {"barcode": "IB-1", "sku": "SKU-A", "description": "A", "location": "A-01"},
            {"barcode": "IB-2", "sku": "SKU-B", "location": "B-02"},
            {"barcode": "IB-1", "sku": "SKU-A"},  # duplicate barcode -> skipped
        ]
    )
    batch = import_inventory(wb, "Inventory.xlsx")
    assert batch.row_count == 2
    assert InventoryUnit.query.count() == 2


def test_import_inventory_missing_column(db):
    import io
    from openpyxl import Workbook

    wb = Workbook()
    wb.active.append(["barcode"])  # missing sku
    wb.active.append(["X-1"])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    with pytest.raises(ImportError_):
        import_inventory(buf, "bad.xlsx")


def test_import_orders_groups_lines(db):
    wb = build_orders_workbook(
        [
            {"order_number": "SO-1", "customer": "Acme", "sku": "SKU-A", "quantity": 2},
            {"order_number": "SO-1", "customer": "Acme", "sku": "SKU-B", "quantity": 1},
            {"order_number": "SO-2", "customer": "Beta", "sku": "SKU-A", "quantity": 5},
        ]
    )
    batch = import_orders(wb, "Orders.xlsx")
    assert batch.row_count == 3
    assert Order.query.count() == 2
    so1 = Order.query.filter_by(order_number="SO-1").first()
    assert so1.ordered_quantity == 3
    assert OrderLine.query.count() == 3
