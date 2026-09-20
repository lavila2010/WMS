import pytest

from app.models import Order, OrderLine
from app.services.imports import (
    ImportError_,
    build_orders_workbook,
    import_orders,
)


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
