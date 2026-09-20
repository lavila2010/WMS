"""Permanent client-aware pick tickets."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from io import BytesIO

from flask import request

from ..auth import current_actor, record_audit
from ..constants import AllocationStatus, OrderStatus
from ..extensions import db
from ..models import Allocation, InventoryUnit, Order, OrderLine, PickTicket, PickTicketPrintEvent


class PickTicketError(ValueError):
    pass


def pick_ticket_number(order: Order) -> str:
    return f"{order.client.client_code}-{order.client_order_number}-01"


def ticket_lines(order: Order) -> list[dict]:
    rows = (
        db.session.query(Allocation, InventoryUnit, OrderLine)
        .join(InventoryUnit, InventoryUnit.id == Allocation.inventory_unit_id)
        .join(OrderLine, OrderLine.id == Allocation.order_line_id)
        .filter(
            Allocation.order_id == order.id,
            Allocation.status == AllocationStatus.ACTIVE,
        )
        .all()
    )
    grouped = defaultdict(lambda: {"qty": 0, "sku": None, "description": None})
    for allocation, unit, line in rows:
        key = (allocation.location, allocation.upc)
        grouped[key]["qty"] += 1
        grouped[key]["sku"] = unit.sku or line.sku
        grouped[key]["description"] = line.description
    return [
        {
            "location": location,
            "upc": upc,
            "sku": meta["sku"],
            "description": meta["description"],
            "qty": meta["qty"],
        }
        for (location, upc), meta in sorted(grouped.items(), key=lambda item: (item[0][0], item[0][1]))
    ]


def create_pick_ticket(order: Order) -> PickTicket:
    existing = PickTicket.query.filter_by(order_id=order.id).first()
    if existing:
        return existing
    if order.status != OrderStatus.ALLOCATED:
        raise PickTicketError("Pick ticket is allowed only for a fully ALLOCATED order.")
    uid, _ = current_actor()
    ticket = PickTicket(
        client_id=order.client_id,
        warehouse_id=order.warehouse_id,
        order_id=order.id,
        pick_ticket_number=pick_ticket_number(order),
        ticket_sequence=1,
        status="ACTIVE",
        assigned_by_user_id=uid,
    )
    db.session.add(ticket)
    db.session.flush()
    order.status = OrderStatus.PICK_TICKET_READY
    record_audit(
        "PICK_TICKET_CREATED",
        module="Orders",
        entity_type="pick_ticket",
        entity_id=ticket.id,
        client_id=order.client_id,
        detail=ticket.pick_ticket_number,
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
    record_audit(
        "PICK_TICKET_PRINTED",
        module="Orders",
        entity_type="pick_ticket",
        entity_id=ticket.id,
        client_id=ticket.client_id,
        detail=f"{ticket.pick_ticket_number} print #{ticket.print_count}",
    )
    db.session.commit()
    return event


def render_pdf(ticket: PickTicket) -> bytes:
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.pdfgen import canvas

    order = db.session.get(Order, ticket.order_id)
    lines = ticket_lines(order)
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter
    y = height - inch
    pdf.setFont("Helvetica-Bold", 14)
    pdf.drawString(inch, y, f"Pick Ticket {ticket.pick_ticket_number}")
    y -= 18
    pdf.setFont("Helvetica", 10)
    pdf.drawString(inch, y, f"Client {order.client.client_code}  Division {order.division.code}  WH {order.warehouse.warehouse_code}")
    y -= 14
    pdf.drawString(inch, y, f"Order {order.wms_order_id}  Customer {order.customer or '—'}  {order.carrier or ''} {order.shipping_service or ''}")
    y -= 22
    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawString(inch, y, "Location")
    pdf.drawString(inch + 100, y, "UPC")
    pdf.drawString(inch + 220, y, "SKU")
    pdf.drawString(inch + 320, y, "Description")
    pdf.drawString(inch + 460, y, "Qty")
    pdf.setFont("Helvetica", 9)
    y -= 14
    for line in lines:
        pdf.drawString(inch, y, str(line["location"]))
        pdf.drawString(inch + 100, y, str(line["upc"]))
        pdf.drawString(inch + 220, y, str(line["sku"] or "—"))
        pdf.drawString(inch + 320, y, str(line["description"] or "—")[:24])
        pdf.drawRightString(inch + 480, y, str(line["qty"]))
        y -= 12
        if y < inch:
            pdf.showPage()
            y = height - inch
    pdf.save()
    return buffer.getvalue()
