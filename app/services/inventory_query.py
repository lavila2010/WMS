"""Derived inventory quantities. Units are the system of record."""

from __future__ import annotations

from sqlalchemy import String, case, cast, func, or_

from ..constants import UnitStatus
from ..extensions import db
from ..models import InventoryTransaction, InventoryUnit


def _base(client_id=None, warehouse_id=None, client_ids=None):
    query = InventoryUnit.query
    if client_id:
        query = query.filter(InventoryUnit.client_id == client_id)
    elif client_ids is not None:
        query = query.filter(InventoryUnit.client_id.in_(client_ids or [-1]))
    if warehouse_id:
        query = query.filter(InventoryUnit.warehouse_id == warehouse_id)
    return query


def status_counts(client_id=None, warehouse_id=None, client_ids=None, upc=None, location=None):
    query = _base(client_id, warehouse_id, client_ids)
    if upc:
        query = query.filter(InventoryUnit.upc == upc)
    if location:
        query = query.filter(InventoryUnit.location == location)
    available = query.filter(InventoryUnit.status == UnitStatus.AVAILABLE).count()
    reserved = query.filter(InventoryUnit.status == UnitStatus.RESERVED).count()
    packed = query.filter(InventoryUnit.status == UnitStatus.PACKED).count()
    shipped = query.filter(InventoryUnit.status == UnitStatus.SHIPPED).count()
    return {
        "available": available,
        "reserved": reserved,
        "packed": packed,
        "shipped": shipped,
        "on_hand": available + reserved + packed,
        "total": available + reserved + packed + shipped,
        "unique_upcs": query.with_entities(InventoryUnit.upc).distinct().count(),
        "unique_skus": query.filter(InventoryUnit.sku.isnot(None))
        .with_entities(InventoryUnit.sku)
        .distinct()
        .count(),
        "locations": query.with_entities(InventoryUnit.location).distinct().count(),
    }


def aggregate_rows(client_id=None, warehouse_id=None, client_ids=None):
    filters = []
    if client_id:
        filters.append(InventoryUnit.client_id == client_id)
    elif client_ids is not None:
        filters.append(InventoryUnit.client_id.in_(client_ids or [-1]))
    if warehouse_id:
        filters.append(InventoryUnit.warehouse_id == warehouse_id)

    available = func.sum(
        case((InventoryUnit.status == UnitStatus.AVAILABLE, 1), else_=0)
    )
    reserved = func.sum(case((InventoryUnit.status == UnitStatus.RESERVED, 1), else_=0))
    packed = func.sum(case((InventoryUnit.status == UnitStatus.PACKED, 1), else_=0))
    shipped = func.sum(case((InventoryUnit.status == UnitStatus.SHIPPED, 1), else_=0))

    rows = (
        db.session.query(
            InventoryUnit.client_id,
            InventoryUnit.warehouse_id,
            InventoryUnit.upc,
            InventoryUnit.location,
            func.max(InventoryUnit.sku).label("sku"),
            func.max(InventoryUnit.style).label("style"),
            func.max(InventoryUnit.color).label("color"),
            func.max(InventoryUnit.size).label("size"),
            available.label("available"),
            reserved.label("reserved"),
            packed.label("packed"),
            shipped.label("shipped"),
        )
        .filter(*filters)
        .group_by(
            InventoryUnit.client_id,
            InventoryUnit.warehouse_id,
            InventoryUnit.upc,
            InventoryUnit.location,
        )
        .order_by(
            InventoryUnit.client_id,
            InventoryUnit.warehouse_id,
            InventoryUnit.upc,
            InventoryUnit.location,
        )
        .all()
    )
    result = []
    for row in rows:
        available_n = int(row.available or 0)
        reserved_n = int(row.reserved or 0)
        packed_n = int(row.packed or 0)
        shipped_n = int(row.shipped or 0)
        result.append(
            {
                "client_id": row.client_id,
                "warehouse_id": row.warehouse_id,
                "upc": row.upc,
                "location": row.location,
                "sku": row.sku,
                "style": row.style,
                "color": row.color,
                "size": row.size,
                "available": available_n,
                "reserved": reserved_n,
                "packed": packed_n,
                "shipped": shipped_n,
                "on_hand": available_n + reserved_n + packed_n,
            }
        )
    return result


def search_units(q, *, status=None, client_id=None, warehouse_id=None, client_ids=None, limit=200):
    query = _base(client_id, warehouse_id, client_ids)
    term = (q or "").strip()
    if term:
        like = f"%{term}%"
        query = query.filter(
            or_(
                InventoryUnit.upc.ilike(like),
                InventoryUnit.sku.ilike(like),
                InventoryUnit.style.ilike(like),
                InventoryUnit.color.ilike(like),
                InventoryUnit.size.ilike(like),
                InventoryUnit.location.ilike(like),
                cast(InventoryUnit.id, String).ilike(like),
            )
        )
    if status:
        query = query.filter(InventoryUnit.status == status)
    return query.order_by(InventoryUnit.location.asc(), InventoryUnit.id.asc()).limit(limit).all()


def ledger_rows(
    *,
    client_id=None,
    warehouse_id=None,
    client_ids=None,
    upc=None,
    transaction_type=None,
    date_from=None,
    date_to=None,
    limit=500,
):
    query = InventoryTransaction.query
    if client_id:
        query = query.filter(InventoryTransaction.client_id == client_id)
    elif client_ids is not None:
        query = query.filter(InventoryTransaction.client_id.in_(client_ids or [-1]))
    if warehouse_id:
        query = query.filter(InventoryTransaction.warehouse_id == warehouse_id)
    if upc:
        query = query.filter(InventoryTransaction.upc == upc.strip())
    if transaction_type:
        query = query.filter(InventoryTransaction.transaction_type == transaction_type.strip().upper())
    if date_from:
        query = query.filter(InventoryTransaction.created_at >= date_from)
    if date_to:
        query = query.filter(InventoryTransaction.created_at < date_to)
    return (
        query.order_by(InventoryTransaction.created_at.desc(), InventoryTransaction.id.desc())
        .limit(limit)
        .all()
    )


def warehouse_comparison(client_id, client_ids=None):
    filters = []
    if client_id:
        filters.append(InventoryUnit.client_id == client_id)
    elif client_ids is not None:
        filters.append(InventoryUnit.client_id.in_(client_ids or [-1]))
    rows = (
        db.session.query(
            InventoryUnit.warehouse_id,
            func.sum(case((InventoryUnit.status == UnitStatus.AVAILABLE, 1), else_=0)).label("available"),
            func.sum(case((InventoryUnit.status == UnitStatus.RESERVED, 1), else_=0)).label("reserved"),
            func.sum(case((InventoryUnit.status == UnitStatus.PACKED, 1), else_=0)).label("packed"),
        )
        .filter(*filters)
        .group_by(InventoryUnit.warehouse_id)
        .all()
    )
    return [
        {
            "warehouse_id": row.warehouse_id,
            "available": int(row.available or 0),
            "reserved": int(row.reserved or 0),
            "packed": int(row.packed or 0),
            "on_hand": int(row.available or 0) + int(row.reserved or 0) + int(row.packed or 0),
        }
        for row in rows
    ]
