"""Invoice numbering and creation.

Invoice-number generation is intentionally isolated here so that
client-specific numbering schemes can be added later without touching the
order-close workflow. The initial scheme is a global yearly sequence:

    INV-YYYY-000001
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func

from ..constants import InvoiceStatus
from ..extensions import db
from ..models import Box, BoxContent, Invoice, Order


def next_invoice_number(year: int | None = None) -> str:
    """Return the next global invoice number for the given year.

    Isolated so future client-specific sequences can override this.
    """
    year = year or datetime.utcnow().year
    prefix = f"INV-{year}-"
    last = (
        Invoice.query.filter(Invoice.invoice_number.like(f"{prefix}%"))
        .order_by(Invoice.invoice_number.desc())
        .first()
    )
    if last is None:
        seq = 1
    else:
        try:
            seq = int(last.invoice_number.rsplit("-", 1)[1]) + 1
        except (IndexError, ValueError):
            seq = (
                db.session.query(func.count(Invoice.id))
                .filter(Invoice.invoice_number.like(f"{prefix}%"))
                .scalar()
                or 0
            ) + 1
    return f"{prefix}{seq:06d}"


def create_invoice_for_order(order: Order, created_by: str = "system") -> Invoice:
    """Create exactly one invoice for a closed order.

    Raises if an invoice already exists for the order (enforced by the
    UNIQUE(order_id) constraint as well).
    """
    if order.invoice is not None:
        raise ValueError(f"Order {order.order_number} already has an invoice.")

    total_units = (
        BoxContent.query.join(Box, BoxContent.box_id == Box.id)
        .filter(Box.order_id == order.id)
        .count()
    )
    boxes = list(order.boxes)
    total_boxes = len(boxes)
    total_weight = round(sum(b.weight_kg or 0.0 for b in boxes), 3)

    invoice = Invoice(
        invoice_number=next_invoice_number(),
        order_id=order.id,
        client_id=order.client_id,
        warehouse_id=order.warehouse_id,
        order_type_id=order.order_type_id,
        customer=order.customer,
        carrier=order.carrier,
        shipping_service=order.shipping_service,
        total_units=total_units,
        total_boxes=total_boxes,
        total_weight=total_weight,
        status=InvoiceStatus.ISSUED,
        created_by=created_by,
    )
    db.session.add(invoice)
    db.session.flush()
    return invoice
