"""Cross-phase data integrity assertions. Fail closed."""

from __future__ import annotations

from sqlalchemy import text

from ..constants import UnitStatus
from ..extensions import db
from ..models import InventoryUnit


class IntegrityError(AssertionError):
    pass


def assert_invariants(client_id=None):
    _no_cross_client_units(client_id)
    _every_unit_has_ledger(client_id)
    _unit_status_matches_latest_ledger(client_id)
    _on_hand_definition(client_id)
    _one_active_allocation_per_unit(client_id)
    _one_carton_content_per_unit(client_id)
    _order_masters_same_client(client_id)
    _mappings_same_client()


def _no_cross_client_units(client_id):
    sql = """
        SELECT COUNT(*) FROM inventory_units u
        JOIN warehouses w ON w.id = u.warehouse_id
        WHERE w.client_id <> u.client_id
    """
    params = {}
    if client_id:
        sql += " AND u.client_id = :client_id"
        params["client_id"] = client_id
    count = db.session.execute(text(sql), params).scalar()
    if count:
        raise IntegrityError(f"{count} units have warehouse.client_id ≠ unit.client_id")


def _every_unit_has_ledger(client_id):
    sql = """
        SELECT COUNT(*) FROM inventory_units u
        LEFT JOIN inventory_transactions t ON t.inventory_unit_id = u.id
        WHERE t.id IS NULL
    """
    params = {}
    if client_id:
        sql += " AND u.client_id = :client_id"
        params["client_id"] = client_id
    count = db.session.execute(text(sql), params).scalar()
    if count:
        raise IntegrityError(f"{count} units have no inventory_transactions row")


def _unit_status_matches_latest_ledger(client_id):
    sql = """
        SELECT COUNT(*) FROM inventory_units u
        JOIN LATERAL (
            SELECT t.to_status
            FROM inventory_transactions t
            WHERE t.inventory_unit_id = u.id
            ORDER BY t.id DESC
            LIMIT 1
        ) latest ON TRUE
        WHERE latest.to_status <> u.status
    """
    params = {}
    if client_id:
        sql += " AND u.client_id = :client_id"
        params["client_id"] = client_id
    count = db.session.execute(text(sql), params).scalar()
    if count:
        raise IntegrityError(f"{count} units have status that does not match latest ledger to_status")


def _on_hand_definition(client_id):
    query = InventoryUnit.query
    if client_id:
        query = query.filter_by(client_id=client_id)
    available = query.filter_by(status=UnitStatus.AVAILABLE).count()
    reserved = query.filter_by(status=UnitStatus.RESERVED).count()
    packed = query.filter_by(status=UnitStatus.PACKED).count()
    on_hand = query.filter(InventoryUnit.status.in_(UnitStatus.ON_HAND)).count()
    if on_hand != available + reserved + packed:
        raise IntegrityError("ON_HAND is not AVAILABLE+RESERVED+PACKED")


def _one_active_allocation_per_unit(client_id):
    sql = """
        SELECT inventory_unit_id, COUNT(*) FROM allocations
        WHERE status = 'ACTIVE'
    """
    params = {}
    if client_id:
        sql += " AND client_id = :client_id"
        params["client_id"] = client_id
    sql += " GROUP BY inventory_unit_id HAVING COUNT(*) > 1"
    rows = db.session.execute(text(sql), params).fetchall()
    if rows:
        raise IntegrityError(f"Units with multiple ACTIVE allocations: {rows}")


def _one_carton_content_per_unit(client_id):
    sql = """
        SELECT cc.inventory_unit_id, COUNT(*)
        FROM carton_contents cc
        JOIN cartons c ON c.id = cc.carton_id
        WHERE 1=1
    """
    params = {}
    if client_id:
        sql += " AND c.client_id = :client_id"
        params["client_id"] = client_id
    sql += " GROUP BY cc.inventory_unit_id HAVING COUNT(*) > 1"
    rows = db.session.execute(text(sql), params).fetchall()
    if rows:
        raise IntegrityError(f"Units in multiple cartons: {rows}")


def _order_masters_same_client(client_id):
    sql = """
        SELECT COUNT(*) FROM orders o
        JOIN divisions d ON d.id = o.division_id
        JOIN warehouses w ON w.id = o.warehouse_id
        WHERE d.client_id <> o.client_id OR w.client_id <> o.client_id
    """
    params = {}
    if client_id:
        sql += " AND o.client_id = :client_id"
        params["client_id"] = client_id
    count = db.session.execute(text(sql), params).scalar()
    if count:
        raise IntegrityError(f"{count} orders have division/warehouse on another client")


def _mappings_same_client():
    sql = """
        SELECT COUNT(*) FROM division_warehouses dw
        JOIN divisions d ON d.id = dw.division_id
        JOIN warehouses w ON w.id = dw.warehouse_id
        WHERE d.client_id <> w.client_id
           OR d.client_id <> dw.client_id
           OR w.client_id <> dw.client_id
    """
    count = db.session.execute(text(sql)).scalar()
    if count:
        raise IntegrityError(f"{count} division_warehouses rows are cross-client")
