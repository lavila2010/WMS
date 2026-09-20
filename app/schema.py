"""Additive schema patches for existing PostgreSQL databases."""

from __future__ import annotations

from sqlalchemy import inspect, text

from .extensions import db


_ORDER_COLUMNS = {
    "closed_at": "TIMESTAMP",
    "processing_user_id": "INTEGER",
    "processing_username": "VARCHAR(64)",
    "processing_started_at": "TIMESTAMP",
}

_BOX_COLUMNS = {
    "dimension_unit": "VARCHAR(8) DEFAULT 'in'",
    "weight_unit": "VARCHAR(8) DEFAULT 'lb'",
    "reweigh_required": "BOOLEAN DEFAULT FALSE",
}


def ensure_processing_columns() -> None:
    try:
        bind = db.engine
        inspector = inspect(bind)
        tables = set(inspector.get_table_names())
    except Exception:
        return
    if "orders" not in tables or "boxes" not in tables:
        return
    order_cols = {c["name"] for c in inspector.get_columns("orders")}
    box_cols = {c["name"] for c in inspector.get_columns("boxes")}
    statements = []
    for name, spec in _ORDER_COLUMNS.items():
        if name not in order_cols:
            statements.append(f"ALTER TABLE orders ADD COLUMN {name} {spec}")
    for name, spec in _BOX_COLUMNS.items():
        if name not in box_cols:
            statements.append(f"ALTER TABLE boxes ADD COLUMN {name} {spec}")
    if not statements:
        return
    with bind.begin() as conn:
        for stmt in statements:
            conn.execute(text(stmt))
