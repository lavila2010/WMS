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
        conn.execute(text(statements[0]))
        if tables:
            for stmt in statements[1:]:
                conn.execute(text(stmt))
