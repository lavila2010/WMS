"""Live Daily Allocation Exceptions report (America/New_York)."""

from __future__ import annotations

from io import BytesIO

import pandas as pd
from sqlalchemy import or_
from sqlalchemy.orm import contains_eager

from ..auth import record_audit
from ..constants import OrderStatus
from ..extensions import db
from ..models import Client, Division, Order, Warehouse
from .allocation_overview import bulk_order_quantity_rows, bulk_upc_rows
from .end_of_day import ny_day_utc_bounds, operational_today, parse_eod_date, to_ny
from .order_visibility import apply_operational_order_visibility


EXCEPTION_STATUSES = {
    OrderStatus.UNALLOCATED,
    OrderStatus.PARTIALLY_ALLOCATED,
    OrderStatus.PARTIALLY_FULFILLED,
    OrderStatus.ALLOCATED,
    OrderStatus.PICK_TICKET_READY,
    OrderStatus.PROCESSING,
}


def list_daily_exceptions(
    *,
    day=None,
    client_id=None,
    division_id=None,
    warehouse_id=None,
    allocation_status="",
    q="",
    accessible_client_ids=None,
    admin=False,
) -> dict:
    day = day or operational_today()
    start, end = ny_day_utc_bounds(day)
    query = apply_operational_order_visibility(
        Order.query.filter(Order.created_at >= start, Order.created_at < end)
        .filter(Order.status.notin_([OrderStatus.CLOSED, OrderStatus.CANCELLED]))
    )
    if client_id:
        query = query.filter(Order.client_id == client_id)
    elif not admin:
        query = query.filter(Order.client_id.in_(accessible_client_ids or [-1]))
    if division_id:
        query = query.filter(Order.division_id == division_id)
    if warehouse_id:
        query = query.filter(Order.warehouse_id == warehouse_id)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(
            or_(
                Order.wms_order_id.ilike(like),
                Order.client_order_number.ilike(like),
                Order.customer.ilike(like),
            )
        )
    query = (
        query.join(Client, Client.id == Order.client_id)
        .join(Division, Division.id == Order.division_id)
        .join(Warehouse, Warehouse.id == Order.warehouse_id)
        .options(
            contains_eager(Order.client),
            contains_eager(Order.division),
            contains_eager(Order.warehouse),
        )
        .order_by(
            Client.client_code.asc(),
            Division.code.asc(),
            Warehouse.warehouse_code.asc(),
            Order.created_at.asc(),
            Order.wms_order_id.asc(),
        )
    )
    orders = query.all()
    qty_map = bulk_order_quantity_rows([order.id for order in orders])
    upc_map = bulk_upc_rows([order.id for order in orders])
    rows = []
    for order in orders:
        qty = qty_map.get(order.id)
        if qty is None or qty["remaining"] <= 0:
            continue
        status = order.status
        if status == OrderStatus.PARTIALLY_FULFILLED and qty["currently_allocated"] > 0:
            status = OrderStatus.PARTIALLY_ALLOCATED
        if allocation_status == "UNALLOCATED" and qty["currently_allocated"] > 0:
            continue
        if allocation_status == "PARTIALLY_ALLOCATED" and not (
            qty["currently_allocated"] > 0 and qty["remaining"] > 0
        ):
            continue
        rows.append(
            {
                "order": order,
                "ordered": qty["ordered"],
                "shipped": qty["shipped"],
                "currently_allocated": qty["currently_allocated"],
                "remaining": qty["remaining"],
                "allocation_status": status,
                "partial_approval": qty["partial_approval"],
                "upc_rows": upc_map.get(order.id, []),
                "created_ny": to_ny(order.created_at),
                "last_attempt_ny": to_ny(order.last_allocation_attempt_at),
            }
        )
    kpis = {
        "orders_not_fully_allocated": len(rows),
        "unallocated_orders": sum(1 for r in rows if r["currently_allocated"] == 0),
        "partially_allocated_orders": sum(1 for r in rows if r["currently_allocated"] > 0),
        "outstanding_units": sum(r["remaining"] for r in rows),
    }
    return {"day": day, "rows": rows, "kpis": kpis}


def export_daily_exceptions(payload: dict) -> bytes:
    day = payload["day"]
    order_rows = []
    upc_rows = []
    for row in payload["rows"]:
        order = row["order"]
        order_rows.append(
            {
                "Date": day.isoformat(),
                "Client": order.client.client_code,
                "Division": order.division.code,
                "Warehouse": order.warehouse.warehouse_code,
                "WMS Order ID": order.wms_order_id,
                "Client Order Number": order.client_order_number,
                "Customer": order.customer,
                "Ordered Units": row["ordered"],
                "Already Shipped": row["shipped"],
                "Currently Allocated": row["currently_allocated"],
                "Remaining To Allocate": row["remaining"],
                "Allocation Status": row["allocation_status"],
                "Partial Approval Status": row["partial_approval"],
                "Created Time": row["created_ny"].strftime("%Y-%m-%d %H:%M") if row["created_ny"] else "",
                "Last Allocation Attempt": (
                    row["last_attempt_ny"].strftime("%Y-%m-%d %H:%M") if row["last_attempt_ny"] else ""
                ),
            }
        )
        for upc in row["upc_rows"]:
            if upc["remaining_qty"] <= 0:
                continue
            upc_rows.append(
                {
                    "Client": order.client.client_code,
                    "Division": order.division.code,
                    "Warehouse": order.warehouse.warehouse_code,
                    "WMS Order ID": order.wms_order_id,
                    "UPC": upc["upc"],
                    "SKU": upc["sku"],
                    "Description": upc["description"],
                    "Style": upc["style"],
                    "Color": upc["color"],
                    "Size": upc["size"],
                    "Remaining Qty": upc["remaining_qty"],
                }
            )
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        pd.DataFrame(order_rows).to_excel(writer, index=False, sheet_name="Orders")
        pd.DataFrame(upc_rows).to_excel(writer, index=False, sheet_name="Outstanding by UPC")
    record_audit(
        "DAILY_ALLOCATION_EXCEPTION_EXPORTED",
        module="Allocation",
        entity_type="allocation_exceptions",
        detail=f"date={day.isoformat()} rows={len(order_rows)}",
    )
    db.session.commit()
    return buffer.getvalue()


def parse_exception_date(value: str | None):
    return parse_eod_date(value)
