"""Pick ticket numbering, eligibility, pick lines, and print history.

A Pick Ticket Number is permanently assigned once an order becomes fully
allocated. Reprinting never generates a new number.
"""

from __future__ import annotations

from datetime import datetime

from flask import request
from sqlalchemy import func

from ..auth import current_actor
from ..constants import AllocationStatus, OrderStatus, PickTicketStatus, PrintSource
from ..extensions import db
from ..models import Allocation, Order, PickTicket, PickTicketPrintEvent
from .allocation import allocation_progress, is_fully_allocated


def next_pick_ticket_number(year: int | None = None) -> str:
    year = year or datetime.utcnow().year
    prefix = f"PT-{year}-"
    last = (
        PickTicket.query.filter(PickTicket.pick_ticket_number.like(f"{prefix}%"))
        .order_by(PickTicket.pick_ticket_number.desc())
        .first()
    )
    if last is None:
        seq = 1
    else:
        try:
            seq = int(last.pick_ticket_number.rsplit("-", 1)[1]) + 1
        except (IndexError, ValueError):
            seq = (
                db.session.query(func.count(PickTicket.id))
                .filter(PickTicket.pick_ticket_number.like(f"{prefix}%"))
                .scalar()
                or 0
            ) + 1
    return f"{prefix}{seq:06d}"


def ensure_pick_ticket(order: Order) -> PickTicket:
    """Return the permanent pick ticket for ``order``, creating it if needed."""
    existing = PickTicket.query.filter_by(order_id=order.id).first()
    if existing is not None:
        return existing
    uid, uname = current_actor()
    ticket = PickTicket(
        pick_ticket_number=next_pick_ticket_number(),
        order_id=order.id,
        status=PickTicketStatus.ACTIVE,
        assigned_at=datetime.utcnow(),
        assigned_by_user_id=uid,
        assigned_by_username=uname,
    )
    db.session.add(ticket)
    db.session.flush()
    return ticket


def is_eligible(order: Order) -> bool:
    return order.status != OrderStatus.CLOSED and is_fully_allocated(order)


def eligible_orders(client_id: int, warehouse_id: int | None = None):
    q = Order.query.filter(Order.status != OrderStatus.CLOSED, Order.client_id == client_id)
    if warehouse_id:
        q = q.filter(Order.warehouse_id == warehouse_id)
    orders = q.order_by(Order.created_at.desc()).all()
    return [o for o in orders if is_fully_allocated(o)]


def sync_eligible_tickets(client_id: int, warehouse_id: int | None = None) -> list[PickTicket]:
    """Assign pick ticket numbers to every eligible order that is missing one."""
    tickets = []
    for order in eligible_orders(client_id, warehouse_id):
        tickets.append(ensure_pick_ticket(order))
    db.session.flush()
    return tickets


def pick_lines(order: Order) -> list[dict]:
    """Exact allocated physical units, sorted Location → SKU → Barcode."""
    allocs = Allocation.query.filter_by(
        order_id=order.id, status=AllocationStatus.ACTIVE
    ).all()
    rows = []
    for a in allocs:
        unit = a.unit
        rows.append(
            {
                "location": unit.location or "",
                "sku": unit.sku,
                "upc": unit.upc or "",
                "description": unit.description or "",
                "barcode": a.barcode,
                "qty": 1,
            }
        )
    rows.sort(key=lambda r: (r["location"], r["sku"], r["barcode"]))
    return rows


def unique_locations(order: Order) -> int:
    return len({row["location"] for row in pick_lines(order) if row["location"]})


def record_print(ticket: PickTicket, source: str) -> PickTicketPrintEvent:
    uid, uname = current_actor()
    ip = None
    try:
        ip = request.remote_addr
    except Exception:
        ip = None
    event = PickTicketPrintEvent(
        pick_ticket_id=ticket.id,
        user_id=uid,
        username=uname,
        source=source,
        ip_address=ip,
    )
    db.session.add(event)
    db.session.flush()
    return event


def printed_by_usernames(client_id: int, warehouse_id: int | None = None) -> list[str]:
    q = (
        db.session.query(PickTicketPrintEvent.username)
        .join(PickTicket, PickTicket.id == PickTicketPrintEvent.pick_ticket_id)
        .join(Order, Order.id == PickTicket.order_id)
        .filter(Order.client_id == client_id, PickTicketPrintEvent.username.isnot(None))
    )
    if warehouse_id:
        q = q.filter(Order.warehouse_id == warehouse_id)
    names = sorted({n for (n,) in q.all() if n})
    return names


def ticket_row(ticket: PickTicket) -> dict:
    order = ticket.order
    progress = allocation_progress(order)
    last = ticket.last_print
    return {
        "ticket": ticket,
        "order": order,
        "units": progress["total_ordered"],
        "locations": unique_locations(order),
        "print_count": ticket.print_count,
        "last_printed": last.printed_at if last else None,
        "last_printed_by": last.username if last else None,
        "printed": ticket.print_count > 0,
    }
