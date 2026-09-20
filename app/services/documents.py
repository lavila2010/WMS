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
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f1f5f9")]),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
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


def generate_pick_ticket(order: Order) -> Document:
    styles = _styles()
    elements = _header(
        styles, "Pick Ticket",
        f"Order {order.order_number} — {order.customer or 'N/A'} "
        f"(status {order.status})",
    )
    elements.append(Paragraph(_scope_line(order), styles["Normal"]))
    elements.append(
        Paragraph(
            f"Carrier: {order.carrier or 'N/A'} · "
            f"Shipping Service: {order.shipping_service or 'N/A'}",
            styles["Normal"],
        )
    )
    elements.append(Spacer(1, 5 * mm))
    allocs = (
        Allocation.query.filter_by(order_id=order.id, status=AllocationStatus.ACTIVE)
        .all()
    )
    rows = [["#", "Barcode", "SKU", "Description", "Location"]]
    for i, a in enumerate(sorted(allocs, key=lambda x: (x.unit.location or "", x.barcode)), 1):
        rows.append([
            str(i), a.barcode, a.unit.sku,
            a.unit.description or "", a.unit.location or "",
        ])
    if len(rows) == 1:
        rows.append(["—", "No allocated units", "", "", ""])
    elements.append(_table(rows, col_widths=[12 * mm, 45 * mm, 30 * mm, 55 * mm, 25 * mm]))
    elements.append(Spacer(1, 6 * mm))
    elements.append(
        Paragraph(f"Total units to pick: <b>{len(allocs)}</b>", styles["Normal"])
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
