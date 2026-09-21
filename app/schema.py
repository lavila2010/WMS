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
                "ALTER TABLE import_batches ADD COLUMN IF NOT EXISTS last_progress_at TIMESTAMP",
                "ALTER TABLE import_batches ADD COLUMN IF NOT EXISTS last_executor VARCHAR(32)",
            ):
                conn.execute(text(stmt))
        orders = conn.execute(text("SELECT to_regclass('public.orders')")).scalar()
        if orders:
            has_old_uq = conn.execute(
                text("SELECT 1 FROM pg_constraint WHERE conname = 'uq_order_client_number'")
            ).scalar()
            if has_old_uq:
                conn.execute(text("ALTER TABLE orders DROP CONSTRAINT IF EXISTS uq_order_client_number"))
            conn.execute(
                text(
                    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS destination_sequence INTEGER NOT NULL DEFAULT 1"
                )
            )
            has_client_order_number = conn.execute(
                text(
                    """
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = 'orders'
                      AND column_name = 'client_order_number'
                    """
                )
            ).scalar()
            if has_client_order_number:
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
            conn.execute(
                text(
                    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS shipping_status VARCHAR(24) NOT NULL DEFAULT 'NOT_READY'"
                )
            )
            conn.execute(
                text(
                    """
                    UPDATE orders
                    SET shipping_status = 'PENDING_TRACKING'
                    WHERE status = 'CLOSED' AND shipping_status = 'NOT_READY'
                    """
                )
            )
        pick_tickets = conn.execute(text("SELECT to_regclass('public.pick_tickets')")).scalar()
        if pick_tickets:
            conn.execute(
                text("UPDATE pick_tickets SET status = 'OPEN' WHERE status = 'ACTIVE'")
            )
        cartons = conn.execute(text("SELECT to_regclass('public.cartons')")).scalar()
        if cartons:
            for stmt in (
                "ALTER TABLE cartons ADD COLUMN IF NOT EXISTS tracking_number VARCHAR(64)",
                "ALTER TABLE cartons ADD COLUMN IF NOT EXISTS tracking_carrier VARCHAR(16)",
                "ALTER TABLE cartons ADD COLUMN IF NOT EXISTS tracking_entered_at TIMESTAMP",
                "ALTER TABLE cartons ADD COLUMN IF NOT EXISTS tracking_entered_by_user_id INTEGER REFERENCES users(id)",
                "ALTER TABLE cartons ADD COLUMN IF NOT EXISTS tracking_validated_at TIMESTAMP",
                "ALTER TABLE cartons ADD COLUMN IF NOT EXISTS tracking_validated_by_user_id INTEGER REFERENCES users(id)",
                "ALTER TABLE cartons ADD COLUMN IF NOT EXISTS shipping_label_status VARCHAR(16) NOT NULL DEFAULT 'PENDING'",
                "ALTER TABLE cartons ADD COLUMN IF NOT EXISTS pick_ticket_id INTEGER REFERENCES pick_tickets(id)",
            ):
                conn.execute(text(stmt))
        if orders:
            for stmt in (
                "ALTER TABLE orders ADD COLUMN IF NOT EXISTS current_wave_number INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE orders ADD COLUMN IF NOT EXISTS partial_approved_wave INTEGER",
                "ALTER TABLE orders ADD COLUMN IF NOT EXISTS partial_allocation_approved_at TIMESTAMP",
                "ALTER TABLE orders ADD COLUMN IF NOT EXISTS partial_allocation_approved_by_user_id INTEGER REFERENCES users(id)",
                "ALTER TABLE orders ADD COLUMN IF NOT EXISTS last_allocation_attempt_at TIMESTAMP",
            ):
                conn.execute(text(stmt))
        allocs = conn.execute(text("SELECT to_regclass('public.allocations')")).scalar()
        if allocs:
            for stmt in (
                "ALTER TABLE allocations ADD COLUMN IF NOT EXISTS wave_number INTEGER NOT NULL DEFAULT 1",
                "ALTER TABLE allocations ADD COLUMN IF NOT EXISTS pick_ticket_id INTEGER REFERENCES pick_tickets(id)",
            ):
                conn.execute(text(stmt))
        if pick_tickets:
            for conname in ("pick_tickets_order_id_key", "uq_pick_tickets_order_id"):
                exists = conn.execute(
                    text("SELECT 1 FROM pg_constraint WHERE conname = :n"),
                    {"n": conname},
                ).scalar()
                if exists:
                    conn.execute(text(f"ALTER TABLE pick_tickets DROP CONSTRAINT IF EXISTS {conname}"))
            conn.execute(text("ALTER TABLE pick_tickets ADD COLUMN IF NOT EXISTS revision_number INTEGER NOT NULL DEFAULT 1"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_pick_tickets_order ON pick_tickets (order_id)"))
            has_ticket_sequence = conn.execute(
                text(
                    """
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = 'pick_tickets'
                      AND column_name = 'ticket_sequence'
                    """
                )
            ).scalar()
            if has_ticket_sequence and allocs:
                conn.execute(
                    text(
                        """
                        UPDATE allocations a
                        SET pick_ticket_id = pt.id
                        FROM pick_tickets pt
                        WHERE a.order_id = pt.order_id
                          AND a.pick_ticket_id IS NULL
                          AND a.wave_number = pt.ticket_sequence
                          AND a.created_at <= pt.created_at
                        """
                    )
                )
            if has_ticket_sequence and cartons:
                conn.execute(
                    text(
                        """
                        UPDATE cartons c
                        SET pick_ticket_id = pt.id
                        FROM pick_tickets pt
                        WHERE c.order_id = pt.order_id
                          AND c.pick_ticket_id IS NULL
                          AND c.created_at <= pt.created_at
                          AND pt.ticket_sequence = (
                            SELECT MIN(pt2.ticket_sequence) FROM pick_tickets pt2 WHERE pt2.order_id = c.order_id
                          )
                        """
                    )
                )
        docs = conn.execute(text("SELECT to_regclass('public.documents')")).scalar()
        if docs:
            conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS pick_ticket_id INTEGER REFERENCES pick_tickets(id)"))
        clients = conn.execute(text("SELECT to_regclass('public.clients')")).scalar()
        units = conn.execute(text("SELECT to_regclass('public.inventory_units')")).scalar()
        if clients and units:
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS inventory_issues (
                        id SERIAL PRIMARY KEY,
                        client_id INTEGER NOT NULL REFERENCES clients(id),
                        warehouse_id INTEGER NOT NULL REFERENCES warehouses(id),
                        inventory_unit_id INTEGER NOT NULL REFERENCES inventory_units(id),
                        upc VARCHAR(64) NOT NULL,
                        original_location VARCHAR(64) NOT NULL,
                        current_location VARCHAR(64),
                        source_order_id INTEGER REFERENCES orders(id),
                        source_order_line_id INTEGER REFERENCES order_lines(id),
                        source_pick_ticket_id INTEGER REFERENCES pick_tickets(id),
                        replacement_inventory_unit_id INTEGER REFERENCES inventory_units(id),
                        issue_type VARCHAR(32) NOT NULL DEFAULT 'PICK_UNIT_NOT_FOUND',
                        status VARCHAR(16) NOT NULL DEFAULT 'OPEN',
                        reported_by_user_id INTEGER REFERENCES users(id),
                        reported_at TIMESTAMP NOT NULL,
                        resolved_by_user_id INTEGER REFERENCES users(id),
                        resolved_at TIMESTAMP,
                        resolution_note TEXT,
                        created_at TIMESTAMP NOT NULL,
                        updated_at TIMESTAMP NOT NULL
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS pick_ticket_print_batches (
                        id SERIAL PRIMARY KEY,
                        created_by_user_id INTEGER REFERENCES users(id),
                        created_at TIMESTAMP NOT NULL,
                        ticket_count INTEGER NOT NULL DEFAULT 0,
                        total_units INTEGER NOT NULL DEFAULT 0,
                        sort_order VARCHAR(64),
                        filter_context VARCHAR(512),
                        document_id INTEGER REFERENCES documents(id)
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS pick_ticket_print_batch_items (
                        id SERIAL PRIMARY KEY,
                        batch_id INTEGER NOT NULL REFERENCES pick_ticket_print_batches(id),
                        pick_ticket_id INTEGER NOT NULL REFERENCES pick_tickets(id),
                        sequence_in_batch INTEGER NOT NULL DEFAULT 1
                    )
                    """
                )
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
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS worker_heartbeats (
                        id SERIAL PRIMARY KEY,
                        worker_name VARCHAR(64) NOT NULL,
                        worker_type VARCHAR(32) NOT NULL DEFAULT 'import',
                        instance_id VARCHAR(128) NOT NULL,
                        last_seen_at TIMESTAMP NOT NULL,
                        status VARCHAR(20) NOT NULL DEFAULT 'ONLINE',
                        current_batch_id INTEGER REFERENCES import_batches(id),
                        created_at TIMESTAMP NOT NULL,
                        updated_at TIMESTAMP NOT NULL,
                        UNIQUE (worker_name, instance_id)
                    )
                    """
                )
            )
        for idx in (
            "CREATE INDEX IF NOT EXISTS ix_import_batches_status ON import_batches (status)",
            "CREATE INDEX IF NOT EXISTS ix_import_batches_cwc ON import_batches (client_id, warehouse_id, created_at)",
            "CREATE INDEX IF NOT EXISTS ix_units_cwus ON inventory_units (client_id, warehouse_id, upc, status)",
            "CREATE INDEX IF NOT EXISTS ix_units_cwuls ON inventory_units (client_id, warehouse_id, upc, location, status)",
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
            "CREATE INDEX IF NOT EXISTS ix_orders_shipping_status ON orders (shipping_status)",
            "CREATE INDEX IF NOT EXISTS ix_orders_closed_at ON orders (closed_at)",
            "CREATE INDEX IF NOT EXISTS ix_orders_client_div_closed ON orders (client_id, division_id, closed_at)",
            "CREATE INDEX IF NOT EXISTS ix_cartons_tracking_number ON cartons (tracking_number)",
            "CREATE INDEX IF NOT EXISTS ix_cartons_order_label ON cartons (order_id, shipping_label_status)",
            "CREATE INDEX IF NOT EXISTS ix_worker_heartbeats_seen ON worker_heartbeats (worker_type, last_seen_at)",
            "CREATE INDEX IF NOT EXISTS ix_alloc_ticket ON allocations (pick_ticket_id)",
            "CREATE INDEX IF NOT EXISTS ix_alloc_wave ON allocations (order_id, wave_number)",
            "CREATE INDEX IF NOT EXISTS ix_issues_status ON inventory_issues (status)",
            "CREATE INDEX IF NOT EXISTS ix_issues_client_wh ON inventory_issues (client_id, warehouse_id)",
            "CREATE INDEX IF NOT EXISTS ix_issues_upc ON inventory_issues (upc)",
            "CREATE INDEX IF NOT EXISTS ix_pt_batch_items_batch ON pick_ticket_print_batch_items (batch_id)",
            "CREATE INDEX IF NOT EXISTS ix_cartons_pick_ticket ON cartons (pick_ticket_id)",
        ):
            try:
                with conn.begin_nested():
                    conn.execute(text(idx))
            except Exception:
                pass
        if tables:
            for stmt in statements[1:]:
                conn.execute(text(stmt))
    from .services.fulfillment import repair_zero_current_wave_numbers

    repair_zero_current_wave_numbers()
