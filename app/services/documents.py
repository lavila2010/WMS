"""PDF document generation using reportlab.

Documents:
    - Pick Ticket
    - Order Closure Report
    - Packing Report
    - Box Detail
"""

from __future__ import annotations

import os
from datetime import datetime

from flask import current_app
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from ..constants import AllocationStatus, DocumentType
from ..extensions import db
from ..models import Allocation, Box, Document, Order
from .packing import reconcile


def _styles():
    return getSampleStyleSheet()


def _table(data, col_widths=None):
    tbl = Table(data, colWidths=col_widths, repeatRows=1)
    tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F4F6F8")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#6B7280")),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#E3E7EB")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F9FBFC")]),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TEXTCOLOR", (0, 1), (-1, -1), colors.HexColor("#182230")),
            ]
        )
    )
    return tbl


def _persist(elements, *, doc_type: str, order_id=None, box_id=None, label: str) -> Document:
    docs_dir = current_app.config["DOCUMENTS_DIR"]
    os.makedirs(docs_dir, exist_ok=True)
    stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
    filename = f"{doc_type.lower()}_{label}_{stamp}.pdf"
    path = os.path.join(docs_dir, filename)

    pdf = SimpleDocTemplate(path, pagesize=A4, title=f"{doc_type} {label}")
    pdf.build(elements)

    record = Document(
        order_id=order_id,
        box_id=box_id,
        type=doc_type,
        filename=filename,
        path=path,
    )
    db.session.add(record)
    db.session.commit()
    return record


def _header(styles, title: str, subtitle: str) -> list:
    return [
        Paragraph(f"<b>{title}</b>", styles["Title"]),
        Paragraph(subtitle, styles["Normal"]),
        Paragraph(
            f"Generated {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
            styles["Normal"],
        ),
        Spacer(1, 8 * mm),
    ]


def _scope_line(order: Order) -> str:
    client = order.client.code if order.client else "?"
    wh = order.warehouse.code if order.warehouse else "?"
    ot = order.order_type.code if order.order_type else "?"
    return f"Client {client} · Warehouse {wh} · Order Type {ot}"


def generate_pick_ticket(order: Order, pick_ticket=None) -> Document:
    """Warehouse Pick Ticket showing exact allocated barcode + location."""
    from .pick_tickets import pick_lines, unique_locations

    styles = _styles()
    pt_number = getattr(pick_ticket, "pick_ticket_number", None) or (
        order.pick_ticket.pick_ticket_number if getattr(order, "pick_ticket", None) else "—"
    )
    allocated_at = None
    if pick_ticket is not None:
        allocated_at = pick_ticket.assigned_at
    elif getattr(order, "pick_ticket", None):
        allocated_at = order.pick_ticket.assigned_at

    head = [
        [
            Paragraph("<b>WMS SYSTEM</b><br/>Warehouse Pick Ticket", styles["Heading2"]),
            Paragraph(
                f"<b>Pick Ticket</b> {pt_number}<br/>"
                f"<b>Order</b> {order.order_number}",
                styles["Normal"],
            ),
        ]
    ]
    head_tbl = Table(head, colWidths=[110 * mm, 55 * mm])
    head_tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    elements = [head_tbl, Spacer(1, 4 * mm)]

    info = [
        ["Order Information", ""],
        ["Client", order.client.code if order.client else "—"],
        ["Warehouse", order.warehouse.code if order.warehouse else "—"],
        ["Order Type", order.order_type.code if order.order_type else "—"],
        ["Customer", order.customer or "—"],
        ["Carrier", order.carrier or "—"],
        ["Shipping Service", order.shipping_service or "—"],
        ["Order Date", order.created_at.strftime("%Y-%m-%d %H:%M") if order.created_at else "—"],
        ["Allocated", allocated_at.strftime("%Y-%m-%d %H:%M") if allocated_at else "—"],
        ["Total Units", str(sum(line.quantity for line in order.lines))],
    ]
    elements.append(_table(info, col_widths=[50 * mm, 115 * mm]))
    elements.append(Spacer(1, 6 * mm))

    lines = pick_lines(order)
    rows = [["Seq", "Location", "SKU", "UPC", "Description", "Barcode", "Qty", "Picked"]]
    for i, line in enumerate(lines, 1):
        rows.append([
            str(i),
            line["location"],
            line["sku"],
            line["upc"],
            line["description"],
            line["barcode"],
            str(line["qty"]),
            "☐",
        ])
    if len(rows) == 1:
        rows.append(["—", "", "", "", "No allocated units", "", "", ""])
    elements.append(
        _table(
            rows,
            col_widths=[12 * mm, 24 * mm, 24 * mm, 28 * mm, 40 * mm, 28 * mm, 12 * mm, 16 * mm],
        )
    )
    elements.append(Spacer(1, 6 * mm))
    elements.append(
        Paragraph(
            f"Total Units to Pick: <b>{len(lines)}</b> &nbsp;&nbsp; "
            f"Unique Locations: <b>{unique_locations(order)}</b> &nbsp;&nbsp; "
            f"Status: <b>{getattr(pick_ticket, 'status', None) or 'ACTIVE'}</b>",
            styles["Normal"],
        )
    )
    return _persist(
        elements, doc_type=DocumentType.PICK_TICKET, order_id=order.id,
        label=order.order_number,
    )


def generate_packing_report(order: Order) -> Document:
    styles = _styles()
    elements = _header(
        styles, "Packing Report", f"Order {order.order_number}"
    )
    for box in sorted(order.boxes, key=lambda b: b.box_number):
        dims = "×".join(
            str(d) for d in [box.length_cm, box.width_cm, box.height_cm] if d
        ) or "n/a"
        elements.append(
            Paragraph(
                f"<b>Box {box.box_number}</b> — status {box.status}, "
                f"dims {dims} cm, weight {box.weight_kg or 'n/a'} kg, "
                f"{len(box.contents)} units",
                styles["Normal"],
            )
        )
        rows = [["#", "Barcode", "SKU"]]
        for i, c in enumerate(box.contents, 1):
            rows.append([str(i), c.barcode, c.unit.sku])
        if len(rows) == 1:
            rows.append(["—", "empty", ""])
        elements.append(_table(rows, col_widths=[12 * mm, 60 * mm, 40 * mm]))
        elements.append(Spacer(1, 5 * mm))
    if not order.boxes:
        elements.append(Paragraph("No boxes created yet.", styles["Normal"]))
    return _persist(
        elements, doc_type=DocumentType.PACKING_REPORT, order_id=order.id,
        label=order.order_number,
    )


def generate_box_detail(box: Box) -> Document:
    styles = _styles()
    dims = "×".join(
        str(d) for d in [box.length_cm, box.width_cm, box.height_cm] if d
    ) or "n/a"
    elements = _header(
        styles, "Box Detail",
        f"Box {box.box_number} — order {box.order.order_number}",
    )
    elements.append(
        Paragraph(
            f"Status: {box.status} · Dimensions: {dims} cm · "
            f"Weight: {box.weight_kg or 'n/a'} kg · Units: {len(box.contents)}",
            styles["Normal"],
        )
    )
    elements.append(Spacer(1, 5 * mm))
    rows = [["#", "Barcode", "SKU", "Description"]]
    for i, c in enumerate(box.contents, 1):
        rows.append([str(i), c.barcode, c.unit.sku, c.unit.description or ""])
    if len(rows) == 1:
        rows.append(["—", "empty", "", ""])
    elements.append(_table(rows, col_widths=[12 * mm, 55 * mm, 30 * mm, 65 * mm]))
    return _persist(
        elements, doc_type=DocumentType.BOX_DETAIL, order_id=box.order_id,
        box_id=box.id, label=f"{box.order.order_number}-{box.box_number}",
    )


def generate_order_closure(order: Order) -> Document:
    styles = _styles()
    rec = reconcile(order)
    invoice = order.invoice
    total_weight = round(sum(b.weight_kg or 0.0 for b in order.boxes), 3)
    elements = _header(
        styles, "Order Closure Report",
        f"Order {order.order_number} — {order.customer or 'N/A'} "
        f"(status {order.status})",
    )
    elements.append(Paragraph(_scope_line(order), styles["Normal"]))
    elements.append(Spacer(1, 4 * mm))
    summary = [
        ["Field", "Value"],
        ["Invoice Number", invoice.invoice_number if invoice else "—"],
        ["Order Number", order.order_number],
        ["Client", order.client.code if order.client else "—"],
        ["Warehouse", order.warehouse.code if order.warehouse else "—"],
        ["Order Type", order.order_type.code if order.order_type else "—"],
        ["Customer", order.customer or "—"],
        ["Carrier", order.carrier or "—"],
        ["Shipping Service", order.shipping_service or "—"],
        ["Total Units", str(rec["packed"])],
        ["Total Boxes", str(len(order.boxes))],
        ["Total Weight (kg)", str(total_weight)],
        ["Reconciliation", "PASS" if rec["ok"] else "FAIL"],
    ]
    elements.append(_table(summary, col_widths=[60 * mm, 40 * mm]))
    elements.append(Spacer(1, 6 * mm))

    rows = [["Box", "Dimensions (cm)", "Weight (kg)", "Units", "Status"]]
    for box in sorted(order.boxes, key=lambda b: b.box_number):
        dims = "×".join(
            str(d) for d in [box.length_cm, box.width_cm, box.height_cm] if d
        ) or "n/a"
        rows.append([
            box.box_number, dims, str(box.weight_kg or "n/a"),
            str(len(box.contents)), box.status,
        ])
    if len(rows) == 1:
        rows.append(["—", "n/a", "n/a", "0", "n/a"])
    elements.append(_table(rows))
    return _persist(
        elements, doc_type=DocumentType.ORDER_CLOSURE, order_id=order.id,
        label=order.order_number,
    )
