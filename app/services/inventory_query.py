"""Aggregation queries for the Inventory Control module.

All queries are scoped by Client + Warehouse only (never Order Type).
On-hand statuses are AVAILABLE, ALLOCATED (reserved), and PACKED.
"""

from __future__ import annotations

from sqlalchemy import case, distinct, func

from ..constants import UnitStatus
from ..extensions import db
from ..models import (
    Client,
    InventoryException,
    InventoryUnit,
    Order,
    Warehouse,
)

ON_HAND = [UnitStatus.AVAILABLE, UnitStatus.ALLOCATED, UnitStatus.PACKED]

_AVAIL = func.coalesce(func.sum(case((InventoryUnit.status == UnitStatus.AVAILABLE, 1), else_=0)), 0)
_RESV = func.coalesce(func.sum(case((InventoryUnit.status == UnitStatus.ALLOCATED, 1), else_=0)), 0)
_PACK = func.coalesce(func.sum(case((InventoryUnit.status == UnitStatus.PACKED, 1), else_=0)), 0)
_TOTAL = func.coalesce(func.sum(case((InventoryUnit.status.in_(ON_HAND), 1), else_=0)), 0)


def scoped_units(client_id=None, warehouse_id=None):
    q = InventoryUnit.query
    if client_id:
        q = q.filter(InventoryUnit.client_id == client_id)
    if warehouse_id:
        q = q.filter(InventoryUnit.warehouse_id == warehouse_id)
    return q


def _base(client_id=None, warehouse_id=None):
    q = db.session.query(InventoryUnit)
    if client_id:
        q = q.filter(InventoryUnit.client_id == client_id)
    if warehouse_id:
        q = q.filter(InventoryUnit.warehouse_id == warehouse_id)
    return q


def kpis(client_id=None, warehouse_id=None) -> dict:
    base = _base(client_id, warehouse_id)
    onhand = base.filter(InventoryUnit.status.in_(ON_HAND))
    exc = InventoryException.query.filter(InventoryException.status == "OPEN")
    if client_id:
        exc = exc.filter(InventoryException.client_id == client_id)
    if warehouse_id:
        exc = exc.filter(InventoryException.warehouse_id == warehouse_id)
    return {
        "total": onhand.count(),
        "available": base.filter(InventoryUnit.status == UnitStatus.AVAILABLE).count(),
        "reserved": base.filter(InventoryUnit.status == UnitStatus.ALLOCATED).count(),
        "packed": base.filter(InventoryUnit.status == UnitStatus.PACKED).count(),
        "unique_upcs": onhand.with_entities(func.count(distinct(InventoryUnit.upc))).scalar() or 0,
        "unique_skus": onhand.with_entities(func.count(distinct(InventoryUnit.sku))).scalar() or 0,
        "locations": onhand.with_entities(func.count(distinct(InventoryUnit.location))).scalar() or 0,
        "open_exceptions": exc.count(),
    }


def inventory_by_upc(client_id=None, warehouse_id=None) -> list[dict]:
    q = db.session.query(
        InventoryUnit.upc.label("upc"),
        func.min(InventoryUnit.sku).label("sku"),
        func.min(InventoryUnit.description).label("description"),
        _AVAIL.label("available"),
        _RESV.label("reserved"),
        _PACK.label("packed"),
        _TOTAL.label("total"),
        func.count(distinct(InventoryUnit.location)).label("locations"),
    ).filter(InventoryUnit.status.in_(ON_HAND))
    if client_id:
        q = q.filter(InventoryUnit.client_id == client_id)
    if warehouse_id:
        q = q.filter(InventoryUnit.warehouse_id == warehouse_id)
    q = q.group_by(InventoryUnit.upc).order_by(_TOTAL.desc())
    return [dict(r._mapping) for r in q.all()]


def warehouse_comparison(client_id) -> list[dict]:
    q = (
        db.session.query(
            Warehouse.code.label("warehouse"),
            _AVAIL.label("available"),
            _RESV.label("reserved"),
            _PACK.label("packed"),
            _TOTAL.label("total"),
        )
        .join(Warehouse, Warehouse.id == InventoryUnit.warehouse_id)
        .filter(InventoryUnit.client_id == client_id, InventoryUnit.status.in_(ON_HAND))
        .group_by(Warehouse.code)
        .order_by(Warehouse.code)
    )
    return [dict(r._mapping) for r in q.all()]


def upc_summary(client_id, warehouse_id, upc) -> dict | None:
    base = _base(client_id, warehouse_id).filter(
        InventoryUnit.upc == upc, InventoryUnit.status.in_(ON_HAND)
    )
    header = (
        base.with_entities(
            func.min(InventoryUnit.sku),
            func.min(InventoryUnit.description),
            _AVAIL, _RESV, _PACK, _TOTAL,
            func.count(distinct(InventoryUnit.location)),
        ).first()
    )
    if not header or (header[5] or 0) == 0:
        return None
    locations = (
        base.with_entities(
            InventoryUnit.location.label("location"),
            _AVAIL.label("available"),
            _RESV.label("reserved"),
            _PACK.label("packed"),
            _TOTAL.label("total"),
        )
        .group_by(InventoryUnit.location)
        .order_by(InventoryUnit.location)
        .all()
    )
    return {
        "upc": upc,
        "sku": header[0],
        "description": header[1],
        "available": header[2],
        "reserved": header[3],
        "packed": header[4],
        "total": header[5],
        "num_locations": header[6],
        "locations": [dict(r._mapping) for r in locations],
    }


def location_units(client_id, warehouse_id, upc, location):
    return (
        scoped_units(client_id, warehouse_id)
        .filter(InventoryUnit.upc == upc, InventoryUnit.location == location)
        .order_by(InventoryUnit.barcode)
        .all()
    )
