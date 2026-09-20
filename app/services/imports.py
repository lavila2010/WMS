"""Excel import service (multi-client aware).

The ONLY Excel usage in the system:
    1. Inventory.xlsx import
    2. Orders.xlsx import

All imported data is persisted to PostgreSQL, scoped by
Client + Warehouse + Order Type. A single upload may contain multiple
clients, warehouses, and order types.
"""

from __future__ import annotations

import io
import re

import pandas as pd
from openpyxl import Workbook

from ..constants import ImportType, OrderStatus, UnitStatus
from ..extensions import db
from ..models import ImportBatch, InventoryUnit, Order, OrderLine
from .scope import resolve_scope


class ImportError_(Exception):
    """Raised when an uploaded workbook is invalid (missing columns, or
    conflicting header values within a single operational order)."""


def _norm(col: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(col).lower())


def _read_excel(source) -> pd.DataFrame:
    df = pd.read_excel(source, engine="openpyxl", dtype=str)
    df.columns = [_norm(c) for c in df.columns]
    return df.fillna("")


def _require_columns(df: pd.DataFrame, required: list[str], kind: str) -> None:
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ImportError_(
            f"{kind} workbook is missing required column(s): {', '.join(missing)}"
        )


def _val(row, key) -> str:
    return str(row.get(key, "")).strip()


def _merge_header(existing: dict, candidate: dict, key: str) -> None:
    """Merge a header field, rejecting conflicting non-empty values."""
    field, label, order_key = key, candidate["_label"], candidate["_order"]
    new = candidate.get(field, "")
    if not new:
        return
    cur = existing.get(field, "")
    if cur and cur != new:
        raise ImportError_(
            f"Conflicting {label} for order {order_key}: "
            f"'{cur}' vs '{new}'. All lines of an order must agree."
        )
    existing[field] = new


def import_orders(source, filename: str) -> ImportBatch:
    """Import Orders.xlsx.

    Columns: Client, Warehouse, OrderType, OrderNumber, Customer, SKU,
    Description, QtyOrdered, Carrier, ShippingService.

    Rows sharing (Client, Warehouse, OrderType, OrderNumber) form one
    operational order; their Customer/Carrier/ShippingService must agree.
    """
    df = _read_excel(source)
    _require_columns(
        df,
        ["client", "warehouse", "ordertype", "ordernumber", "sku", "qtyordered"],
        "Orders",
    )

    # First pass: group + validate consistent header fields (no DB writes yet).
    groups: dict[tuple, dict] = {}
    for _, row in df.iterrows():
        client_code = _val(row, "client")
        warehouse_code = _val(row, "warehouse")
        order_type_code = _val(row, "ordertype")
        order_number = _val(row, "ordernumber")
        sku = _val(row, "sku")
        qty_raw = _val(row, "qtyordered")
        if not (client_code and warehouse_code and order_type_code and order_number and sku and qty_raw):
            continue
        try:
            quantity = int(float(qty_raw))
        except ValueError:
            continue

        key = (client_code, warehouse_code, order_type_code, order_number)
        group = groups.setdefault(
            key,
            {
                "client": client_code,
                "warehouse": warehouse_code,
                "order_type": order_type_code,
                "order_number": order_number,
                "customer": "",
                "carrier": "",
                "shipping_service": "",
                "lines": [],
            },
        )
        candidate = {
            "customer": _val(row, "customer"),
            "carrier": _val(row, "carrier"),
            "shipping_service": _val(row, "shippingservice"),
            "_label": None,
            "_order": order_number,
        }
        for field, label in (
            ("customer", "Customer"),
            ("carrier", "Carrier"),
            ("shipping_service", "Shipping Service"),
        ):
            candidate["_label"] = label
            _merge_header(group, candidate, field)
        group["lines"].append(
            {"sku": sku, "description": _val(row, "description") or None, "quantity": quantity}
        )

    # Second pass: persist.
    batch = ImportBatch(type=ImportType.ORDERS, filename=filename, row_count=0)
    db.session.add(batch)
    db.session.flush()

    lines_created = 0
    orders_created = 0
    orders_skipped = 0
    for group in groups.values():
        client, warehouse, order_type = resolve_scope(
            group["client"], group["warehouse"], group["order_type"]
        )
        existing = Order.query.filter_by(
            client_id=client.id,
            warehouse_id=warehouse.id,
            order_type_id=order_type.id,
            order_number=group["order_number"],
        ).first()
        if existing is not None:
            orders_skipped += 1
            continue

        order = Order(
            order_number=group["order_number"],
            customer=group["customer"] or None,
            carrier=group["carrier"] or None,
            shipping_service=group["shipping_service"] or None,
            status=OrderStatus.NEW,
            client_id=client.id,
            warehouse_id=warehouse.id,
            order_type_id=order_type.id,
            import_batch_id=batch.id,
        )
        db.session.add(order)
        db.session.flush()
        orders_created += 1
        for line in group["lines"]:
            db.session.add(
                OrderLine(
                    order_id=order.id,
                    sku=line["sku"],
                    description=line["description"],
                    quantity=line["quantity"],
                )
            )
            lines_created += 1

    batch.row_count = lines_created
    batch.message = (
        f"Imported {orders_created} order(s), {lines_created} line(s); "
        f"skipped {orders_skipped} existing order(s)."
    )
    db.session.commit()
    return batch


# --- Workbook builders (used for downloadable templates and tests) ---

INVENTORY_HEADERS = ["Client", "Warehouse", "UPC", "SKU", "Description", "Barcode", "Location"]
ORDER_HEADERS = [
    "Client", "Warehouse", "OrderType", "OrderNumber", "Customer",
    "SKU", "Description", "QtyOrdered", "Carrier", "ShippingService",
]


def build_inventory_workbook(rows: list[dict]) -> io.BytesIO:
    wb = Workbook()
    ws = wb.active
    ws.title = "Inventory"
    ws.append(INVENTORY_HEADERS)
    for r in rows:
        ws.append([
            r.get("client", ""), r.get("warehouse", ""), r.get("upc", ""),
            r.get("sku", ""), r.get("description", ""),
            r.get("barcode", ""), r.get("location", ""),
        ])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def build_orders_workbook(rows: list[dict]) -> io.BytesIO:
    wb = Workbook()
    ws = wb.active
    ws.title = "Orders"
    ws.append(ORDER_HEADERS)
    for r in rows:
        ws.append([
            r.get("client", ""), r.get("warehouse", ""), r.get("order_type", r.get("ordertype", "")),
            r.get("order_number", r.get("ordernumber", "")), r.get("customer", ""),
            r.get("sku", ""), r.get("description", ""),
            r.get("quantity", r.get("qtyordered", "")),
            r.get("carrier", ""), r.get("shipping_service", r.get("shippingservice", "")),
        ])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf
