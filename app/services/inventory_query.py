"""Derived inventory quantities. Units are the system of record."""

from __future__ import annotations

from sqlalchemy import String, case, cast, func, or_

from ..constants import UnitStatus
from ..extensions import db
from ..models import ImportBatch, InventoryTransaction, InventoryUnit
from .inventory_visibility import apply_operational_visibility, operational_batch_clause


def show_zero_enabled(raw) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "on", "yes"}


def _on_hand_sum():
    return (
        func.sum(case((InventoryUnit.status == UnitStatus.AVAILABLE, 1), else_=0))
        + func.sum(case((InventoryUnit.status == UnitStatus.RESERVED, 1), else_=0))
        + func.sum(case((InventoryUnit.status == UnitStatus.PACKED, 1), else_=0))
    )


def _base(client_id=None, warehouse_id=None, client_ids=None, *, operational=True):
    query = InventoryUnit.query
    if operational:
        query = apply_operational_visibility(query)
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
    on_hand_q = query.filter(InventoryUnit.status.in_(UnitStatus.ON_HAND))
    return {
        "available": available,
        "reserved": reserved,
        "packed": packed,
        "shipped": shipped,
        "on_hand": available + reserved + packed,
        "total": available + reserved + packed + shipped,
        "unique_upcs": on_hand_q.with_entities(InventoryUnit.upc).distinct().count(),
        "unique_skus": on_hand_q.filter(InventoryUnit.sku.isnot(None), InventoryUnit.sku != "")
        .with_entities(InventoryUnit.sku)
        .distinct()
        .count(),
        "locations": on_hand_q.with_entities(InventoryUnit.location).distinct().count(),
    }


def aggregate_rows(client_id=None, warehouse_id=None, client_ids=None, *, include_zero=False):
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
    on_hand_expr = _on_hand_sum()

    query = (
        db.session.query(
            InventoryUnit.client_id,
            InventoryUnit.warehouse_id,
            InventoryUnit.upc,
            InventoryUnit.location,
            func.max(InventoryUnit.sku).label("sku"),
            func.max(InventoryUnit.description).label("description"),
            func.max(InventoryUnit.style).label("style"),
            func.max(InventoryUnit.color).label("color"),
            func.max(InventoryUnit.size).label("size"),
            available.label("available"),
            reserved.label("reserved"),
            packed.label("packed"),
            shipped.label("shipped"),
        )
        .outerjoin(ImportBatch, InventoryUnit.import_batch_id == ImportBatch.id)
        .filter(operational_batch_clause(), *filters)
        .group_by(
            InventoryUnit.client_id,
            InventoryUnit.warehouse_id,
            InventoryUnit.upc,
            InventoryUnit.location,
        )
    )
    if not include_zero:
        query = query.having(on_hand_expr > 0)
    rows = query.order_by(
        InventoryUnit.client_id,
        InventoryUnit.warehouse_id,
        InventoryUnit.upc,
        InventoryUnit.location,
    ).all()
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
                "description": row.description,
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


def search_units(
    q,
    *,
    status=None,
    client_id=None,
    warehouse_id=None,
    client_ids=None,
    include_zero=False,
    limit=200,
):
    query = _base(client_id, warehouse_id, client_ids)
    term = (q or "").strip()
    if term:
        like = f"%{term}%"
        query = query.filter(
            or_(
                InventoryUnit.upc.ilike(like),
                InventoryUnit.sku.ilike(like),
                InventoryUnit.description.ilike(like),
                InventoryUnit.style.ilike(like),
                InventoryUnit.color.ilike(like),
                InventoryUnit.size.ilike(like),
                InventoryUnit.location.ilike(like),
                cast(InventoryUnit.id, String).ilike(like),
            )
        )
    if status == UnitStatus.SHIPPED and not include_zero:
        return []
    if status:
        query = query.filter(InventoryUnit.status == status)
    elif not include_zero:
        query = query.filter(InventoryUnit.status.in_(UnitStatus.ON_HAND))
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


def bulk_client_upc_descriptions(client_id: int, upcs) -> dict[str, str]:
    """Return Client+UPC descriptions that resolve to exactly one value.

    Issues one query per 1,000 UPCs. Never reads another client's units.
    """
    wanted = [str(u).strip() for u in (upcs or []) if str(u or "").strip()]
    if not client_id or not wanted:
        return {}
    found: dict[str, set[str]] = {}
    for offset in range(0, len(wanted), 1000):
        chunk = wanted[offset : offset + 1000]
        rows = (
            db.session.query(InventoryUnit.upc, InventoryUnit.description)
            .outerjoin(ImportBatch, InventoryUnit.import_batch_id == ImportBatch.id)
            .filter(
                InventoryUnit.client_id == client_id,
                InventoryUnit.upc.in_(chunk),
                InventoryUnit.description.isnot(None),
                InventoryUnit.description != "",
                operational_batch_clause(),
            )
            .distinct()
            .all()
        )
        for upc, description in rows:
            found.setdefault(upc, set()).add(description)
    return {upc: next(iter(values)) for upc, values in found.items() if len(values) == 1}


def unique_client_upc_description(client_id: int, upc: str) -> str | None:
    """Return the Description for Client+UPC when it resolves to one value.

    Never reads another client's units. Returns None if the UPC is unknown
    for the client or if multiple distinct descriptions exist.
    """
    upc = (upc or "").strip()
    if not client_id or not upc:
        return None
    values = (
        db.session.query(InventoryUnit.description)
        .outerjoin(ImportBatch, InventoryUnit.import_batch_id == ImportBatch.id)
        .filter(
            InventoryUnit.client_id == client_id,
            InventoryUnit.upc == upc,
            InventoryUnit.description.isnot(None),
            InventoryUnit.description != "",
            operational_batch_clause(),
        )
        .distinct()
        .all()
    )
    if len(values) != 1:
        return None
    return values[0][0]


def warehouse_comparison(client_id, client_ids=None, *, include_zero=False):
    filters = []
    if client_id:
        filters.append(InventoryUnit.client_id == client_id)
    elif client_ids is not None:
        filters.append(InventoryUnit.client_id.in_(client_ids or [-1]))
    on_hand_expr = _on_hand_sum()
    query = (
        db.session.query(
            InventoryUnit.warehouse_id,
            func.sum(case((InventoryUnit.status == UnitStatus.AVAILABLE, 1), else_=0)).label("available"),
            func.sum(case((InventoryUnit.status == UnitStatus.RESERVED, 1), else_=0)).label("reserved"),
            func.sum(case((InventoryUnit.status == UnitStatus.PACKED, 1), else_=0)).label("packed"),
        )
        .outerjoin(ImportBatch, InventoryUnit.import_batch_id == ImportBatch.id)
        .filter(operational_batch_clause(), *filters)
        .group_by(InventoryUnit.warehouse_id)
    )
    if not include_zero:
        query = query.having(on_hand_expr > 0)
    rows = query.all()
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
