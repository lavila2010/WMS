"""Excel import service.

The ONLY Excel usage in the system:
    1. Inventory.xlsx import
    2. Orders.xlsx import

All imported data is persisted to PostgreSQL. Excel is never used as
operational storage.
"""

from __future__ import annotations

import io

import pandas as pd
from openpyxl import Workbook

from ..constants import ImportType, OrderStatus
from ..extensions import db
from ..models import ImportBatch, InventoryUnit, Order, OrderLine


class ImportError_(Exception):
    """Raised when an uploaded workbook is missing required columns/data."""


def _read_excel(source) -> pd.DataFrame:
    df = pd.read_excel(source, engine="openpyxl", dtype=str)
    df.columns = [str(c).strip().lower() for c in df.columns]
    return df.fillna("")


def _require_columns(df: pd.DataFrame, required: list[str], kind: str) -> None:
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ImportError_(
            f"{kind} workbook is missing required column(s): {', '.join(missing)}"
        )


def import_inventory(source, filename: str) -> ImportBatch:
    """Import Inventory.xlsx. Columns: barcode, sku, [description, location]."""
    df = _read_excel(source)
    _require_columns(df, ["barcode", "sku"], "Inventory")

    batch = ImportBatch(type=ImportType.INVENTORY, filename=filename, row_count=0)
    db.session.add(batch)
    db.session.flush()

    created = 0
    skipped = 0
    for _, row in df.iterrows():
        barcode = str(row.get("barcode", "")).strip()
        sku = str(row.get("sku", "")).strip()
        if not barcode or not sku:
            skipped += 1
            continue
        if InventoryUnit.query.filter_by(barcode=barcode).first() is not None:
            skipped += 1
            continue
        db.session.add(
            InventoryUnit(
                barcode=barcode,
                sku=sku,
                description=str(row.get("description", "")).strip() or None,
                location=str(row.get("location", "")).strip() or None,
                import_batch_id=batch.id,
            )
        )
        created += 1

    batch.row_count = created
    batch.message = f"Imported {created} units, skipped {skipped}."
    db.session.commit()
    return batch


def import_orders(source, filename: str) -> ImportBatch:
    """Import Orders.xlsx. Columns: order_number, sku, quantity, [customer, description]."""
    df = _read_excel(source)
    _require_columns(df, ["order_number", "sku", "quantity"], "Orders")

    batch = ImportBatch(type=ImportType.ORDERS, filename=filename, row_count=0)
    db.session.add(batch)
    db.session.flush()

    lines_created = 0
    orders_touched: set[str] = set()
    for _, row in df.iterrows():
        order_number = str(row.get("order_number", "")).strip()
        sku = str(row.get("sku", "")).strip()
        qty_raw = str(row.get("quantity", "")).strip()
        if not order_number or not sku or not qty_raw:
            continue
        try:
            quantity = int(float(qty_raw))
        except ValueError:
            continue

        order = Order.query.filter_by(order_number=order_number).first()
        if order is None:
            order = Order(
                order_number=order_number,
                customer=str(row.get("customer", "")).strip() or None,
                status=OrderStatus.NEW,
                import_batch_id=batch.id,
            )
            db.session.add(order)
            db.session.flush()
        orders_touched.add(order_number)

        db.session.add(
            OrderLine(
                order_id=order.id,
                sku=sku,
                description=str(row.get("description", "")).strip() or None,
                quantity=quantity,
            )
        )
        lines_created += 1

    batch.row_count = lines_created
    batch.message = (
        f"Imported {lines_created} order lines across "
        f"{len(orders_touched)} order(s)."
    )
    db.session.commit()
    return batch


# --- Workbook builders (used for downloadable templates and tests) ---


def build_inventory_workbook(rows: list[dict]) -> io.BytesIO:
    wb = Workbook()
    ws = wb.active
    ws.title = "Inventory"
    ws.append(["barcode", "sku", "description", "location"])
    for r in rows:
        ws.append([
            r.get("barcode", ""),
            r.get("sku", ""),
            r.get("description", ""),
            r.get("location", ""),
        ])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def build_orders_workbook(rows: list[dict]) -> io.BytesIO:
    wb = Workbook()
    ws = wb.active
    ws.title = "Orders"
    ws.append(["order_number", "customer", "sku", "quantity", "description"])
    for r in rows:
        ws.append([
            r.get("order_number", ""),
            r.get("customer", ""),
            r.get("sku", ""),
            r.get("quantity", ""),
            r.get("description", ""),
        ])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf
