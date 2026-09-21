"""KPI Orders: Client + Division grouping, no join duplication."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from flask import current_app
from sqlalchemy import Date, case, cast, func

from ..constants import OperationType, OrderStatus
from ..extensions import db
from ..models import Client, Division, ImportBatch, Order, OrderLine
from .order_visibility import operational_order_batch_clause


def operational_timezone() -> str:
    try:
        return current_app.config.get("OPERATIONAL_TIMEZONE") or "America/New_York"
    except RuntimeError:
        return "America/New_York"


def operational_today(tz_name: str | None = None) -> date:
    return datetime.now(ZoneInfo(tz_name or operational_timezone())).date()


def parse_iso_date(value, default: date) -> date:
    raw = (value or "").strip() if isinstance(value, str) else value
    if not raw:
        return default
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    try:
        return datetime.strptime(str(raw), "%Y-%m-%d").date()
    except ValueError:
        return default


def created_local_date(tz_name: str | None = None):
    tz_name = tz_name or operational_timezone()
    as_utc = func.timezone("UTC", Order.created_at)
    local_ts = func.timezone(tz_name, as_utc)
    return cast(local_ts, Date)


def _units_subquery():
    return (
        db.session.query(
            OrderLine.order_id.label("order_id"),
            func.coalesce(func.sum(OrderLine.qty_ordered), 0).label("units"),
        )
        .group_by(OrderLine.order_id)
        .subquery()
    )


def summarize(client_ids=None, client_id=None, division_id=None, from_date=None, to_date=None, q=""):
    today = operational_today()
    from_date = parse_iso_date(from_date, today)
    to_date = parse_iso_date(to_date, today)
    units = _units_subquery()
    local_day = created_local_date()
    filters = [local_day >= from_date, local_day <= to_date]
    if client_id:
        filters.append(Order.client_id == client_id)
    elif client_ids is not None:
        filters.append(Order.client_id.in_(client_ids or [-1]))
    if division_id:
        filters.append(Order.division_id == division_id)
    if q:
        like = f"%{q}%"
        filters.append(db.or_(Client.name.ilike(like), Client.client_code.ilike(like), Division.name.ilike(like), Division.code.ilike(like)))

    open_flag = case((Order.status.notin_([OrderStatus.CLOSED, OrderStatus.CANCELLED]), 1), else_=0)
    closed_flag = case((Order.status == OrderStatus.CLOSED, 1), else_=0)
    ecom = case((Division.operation_type == OperationType.ECOM, 1), else_=0)
    rtl = case((Division.operation_type == OperationType.RTL, 1), else_=0)
    whls = case((Division.operation_type == OperationType.WHLS, 1), else_=0)
    unit_expr = func.coalesce(units.c.units, 0)

    rows_q = (
        db.session.query(
            Client.client_code.label("client"),
            Division.code.label("division"),
            Division.operation_type.label("operation_type"),
            func.count(Order.id).label("total_orders"),
            func.sum(open_flag).label("open_orders"),
            func.sum(closed_flag).label("processed_orders"),
            func.coalesce(func.sum(unit_expr), 0).label("total_units"),
            func.coalesce(func.sum(case((open_flag == 1, unit_expr), else_=0)), 0).label("open_units"),
            func.coalesce(func.sum(case((closed_flag == 1, unit_expr), else_=0)), 0).label("processed_units"),
            func.coalesce(func.sum(ecom), 0).label("ecommerce_orders"),
            func.coalesce(func.sum(case((ecom == 1, unit_expr), else_=0)), 0).label("ecommerce_units"),
            func.coalesce(func.sum(rtl), 0).label("retail_orders"),
            func.coalesce(func.sum(case((rtl == 1, unit_expr), else_=0)), 0).label("retail_units"),
            func.coalesce(func.sum(whls), 0).label("wholesale_orders"),
            func.coalesce(func.sum(case((whls == 1, unit_expr), else_=0)), 0).label("wholesale_units"),
        )
        .join(Client, Client.id == Order.client_id)
        .join(Division, Division.id == Order.division_id)
        .outerjoin(units, units.c.order_id == Order.id)
        .outerjoin(ImportBatch, Order.import_batch_id == ImportBatch.id)
        .filter(operational_order_batch_clause(), *filters)
        .group_by(Client.client_code, Division.code, Division.operation_type)
        .order_by(Client.client_code, Division.code)
    )
    rows = []
    totals = {
        "total_orders": 0,
        "open_orders": 0,
        "processed_orders": 0,
        "total_units": 0,
        "open_units": 0,
        "processed_units": 0,
        "ecommerce_orders": 0,
        "ecommerce_units": 0,
        "retail_orders": 0,
        "retail_units": 0,
        "wholesale_orders": 0,
        "wholesale_units": 0,
    }
    for row in rows_q:
        item = {k: int(getattr(row, k) or 0) if k not in {"client", "division", "operation_type"} else getattr(row, k) for k in row._mapping}
        rows.append(item)
        for key in totals:
            totals[key] += int(item.get(key) or 0)
    overall = {
        "total_orders": totals["total_orders"],
        "open_orders": totals["open_orders"],
        "processed_orders": totals["processed_orders"],
        "total_units": totals["total_units"],
        "open_units": totals["open_units"],
        "processed_units": totals["processed_units"],
    }
    channels = {
        "ecommerce": {
            "orders": totals["ecommerce_orders"],
            "units": totals["ecommerce_units"],
            "open_orders": sum(r["open_orders"] for r in rows if r["operation_type"] == OperationType.ECOM),
        },
        "retail": {
            "orders": totals["retail_orders"],
            "units": totals["retail_units"],
            "open_orders": sum(r["open_orders"] for r in rows if r["operation_type"] == OperationType.RTL),
        },
        "wholesale": {
            "orders": totals["wholesale_orders"],
            "units": totals["wholesale_units"],
            "open_orders": sum(r["open_orders"] for r in rows if r["operation_type"] == OperationType.WHLS),
        },
    }
    return {
        "from_date": from_date.isoformat(),
        "to_date": to_date.isoformat(),
        "rows": rows,
        "totals": totals,
        "overall": overall,
        "channels": channels,
    }
