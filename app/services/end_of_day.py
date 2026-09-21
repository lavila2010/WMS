"""End of Day closed-order reporting in America/New_York operational time."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from io import BytesIO
from zoneinfo import ZoneInfo

import pandas as pd

from ..auth import record_audit
from ..constants import EOD_TIMEZONE, OrderStatus, ShippingStatus
from ..extensions import db
from ..models import Carton, CartonContent, Client, Division, Order, PickTicket, User
from .order_visibility import apply_operational_order_visibility
from .shipping import derived_shipping_status, sync_shipping_status


TZ = ZoneInfo(EOD_TIMEZONE)


def operational_today() -> date:
    return datetime.now(TZ).date()


def ny_day_utc_bounds(day: date) -> tuple[datetime, datetime]:
    start_local = datetime.combine(day, time.min, tzinfo=TZ)
    end_local = start_local + timedelta(days=1)
    start_utc = start_local.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = end_local.astimezone(timezone.utc).replace(tzinfo=None)
    return start_utc, end_utc


def parse_eod_date(value: str | None) -> date:
    if not value:
        return operational_today()
    return datetime.strptime(value, "%Y-%m-%d").date()


def to_ny(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ)


def closed_orders_query(
    *,
    day: date,
    client_id=None,
    division_id=None,
    warehouse_id=None,
    carrier="",
    closed_by="",
    shipping_status="",
    accessible_client_ids=None,
    admin=False,
):
    start, end = ny_day_utc_bounds(day)
    query = apply_operational_order_visibility(
        Order.query.filter(Order.status == OrderStatus.CLOSED, Order.closed_at >= start, Order.closed_at < end)
    )
    if client_id:
        query = query.filter(Order.client_id == client_id)
    elif not admin:
        query = query.filter(Order.client_id.in_(accessible_client_ids or [-1]))
    if division_id:
        query = query.filter(Order.division_id == division_id)
    if warehouse_id:
        query = query.filter(Order.warehouse_id == warehouse_id)
    if carrier:
        query = query.filter(Order.carrier.ilike(f"%{carrier.strip()}%"))
    if closed_by:
        query = query.filter(Order.closed_by_username.ilike(f"%{closed_by.strip()}%"))
    if shipping_status in (ShippingStatus.PENDING_TRACKING, ShippingStatus.TRACKING_COMPLETE, ShippingStatus.NOT_READY):
        query = query.filter(Order.shipping_status == shipping_status)
    return query.join(Client, Client.id == Order.client_id).join(Division, Division.id == Order.division_id).order_by(
        Client.client_code.asc(),
        Division.code.asc(),
        Order.closed_at.asc(),
        Order.id.asc(),
    )


def _order_metrics(order: Order) -> dict:
    cartons = Carton.query.filter_by(order_id=order.id).order_by(Carton.id).all()
    ticket = PickTicket.query.filter_by(order_id=order.id).first()
    carton_units = sum(CartonContent.query.filter_by(carton_id=c.id).count() for c in cartons)
    total_weight = sum((c.weight or 0) for c in cartons)
    ordered = sum(line.qty_ordered for line in order.lines)
    allocated = sum(line.qty_allocated for line in order.lines)
    packed = sum(line.qty_packed for line in order.lines)
    shipped = sum(line.qty_shipped for line in order.lines)
    status = sync_shipping_status(order, cartons)
    return {
        "order": order,
        "ticket": ticket.pick_ticket_number if ticket else "—",
        "cartons": cartons,
        "carton_count": len(cartons),
        "carton_units": carton_units,
        "total_weight": total_weight,
        "ordered": ordered,
        "allocated": allocated,
        "packed": packed,
        "shipped": shipped,
        "shipping_status": status,
        "closed_local": to_ny(order.closed_at),
        "reconciled": ordered == allocated == packed == shipped == carton_units,
    }


def build_eod_rows(orders: list[Order]) -> list[dict]:
    return [_order_metrics(order) for order in orders]


def eod_kpis(rows: list[dict]) -> dict:
    return {
        "total_orders": len(rows),
        "total_units": sum(r["shipped"] for r in rows),
        "total_cartons": sum(r["carton_count"] for r in rows),
        "total_weight": sum(r["total_weight"] for r in rows),
        "tracking_complete": sum(1 for r in rows if r["shipping_status"] == ShippingStatus.TRACKING_COMPLETE),
        "pending_tracking": sum(1 for r in rows if r["shipping_status"] == ShippingStatus.PENDING_TRACKING),
    }


def _entered_by(carton: Carton) -> str:
    if not carton.tracking_entered_by_user_id:
        return "—"
    user = db.session.get(User, carton.tracking_entered_by_user_id)
    return user.username if user else "—"


def export_eod_excel(rows: list[dict], *, client_id=None) -> bytes:
    order_sheet = []
    carton_sheet = []
    for row in rows:
        order = row["order"]
        local = row["closed_local"]
        order_sheet.append(
            {
                "Closed Date": local.strftime("%Y-%m-%d") if local else "",
                "Closed Time": local.strftime("%H:%M:%S") if local else "",
                "Client": order.client.client_code,
                "Division": order.division.code,
                "Warehouse": order.warehouse.warehouse_code,
                "WMS Order ID": order.wms_order_id,
                "Client Order Number": order.client_order_number,
                "Pick Ticket": row["ticket"],
                "Customer": order.customer or "",
                "Customer Address": order.customer_address or "",
                "Customer Phone": order.customer_phone or "",
                "Carrier": order.carrier or "",
                "Shipping Service": order.shipping_service or "",
                "Ordered Units": row["ordered"],
                "Allocated Units": row["allocated"],
                "Packed Units": row["packed"],
                "Shipped Units": row["shipped"],
                "Carton Count": row["carton_count"],
                "Total Weight": row["total_weight"],
                "Shipping Status": row["shipping_status"],
                "Closed By": order.closed_by_username or "",
            }
        )
        for carton in row["cartons"]:
            confirmed = carton.tracking_validated_at
            confirmed_local = to_ny(confirmed)
            carton_sheet.append(
                {
                    "Client": order.client.client_code,
                    "Division": order.division.code,
                    "Warehouse": order.warehouse.warehouse_code,
                    "WMS Order ID": order.wms_order_id,
                    "Client Order Number": order.client_order_number,
                    "Pick Ticket": row["ticket"],
                    "Carton Number": carton.carton_number,
                    "Length": carton.length,
                    "Width": carton.width,
                    "Height": carton.height,
                    "Dimension Unit": carton.dimension_unit,
                    "Weight": carton.weight,
                    "Weight Unit": carton.weight_unit,
                    "Units": CartonContent.query.filter_by(carton_id=carton.id).count(),
                    "Carrier": carton.tracking_carrier or order.carrier or "",
                    "Tracking Number": carton.tracking_number or "",
                    "Tracking Status": carton.shipping_label_status,
                    "Tracking Entered By": _entered_by(carton),
                    "Tracking Confirmed At": confirmed_local.strftime("%Y-%m-%d %H:%M:%S") if confirmed_local else "",
                }
            )
    order_columns = [
        "Closed Date",
        "Closed Time",
        "Client",
        "Division",
        "Warehouse",
        "WMS Order ID",
        "Client Order Number",
        "Pick Ticket",
        "Customer",
        "Customer Address",
        "Customer Phone",
        "Carrier",
        "Shipping Service",
        "Ordered Units",
        "Allocated Units",
        "Packed Units",
        "Shipped Units",
        "Carton Count",
        "Total Weight",
        "Shipping Status",
        "Closed By",
    ]
    carton_columns = [
        "Client",
        "Division",
        "Warehouse",
        "WMS Order ID",
        "Client Order Number",
        "Pick Ticket",
        "Carton Number",
        "Length",
        "Width",
        "Height",
        "Dimension Unit",
        "Weight",
        "Weight Unit",
        "Units",
        "Carrier",
        "Tracking Number",
        "Tracking Status",
        "Tracking Entered By",
        "Tracking Confirmed At",
    ]
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        pd.DataFrame(order_sheet, columns=order_columns).to_excel(writer, index=False, sheet_name="Orders")
        pd.DataFrame(carton_sheet, columns=carton_columns).to_excel(writer, index=False, sheet_name="Cartons")
    record_audit(
        "END_OF_DAY_EXPORTED",
        module="Orders",
        entity_type="end_of_day",
        client_id=client_id,
        detail=f"orders={len(rows)} cartons={len(carton_sheet)}",
    )
    db.session.commit()
    buffer.seek(0)
    return buffer.getvalue()
