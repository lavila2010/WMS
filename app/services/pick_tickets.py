"""Permanent client-aware pick tickets."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from flask import request
from sqlalchemy import case, func, or_, select

from ..auth import current_actor, record_audit
from ..constants import (
    AllocationStatus,
    OrderStatus,
    PickTicketStatus,
)
from ..extensions import db
from ..models import Allocation, InventoryUnit, Order, OrderLine, PickTicket, PickTicketPrintEvent
from ..services.fulfillment import (
    current_wave_allocation_count,
    current_wave_approved,
    current_wave_unticketed_allocations,
    order_quantities,
    pick_ticket_eligible,
)
from ..services.order_visibility import apply_operational_order_visibility
from .document_pdf import (
    build_pdf,
    data_table,
    format_ts,
    generated_now,
    header_block,
    kv_table,
    section_title,
    styles as pdf_styles,
    summary_row,
)


class PickTicketError(ValueError):
    pass


def pick_ticket_number(order: Order, sequence: int = 1) -> str:
    return f"{order.wms_order_id}-{int(sequence):02d}"


def next_ticket_sequence(order: Order) -> int:
    current = (
        db.session.query(db.func.max(PickTicket.ticket_sequence))
        .filter(PickTicket.order_id == order.id)
        .scalar()
    )
    return int(current or 0) + 1


def desired_ticket_status(order: Order | None, ticket: PickTicket | None = None) -> str:
    if order is not None and order.status == OrderStatus.CANCELLED:
        return PickTicketStatus.CANCELLED
    if order is not None and order.status == OrderStatus.CLOSED:
        return PickTicketStatus.CLOSED
    if ticket is not None:
        return PickTicketStatus.OPEN if ticket.status == "ACTIVE" else ticket.status
    return PickTicketStatus.OPEN


def sync_ticket_status(ticket: PickTicket, order: Order | None = None) -> str:
    order = order or ticket.order or db.session.get(Order, ticket.order_id)
    if order is not None and order.status == OrderStatus.CANCELLED:
        ticket.status = PickTicketStatus.CANCELLED
        return ticket.status
    if ticket.status == "ACTIVE":
        ticket.status = PickTicketStatus.OPEN
    return ticket.status


def ticket_lines(order: Order, ticket: PickTicket | None = None) -> list[dict]:
    query = (
        db.session.query(Allocation, InventoryUnit, OrderLine)
        .join(InventoryUnit, InventoryUnit.id == Allocation.inventory_unit_id)
        .join(OrderLine, OrderLine.id == Allocation.order_line_id)
        .filter(
            Allocation.order_id == order.id,
            Allocation.status == AllocationStatus.ACTIVE,
        )
    )
    if ticket is not None:
        query = query.filter(Allocation.pick_ticket_id == ticket.id)
    rows = query.all()
    grouped = defaultdict(
        lambda: {
            "qty": 0,
            "sku": None,
            "description": None,
            "style": None,
            "color": None,
            "size": None,
        }
    )
    for allocation, unit, line in rows:
        key = (allocation.location, allocation.upc)
        grouped[key]["qty"] += 1
        grouped[key]["sku"] = unit.sku or line.sku or grouped[key]["sku"]
        grouped[key]["description"] = unit.description or line.description or grouped[key]["description"]
        grouped[key]["style"] = unit.style or grouped[key]["style"]
        grouped[key]["color"] = unit.color or grouped[key]["color"]
        grouped[key]["size"] = unit.size or grouped[key]["size"]
    return [
        {
            "location": location,
            "upc": upc,
            "sku": meta["sku"],
            "description": meta["description"],
            "style": meta["style"],
            "color": meta["color"],
            "size": meta["size"],
            "qty": meta["qty"],
        }
        for (location, upc), meta in sorted(grouped.items(), key=lambda item: (item[0][0] or "", item[0][1] or ""))
    ]


def create_pick_ticket(order: Order) -> PickTicket:
    locked = db.session.execute(
        select(Order).where(Order.id == order.id).with_for_update()
    ).scalar_one_or_none()
    if locked is None:
        raise PickTicketError("Order not found.")
    pending = current_wave_unticketed_allocations(locked)
    if not pending:
        existing = (
            PickTicket.query.filter_by(order_id=locked.id)
            .order_by(PickTicket.ticket_sequence.desc())
            .first()
        )
        if existing:
            return existing
        raise PickTicketError("No allocated units are available for a pick ticket.")
    if not pick_ticket_eligible(locked):
        qty = order_quantities(locked)
        if qty["remaining"] > 0 and not current_wave_approved(locked):
            raise PickTicketError("Partial allocation must be approved before a pick ticket can be created.")
        raise PickTicketError("Pick ticket is allowed only for a fully allocated or approved partial wave.")
    uid, _ = current_actor()
    sequence = next_ticket_sequence(locked)
    ticket = PickTicket(
        client_id=locked.client_id,
        warehouse_id=locked.warehouse_id,
        order_id=locked.id,
        pick_ticket_number=pick_ticket_number(locked, sequence),
        ticket_sequence=sequence,
        revision_number=1,
        status=PickTicketStatus.OPEN,
        assigned_by_user_id=uid,
    )
    db.session.add(ticket)
    db.session.flush()
    for allocation in pending:
        allocation.pick_ticket_id = ticket.id
    qty = order_quantities(locked)
    event = "PARTIAL_PICK_TICKET_CREATED" if qty["remaining"] > 0 else "PICK_TICKET_CREATED"
    locked.status = OrderStatus.PICK_TICKET_READY
    record_audit(
        event,
        module="Orders",
        entity_type="pick_ticket",
        entity_id=ticket.id,
        client_id=locked.client_id,
        detail=f"{ticket.pick_ticket_number} {locked.wms_order_id} qty={len(pending)}",
    )
    db.session.commit()
    return ticket


def record_print(ticket: PickTicket, *, source="UI") -> PickTicketPrintEvent:
    uid, uname = current_actor()
    ip = None
    try:
        ip = request.remote_addr
    except Exception:
        ip = None
    ticket.print_count = (ticket.print_count or 0) + 1
    ticket.last_printed_at = datetime.utcnow()
    ticket.last_printed_by = uname
    event = PickTicketPrintEvent(
        pick_ticket_id=ticket.id,
        client_id=ticket.client_id,
        user_id=uid,
        username=uname,
        source=source,
        ip_address=ip,
    )
    db.session.add(event)
    event_type = "PICK_TICKET_PRINTED" if ticket.print_count == 1 else "PICK_TICKET_REPRINTED"
    record_audit(
        event_type,
        module="Orders",
        entity_type="pick_ticket",
        entity_id=ticket.id,
        client_id=ticket.client_id,
        detail=f"{ticket.pick_ticket_number} print #{ticket.print_count}",
    )
    if ticket.print_count == 1:
        record_audit(
            "PDF_PRINTED",
            module="Orders",
            entity_type="pick_ticket",
            entity_id=ticket.id,
            client_id=ticket.client_id,
            detail=ticket.pick_ticket_number,
        )
    else:
        record_audit(
            "PDF_REPRINTED",
            module="Orders",
            entity_type="pick_ticket",
            entity_id=ticket.id,
            client_id=ticket.client_id,
            detail=ticket.pick_ticket_number,
        )
    db.session.commit()
    return event


def ticket_summary(order: Order, lines: list[dict] | None = None) -> dict:
    lines = lines if lines is not None else ticket_lines(order)
    return {
        "total_units": sum(line["qty"] for line in lines),
        "unique_upcs": len({line["upc"] for line in lines}),
        "locations": len({line["location"] for line in lines if line["location"]}),
        "order_status": order.status,
    }


def render_pdf(ticket: PickTicket) -> bytes:
    from reportlab.platypus import Spacer

    order = db.session.get(Order, ticket.order_id)
    lines = ticket_lines(order, ticket)
    summary = ticket_summary(order, lines)
    derived = desired_ticket_status(order, ticket)
    s = pdf_styles()
    generated = generated_now()
    story = [
        header_block(
            s,
            title="PICK TICKET",
            ident=f"{ticket.pick_ticket_number}  Rev {ticket.revision_number or 1}",
            status=derived,
        ),
        Spacer(1, 10),
        section_title(s, "ORDER INFORMATION"),
        Spacer(1, 4),
        kv_table(
            s,
            [
                ("Client", order.client.client_code),
                ("Division", order.division.code),
                ("Warehouse", order.warehouse.warehouse_code),
                ("WMS Order ID", order.wms_order_id),
                ("Client Order Number", order.client_order_number),
                ("Pick Ticket Number", ticket.pick_ticket_number),
                ("Revision", str(ticket.revision_number or 1)),
                ("Customer", order.customer),
                ("Customer Address", order.customer_address),
                ("Customer Phone", order.customer_phone),
                ("Carrier", order.carrier),
                ("Shipping Service", order.shipping_service),
                ("Order Status", order.status),
                ("Pick Ticket Status", derived),
                ("Created Date", format_ts(ticket.created_at, with_time=False)),
                ("Printed Date", format_ts(ticket.last_printed_at)),
                ("Printed By", ticket.last_printed_by),
            ],
        ),
        Spacer(1, 10),
        section_title(s, "PICKING SUMMARY"),
        Spacer(1, 4),
        summary_row(
            s,
            [
                ("Total Units", summary["total_units"]),
                ("Unique UPCs", summary["unique_upcs"]),
                ("Locations", summary["locations"]),
                ("Order Status", summary["order_status"]),
            ],
        ),
        Spacer(1, 10),
        section_title(s, "PICKING DETAIL"),
        Spacer(1, 4),
        data_table(
            s,
            ["Location", "UPC", "SKU", "Description", "Style", "Color", "Size", "Qty"],
            [
                [
                    line["location"],
                    line["upc"],
                    line["sku"],
                    line["description"],
                    line["style"],
                    line["color"],
                    line["size"],
                    line["qty"],
                ]
                for line in lines
            ],
            col_widths=[
                0.9 * 72,
                1.0 * 72,
                0.75 * 72,
                2.05 * 72,
                0.75 * 72,
                0.7 * 72,
                0.55 * 72,
                0.4 * 72,
            ],
            numeric_last=True,
        ),
    ]
    footer = {
        "left": f"{ticket.pick_ticket_number}  ·  WMS SYSTEM",
        "generated": f"Generated {format_ts(generated)}",
        "title": "PICK TICKET",
    }
    return build_pdf(
        story,
        footer=footer,
        later_header={
            "title": "PICK TICKET",
            "ident": f"{ticket.pick_ticket_number}  Rev {ticket.revision_number or 1}",
            "status": derived,
        },
    )


SORT_KEYS = {
    "ticket": PickTicket.pick_ticket_number,
    "wms_order": Order.wms_order_id,
    "customer": Order.customer,
    "status": "status",
    "created": PickTicket.created_at,
    "printed": PickTicket.last_printed_at,
    "units": "units",
    "client": "client",
    "division": "division",
    "warehouse": "warehouse",
}


def status_expression():
    return case(
        (Order.status == OrderStatus.CANCELLED, PickTicketStatus.CANCELLED),
        (PickTicket.status == "ACTIVE", PickTicketStatus.OPEN),
        else_=PickTicket.status,
    )


def list_pick_tickets(
    *,
    client_id: int,
    division_id=None,
    warehouse_id=None,
    status=PickTicketStatus.OPEN,
    date_str="",
    q="",
    sort="created",
    direction="desc",
):
    from ..models import Client, Division, Warehouse

    units_sq = (
        db.session.query(
            Allocation.pick_ticket_id.label("pick_ticket_id"),
            func.count(Allocation.id).label("units"),
            func.count(func.distinct(Allocation.location)).label("locations"),
        )
        .filter(Allocation.status == AllocationStatus.ACTIVE, Allocation.pick_ticket_id.isnot(None))
        .group_by(Allocation.pick_ticket_id)
        .subquery()
    )
    derived = status_expression()
    query = (
        db.session.query(PickTicket, Order, units_sq.c.units, units_sq.c.locations, derived.label("derived_status"))
        .join(Order, Order.id == PickTicket.order_id)
        .outerjoin(Client, Client.id == Order.client_id)
        .outerjoin(Division, Division.id == Order.division_id)
        .outerjoin(Warehouse, Warehouse.id == Order.warehouse_id)
        .outerjoin(units_sq, units_sq.c.pick_ticket_id == PickTicket.id)
    )
    query = apply_operational_order_visibility(query).filter(PickTicket.client_id == client_id)
    if division_id:
        query = query.filter(Order.division_id == division_id)
    if warehouse_id:
        query = query.filter(Order.warehouse_id == warehouse_id)
    status_key = (status or PickTicketStatus.OPEN).upper()
    if status_key in (PickTicketStatus.OPEN, PickTicketStatus.CLOSED, PickTicketStatus.CANCELLED):
        query = query.filter(derived == status_key)
    if date_str:
        day = datetime.strptime(date_str, "%Y-%m-%d")
        query = query.filter(PickTicket.created_at >= day, PickTicket.created_at < day + timedelta(days=1))
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(
            or_(
                PickTicket.pick_ticket_number.ilike(like),
                Order.wms_order_id.ilike(like),
                Order.client_order_number.ilike(like),
                Order.customer.ilike(like),
            )
        )
    sort_key = SORT_KEYS.get(sort, PickTicket.created_at)
    descending = (direction or "desc").lower() != "asc"
    if sort_key == "units":
        order_cols = [units_sq.c.units]
    elif sort_key == "status":
        order_cols = [derived]
    elif sort == "client":
        order_cols = [Client.client_code, Division.code, Warehouse.warehouse_code, PickTicket.pick_ticket_number]
    elif sort == "division":
        order_cols = [Division.code, Warehouse.warehouse_code, PickTicket.pick_ticket_number]
    elif sort == "warehouse":
        order_cols = [Warehouse.warehouse_code, PickTicket.pick_ticket_number]
    else:
        order_cols = [sort_key]
    ordered = [col.desc() if descending else col.asc() for col in order_cols]
    query = query.order_by(*ordered, PickTicket.id.desc())
    rows = []
    for ticket, order, units, locations, derived_status in query.all():
        rows.append(
            {
                "ticket": ticket,
                "order": order,
                "units": int(units or 0),
                "locations": int(locations or 0),
                "status": derived_status,
            }
        )
    return rows


def list_eligible_pick_ticket_orders(*, client_id: int, division_id=None, warehouse_id=None) -> list[dict]:
    """Orders waiting for a Pick Ticket: fully allocated OR approved partial current wave.

    Does not filter by Order.status == ALLOCATED. Closed/cancelled are excluded first,
    then pick_ticket_eligible() is applied.
    """
    query = apply_operational_order_visibility(
        Order.query.filter(
            Order.client_id == int(client_id),
            Order.status.notin_([OrderStatus.CLOSED, OrderStatus.CANCELLED]),
        )
    )
    if division_id:
        query = query.filter(Order.division_id == int(division_id))
    if warehouse_id:
        query = query.filter(Order.warehouse_id == int(warehouse_id))
    rows = []
    for order in query.order_by(Order.created_at.desc()).all():
        qty = order_quantities(order)
        if not pick_ticket_eligible(order, qty):
            continue
        full = qty["remaining"] == 0
        rows.append(
            {
                "order": order,
                "ordered": qty["ordered"],
                "allocated_current_wave": current_wave_allocation_count(order),
                "remaining": qty["remaining"],
                "allocation_type": "FULL" if full else "PARTIAL APPROVED",
                "approval_status": "—" if full else "PARTIAL APPROVED",
            }
        )
    return rows
