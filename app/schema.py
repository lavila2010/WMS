"""V2 additive schema objects (sequence + same-client mapping trigger)."""

from __future__ import annotations

from sqlalchemy import text

from .extensions import db


def ensure_v2_schema() -> None:
    try:
        bind = db.engine
    except Exception:
        return
    statements = [
        "CREATE SEQUENCE IF NOT EXISTS client_code_seq START WITH 1 INCREMENT BY 1",
        """
        CREATE OR REPLACE FUNCTION enforce_dw_same_client()
        RETURNS trigger AS $$
        DECLARE d_client INT; w_client INT;
        BEGIN
          SELECT client_id INTO d_client FROM divisions WHERE id = NEW.division_id;
          SELECT client_id INTO w_client FROM warehouses WHERE id = NEW.warehouse_id;
          IF d_client IS NULL OR w_client IS NULL
             OR d_client <> w_client OR NEW.client_id <> d_client THEN
            RAISE EXCEPTION 'cross-client division/warehouse mapping rejected';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """,
        "DROP TRIGGER IF EXISTS trg_dw_same_client ON division_warehouses",
        """
        CREATE TRIGGER trg_dw_same_client
        BEFORE INSERT OR UPDATE ON division_warehouses
        FOR EACH ROW EXECUTE FUNCTION enforce_dw_same_client()
        """,
    ]
    with bind.begin() as conn:
        tables = conn.execute(
            text("SELECT to_regclass('public.division_warehouses')")
        ).scalar()
        units = conn.execute(text("SELECT to_regclass('public.inventory_units')")).scalar()
        conn.execute(text(statements[0]))
        if units:
            conn.execute(
                text("ALTER TABLE inventory_units ADD COLUMN IF NOT EXISTS description VARCHAR(255)")
            )
        batches = conn.execute(text("SELECT to_regclass('public.import_batches')")).scalar()
        if batches:
            for stmt in (
                "ALTER TABLE import_batches ADD COLUMN IF NOT EXISTS source_storage_key VARCHAR(512)",
                "ALTER TABLE import_batches ADD COLUMN IF NOT EXISTS rows_validated INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE import_batches ADD COLUMN IF NOT EXISTS units_expected INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE import_batches ADD COLUMN IF NOT EXISTS units_created INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE import_batches ADD COLUMN IF NOT EXISTS transactions_created INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE import_batches ADD COLUMN IF NOT EXISTS current_source_row INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE import_batches ADD COLUMN IF NOT EXISTS progress_percent INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE import_batches ADD COLUMN IF NOT EXISTS unique_upc_count INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE import_batches ADD COLUMN IF NOT EXISTS unique_style_count INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE import_batches ADD COLUMN IF NOT EXISTS unique_location_count INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE import_batches ADD COLUMN IF NOT EXISTS orders_expected INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE import_batches ADD COLUMN IF NOT EXISTS order_lines_expected INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE import_batches ADD COLUMN IF NOT EXISTS orders_created INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE import_batches ADD COLUMN IF NOT EXISTS order_lines_created INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE import_batches ADD COLUMN IF NOT EXISTS started_at TIMESTAMP",
                "ALTER TABLE import_batches ADD COLUMN IF NOT EXISTS completed_at TIMESTAMP",
                "ALTER TABLE import_batches ADD COLUMN IF NOT EXISTS failed_at TIMESTAMP",
                "ALTER TABLE import_batches ADD COLUMN IF NOT EXISTS error_message TEXT",
            ):
                conn.execute(text(stmt))
        orders = conn.execute(text("SELECT to_regclass('public.orders')")).scalar()
        if orders:
            conn.execute(text("ALTER TABLE orders DROP CONSTRAINT IF EXISTS uq_order_client_number"))
            conn.execute(
                text(
                    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS destination_sequence INTEGER NOT NULL DEFAULT 1"
                )
            )
            conn.execute(
                text(
                    """
                    DO $$
                    BEGIN
                      IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint WHERE conname = 'uq_order_client_number_dest'
                      ) THEN
                        ALTER TABLE orders
                          ADD CONSTRAINT uq_order_client_number_dest
                          UNIQUE (client_id, client_order_number, destination_sequence);
                      END IF;
                    END $$
                    """
                )
            )
        pick_tickets = conn.execute(text("SELECT to_regclass('public.pick_tickets')")).scalar()
        if pick_tickets:
            conn.execute(
                text("UPDATE pick_tickets SET status = 'OPEN' WHERE status = 'ACTIVE'")
            )
        txns = conn.execute(text("SELECT to_regclass('public.inventory_transactions')")).scalar()
        if txns:
            conn.execute(
                text(
                    "ALTER TABLE inventory_transactions ADD COLUMN IF NOT EXISTS import_batch_id INTEGER "
                    "REFERENCES import_batches(id)"
                )
            )
        if batches:
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS inventory_import_rows (
                        id SERIAL PRIMARY KEY,
                        import_batch_id INTEGER NOT NULL REFERENCES import_batches(id),
                        source_row_number INTEGER NOT NULL,
                        upc VARCHAR(64),
                        sku VARCHAR(64),
                        description VARCHAR(255),
                        style VARCHAR(64),
                        color VARCHAR(64),
                        size VARCHAR(32),
                        quantity INTEGER NOT NULL DEFAULT 0,
                        location VARCHAR(64),
                        validation_status VARCHAR(16) NOT NULL DEFAULT 'INVALID',
                        validation_error TEXT
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS order_import_rows (
                        id SERIAL PRIMARY KEY,
                        import_batch_id INTEGER NOT NULL REFERENCES import_batches(id),
                        source_row_number INTEGER NOT NULL,
                        warehouse VARCHAR(64),
                        warehouse_id INTEGER REFERENCES warehouses(id),
                        raw_order_number VARCHAR(64),
                        customer VARCHAR(255),
                        customer_address VARCHAR(512),
                        customer_phone VARCHAR(64),
                        upc VARCHAR(64),
                        qty INTEGER NOT NULL DEFAULT 0,
                        carrier VARCHAR(128),
                        shipping_service VARCHAR(128),
                        sku VARCHAR(64),
                        description VARCHAR(255),
                        validation_status VARCHAR(16) NOT NULL DEFAULT 'INVALID',
                        validation_error TEXT
                    )
                    """
                )
            )
        for idx in (
            "CREATE INDEX IF NOT EXISTS ix_import_batches_status ON import_batches (status)",
            "CREATE INDEX IF NOT EXISTS ix_import_batches_cwc ON import_batches (client_id, warehouse_id, created_at)",
            "CREATE INDEX IF NOT EXISTS ix_units_cwus ON inventory_units (client_id, warehouse_id, upc, status)",
            "CREATE INDEX IF NOT EXISTS ix_units_import_batch ON inventory_units (import_batch_id)",
            "CREATE INDEX IF NOT EXISTS ix_units_cwl ON inventory_units (client_id, warehouse_id, location)",
            "CREATE INDEX IF NOT EXISTS ix_txn_import_batch ON inventory_transactions (import_batch_id)",
            "CREATE INDEX IF NOT EXISTS ix_txn_cwu ON inventory_transactions (client_id, warehouse_id, upc)",
            "CREATE INDEX IF NOT EXISTS ix_inv_import_rows_batch ON inventory_import_rows (import_batch_id)",
            "CREATE INDEX IF NOT EXISTS ix_inv_import_rows_batch_status ON inventory_import_rows (import_batch_id, validation_status)",
            "CREATE INDEX IF NOT EXISTS ix_ord_import_rows_batch ON order_import_rows (import_batch_id)",
            "CREATE INDEX IF NOT EXISTS ix_ord_import_rows_batch_status ON order_import_rows (import_batch_id, validation_status)",
            "CREATE INDEX IF NOT EXISTS ix_orders_import_batch ON orders (import_batch_id)",
            "CREATE INDEX IF NOT EXISTS ix_orders_client_number ON orders (client_id, client_order_number)",
        ):
            try:
                conn.execute(text(idx))
            except Exception:
                pass
        if tables:
            for stmt in statements[1:]:
                conn.execute(text(stmt))
