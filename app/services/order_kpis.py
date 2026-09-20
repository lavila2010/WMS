"""PostgreSQL-backed KPI Orders aggregates.

Population is orders whose local operational *creation* date falls in the
inclusive From/To range. Closure later the same day moves the order from
Open to Processed; it does not create a second order.

Channel is an ORDER attribute from ``OrderType.channel`` (normalized from
OrderType.code). Inventory OrderType is never used.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from flask import current_app
from sqlalchemy import Date, case, cast, func, or_

from ..constants import Channel, OrderStatus
from ..extensions import db
from ..models import Client, Division, Order, OrderLine, OrderType


def operational_timezone() -> str:
    try:
        return current_app.config.get("OPERATIONAL_TIMEZONE") or "America/New_York"
    except RuntimeError:
        return "America/New_York"


def operational_today(tz_name: str | None = None) -> date:
    zone = ZoneInfo(tz_name or operational_timezone())
    return datetime.now(zone).date()


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
    """SQL expression: calendar date of ``orders.created_at`` in the ops TZ.

    Naive timestamps are treated as UTC, then converted to the operational
    timezone before the date is taken.
    """
    tz_name = tz_name or operational_timezone()
    as_utc = func.timezone("UTC", Order.created_at)
    local_ts = func.timezone(tz_name, as_utc)
    return cast(local_ts, Date)


def _channel_expr():
    mapped = case(
        (
            func.upper(OrderType.code).in_(
                [k for k, v in Channel.ALIASES.items() if v == Channel.ECOMMERCE]
            ),
            Channel.ECOMMERCE,
        ),
        (
            func.upper(OrderType.code).in_(
                [k for k, v in Channel.ALIASES.items() if v == Channel.RETAIL]
            ),
            Channel.RETAIL,
        ),
        (
            func.upper(OrderType.code).in_(
                [k for k, v in Channel.ALIASES.items() if v == Channel.WHOLESALE]
            ),
            Channel.WHOLESALE,
        ),
        else_=None,
    )
    return func.coalesce(OrderType.channel, mapped)


def _empty_channel():
    return {"orders": 0, "units": 0, "open_orders": 0}


def _empty_overall():
    return {
        "total_orders": 0,
        "open_orders": 0,
        "processed_orders": 0,
        "total_units": 0,
        "open_units": 0,
        "processed_units": 0,
    }


def _empty_row(client_name, division_name, client_id=None, division_id=None):
    return {
        "client_id": client_id,
        "division_id": division_id,
        "client": client_name,
        "division": division_name,
        "total_orders": 0,
        "open_orders": 0,
        "total_units": 0,
        "open_units": 0,
        "ecommerce_orders": 0,
        "ecommerce_units": 0,
        "retail_orders": 0,
        "retail_units": 0,
        "wholesale_orders": 0,
        "wholesale_units": 0,
    }


def _int(value):
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def get_order_kpis(
    from_date=None,
    to_date=None,
    client_id=None,
    division_id=None,
    search=None,
    tz_name: str | None = None,
) -> dict:
    tz_name = tz_name or operational_timezone()
    today = operational_today(tz_name)
    from_date = parse_iso_date(from_date, today)
    to_date = parse_iso_date(to_date, today)
    if to_date < from_date:
        from_date, to_date = to_date, from_date

    client_id = _int(client_id)
    division_id = _int(division_id)
    search = (search or "").strip()

    units_sub = (
        db.session.query(
            OrderLine.order_id.label("order_id"),
            func.coalesce(func.sum(OrderLine.quantity), 0).label("units"),
        )
        .group_by(OrderLine.order_id)
        .subquery()
    )
    channel = _channel_expr()
    local_date = created_local_date(tz_name)
    open_pred = Order.status != OrderStatus.CLOSED
    closed_pred = Order.status == OrderStatus.CLOSED

    query = (
        db.session.query(
            Client.id.label("client_id"),
            Client.name.label("client_name"),
            Division.id.label("division_id"),
            Division.name.label("division_name"),
            func.count(func.distinct(Order.id)).label("total_orders"),
            func.count(func.distinct(Order.id)).filter(open_pred).label("open_orders"),
            func.count(func.distinct(Order.id)).filter(closed_pred).label("processed_orders"),
            func.coalesce(func.sum(units_sub.c.units), 0).label("total_units"),
            func.coalesce(func.sum(units_sub.c.units).filter(open_pred), 0).label("open_units"),
            func.coalesce(func.sum(units_sub.c.units).filter(closed_pred), 0).label("processed_units"),
            func.count(func.distinct(Order.id)).filter(channel == Channel.ECOMMERCE).label("ecommerce_orders"),
            func.coalesce(func.sum(units_sub.c.units).filter(channel == Channel.ECOMMERCE), 0).label("ecommerce_units"),
            func.count(func.distinct(Order.id)).filter(channel == Channel.RETAIL).label("retail_orders"),
            func.coalesce(func.sum(units_sub.c.units).filter(channel == Channel.RETAIL), 0).label("retail_units"),
            func.count(func.distinct(Order.id)).filter(channel == Channel.WHOLESALE).label("wholesale_orders"),
            func.coalesce(func.sum(units_sub.c.units).filter(channel == Channel.WHOLESALE), 0).label("wholesale_units"),
            func.count(func.distinct(Order.id)).filter(
                (channel == Channel.ECOMMERCE) & open_pred
            ).label("ecommerce_open"),
            func.count(func.distinct(Order.id)).filter(
                (channel == Channel.RETAIL) & open_pred
            ).label("retail_open"),
            func.count(func.distinct(Order.id)).filter(
                (channel == Channel.WHOLESALE) & open_pred
            ).label("wholesale_open"),
        )
        .select_from(Order)
        .join(Client, Client.id == Order.client_id)
        .join(Division, Division.id == Order.division_id)
        .join(OrderType, OrderType.id == Order.order_type_id)
        .outerjoin(units_sub, units_sub.c.order_id == Order.id)
        .filter(local_date >= from_date, local_date <= to_date)
    )
    if client_id:
        query = query.filter(Order.client_id == client_id)
    if division_id:
        query = query.filter(Order.division_id == division_id)
    if search:
        like = f"%{search}%"
        query = query.filter(or_(Client.name.ilike(like), Division.name.ilike(like)))

    query = query.group_by(Client.id, Client.name, Division.id, Division.name).order_by(
        Client.name.asc(), Division.name.asc()
    )
    fetched = query.all()

    rows = []
    overall = _empty_overall()
    channels = {
        "ecommerce": _empty_channel(),
        "retail": _empty_channel(),
        "wholesale": _empty_channel(),
    }
    for row in fetched:
        item = {
            "client_id": row.client_id,
            "division_id": row.division_id,
            "client": row.client_name,
            "division": row.division_name,
            "total_orders": int(row.total_orders or 0),
            "open_orders": int(row.open_orders or 0),
            "total_units": int(row.total_units or 0),
            "open_units": int(row.open_units or 0),
            "ecommerce_orders": int(row.ecommerce_orders or 0),
            "ecommerce_units": int(row.ecommerce_units or 0),
            "retail_orders": int(row.retail_orders or 0),
            "retail_units": int(row.retail_units or 0),
            "wholesale_orders": int(row.wholesale_orders or 0),
            "wholesale_units": int(row.wholesale_units or 0),
        }
        rows.append(item)
        overall["total_orders"] += item["total_orders"]
        overall["open_orders"] += item["open_orders"]
        overall["processed_orders"] += int(row.processed_orders or 0)
        overall["total_units"] += item["total_units"]
        overall["open_units"] += item["open_units"]
        overall["processed_units"] += int(row.processed_units or 0)
        channels["ecommerce"]["orders"] += item["ecommerce_orders"]
        channels["ecommerce"]["units"] += item["ecommerce_units"]
        channels["ecommerce"]["open_orders"] += int(row.ecommerce_open or 0)
        channels["retail"]["orders"] += item["retail_orders"]
        channels["retail"]["units"] += item["retail_units"]
        channels["retail"]["open_orders"] += int(row.retail_open or 0)
        channels["wholesale"]["orders"] += item["wholesale_orders"]
        channels["wholesale"]["units"] += item["wholesale_units"]
        channels["wholesale"]["open_orders"] += int(row.wholesale_open or 0)

    totals = {
        "client": "TOTAL",
        "division": "",
        "total_orders": overall["total_orders"],
        "open_orders": overall["open_orders"],
        "total_units": overall["total_units"],
        "open_units": overall["open_units"],
        "ecommerce_orders": channels["ecommerce"]["orders"],
        "ecommerce_units": channels["ecommerce"]["units"],
        "retail_orders": channels["retail"]["orders"],
        "retail_units": channels["retail"]["units"],
        "wholesale_orders": channels["wholesale"]["orders"],
        "wholesale_units": channels["wholesale"]["units"],
    }
    return {
        "from_date": from_date,
        "to_date": to_date,
        "timezone": tz_name,
        "overall": overall,
        "rows": rows,
        "totals": totals,
        "channels": channels,
    }


def kpi_filter_options(client_id=None):
    clients = Client.query.order_by(Client.name.asc(), Client.code.asc()).all()
    q = Division.query
    if client_id:
        q = q.filter(Division.client_id == int(client_id))
    divisions = q.order_by(Division.name.asc(), Division.code.asc()).all()
    return {"clients": clients, "divisions": divisions}
