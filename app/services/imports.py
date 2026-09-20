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

from ..constants import ImportType
from ..models import ImportBatch


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
    """Import Orders.xlsx (one-shot wrapper around the two-step service)."""
    from .order_import import import_orders as _import_orders

    return _import_orders(source, filename)


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
