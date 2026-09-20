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


def ensure_kpi_schema() -> None:
    """Create Division + channel columns/indexes for existing databases.

    Schema addition (documented): there was no Division model. Warehouse is a
    physical location and OrderType is the order channel, so a client-scoped
    ``divisions`` table and ``orders.division_id`` are required.

    Indexes added (only if missing):
    - ix_orders_created_at: inclusive date-range KPI filter
    - ix_orders_division_id: Client + Division grouping
    - ix_order_lines_order_id: unit SUM without scanning all lines
    """
    from .constants import normalize_channel
    from .models import Client, Division, Order, OrderType

    try:
        bind = db.engine
        inspector = inspect(bind)
        tables = set(inspector.get_table_names())
    except Exception:
        return
    if "orders" not in tables:
        return

    statements = []
    if "divisions" not in tables:
        statements.extend(
            [
                """
                CREATE TABLE divisions (
                    id SERIAL PRIMARY KEY,
                    client_id INTEGER NOT NULL REFERENCES clients(id),
                    code VARCHAR(32) NOT NULL,
                    name VARCHAR(255) NOT NULL,
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    CONSTRAINT uq_division_client_code UNIQUE (client_id, code)
                )
                """,
                "CREATE INDEX IF NOT EXISTS ix_divisions_client_id ON divisions (client_id)",
                "CREATE INDEX IF NOT EXISTS ix_divisions_code ON divisions (code)",
            ]
        )
        tables.add("divisions")

    order_cols = {c["name"] for c in inspector.get_columns("orders")}
    if "division_id" not in order_cols:
        statements.append(
            "ALTER TABLE orders ADD COLUMN division_id INTEGER REFERENCES divisions(id)"
        )
    ot_cols = {c["name"] for c in inspector.get_columns("order_types")} if "order_types" in tables else set()
    if "channel" not in ot_cols:
        statements.append("ALTER TABLE order_types ADD COLUMN channel VARCHAR(16)")

    existing_indexes = set()
    for table in ("orders", "order_lines"):
        if table in tables:
            existing_indexes.update(i["name"] for i in inspector.get_indexes(table))
    if "ix_orders_created_at" not in existing_indexes:
        statements.append("CREATE INDEX IF NOT EXISTS ix_orders_created_at ON orders (created_at)")
    if "ix_orders_division_id" not in existing_indexes:
        statements.append("CREATE INDEX IF NOT EXISTS ix_orders_division_id ON orders (division_id)")
    if "ix_order_lines_order_id" not in existing_indexes and "order_lines" in tables:
        statements.append("CREATE INDEX IF NOT EXISTS ix_order_lines_order_id ON order_lines (order_id)")
    if "ix_order_types_channel" not in existing_indexes and "order_types" in tables:
        statements.append("CREATE INDEX IF NOT EXISTS ix_order_types_channel ON order_types (channel)")

    if statements:
        with bind.begin() as conn:
            for stmt in statements:
                conn.execute(text(stmt))

    for ot in OrderType.query.all():
        if not ot.channel:
            ot.channel = normalize_channel(ot.code)
    for client in Client.query.all():
        existing = Division.query.filter_by(client_id=client.id, code="MAIN").first()
        if existing is None:
            existing = Division(client_id=client.id, code="MAIN", name="Main")
            db.session.add(existing)
            db.session.flush()
        Order.query.filter(
            Order.client_id == client.id, Order.division_id.is_(None)
        ).update({"division_id": existing.id}, synchronize_session=False)
    db.session.commit()
