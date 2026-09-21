"""Immutable carton packing list generated at order close."""

from __future__ import annotations

from collections import defaultdict

from reportlab.lib.units import inch
from reportlab.platypus import KeepTogether, PageBreak, Paragraph, Spacer

from ..auth import current_actor, record_audit
from ..constants import InventoryIssueStatus, OrderStatus
from ..extensions import db
from ..models import Carton, CartonContent, Document, InventoryIssue, InventoryUnit, Order, OrderLine, PickTicket
from .document_pdf import (
    build_pdf,
    data_table,
    format_ts,
    generated_now,
    header_block,
    kv_table,
    section_title,
    styles as pdf_styles,
)
from .documents import _stored_pdf_exists, get_store


def existing_packing_list_document(order: Order, ticket: PickTicket | None = None) -> Document | None:
    query = Document.query.filter_by(order_id=order.id, type="PACKING_LIST")
    if ticket is not None:
        query = query.filter(Document.pick_ticket_id == ticket.id)
    return query.order_by(Document.id.asc()).first()


def carton_product_lines(carton: Carton) -> list[dict]:
    grouped = defaultdict(
        lambda: {"qty": 0, "sku": None, "description": None, "style": None, "color": None, "size": None}
    )
    rows = (
        db.session.query(CartonContent, InventoryUnit)
        .join(InventoryUnit, InventoryUnit.id == CartonContent.inventory_unit_id)
        .filter(CartonContent.carton_id == carton.id)
        .order_by(InventoryUnit.upc.asc(), InventoryUnit.id.asc())
        .all()
    )
    for _content, unit in rows:
        key = (unit.upc, unit.sku, unit.description, unit.style, unit.color, unit.size)
        grouped[key]["qty"] += 1
        grouped[key]["sku"] = unit.sku
        grouped[key]["description"] = unit.description
        grouped[key]["style"] = unit.style
        grouped[key]["color"] = unit.color
        grouped[key]["size"] = unit.size
    return [
        {
            "upc": upc,
            "sku": meta["sku"],
            "description": meta["description"],
            "style": meta["style"],
            "color": meta["color"],
            "size": meta["size"],
            "qty": meta["qty"],
        }
        for (upc, _sku, _desc, _style, _color, _size), meta in grouped.items()
    ]


def fulfillment_exceptions(order: Order, ticket: PickTicket | None = None) -> list[dict]:
    query = InventoryIssue.query.filter_by(
        source_order_id=order.id, status=InventoryIssueStatus.MISSING
    )
    if ticket is not None:
        query = query.filter(InventoryIssue.source_pick_ticket_id == ticket.id)
    issues = query.order_by(InventoryIssue.upc.asc(), InventoryIssue.id.asc()).all()
    grouped = defaultdict(
        lambda: {"sku": None, "description": None, "expected": 0, "shipped": 0, "missing": 0}
    )
    for issue in issues:
        line = db.session.get(OrderLine, issue.source_order_line_id) if issue.source_order_line_id else None
        unit = db.session.get(InventoryUnit, issue.inventory_unit_id)
        key = issue.upc
        grouped[key]["missing"] += 1
        grouped[key]["sku"] = (unit.sku if unit else None) or (line.sku if line else None)
        grouped[key]["description"] = (unit.description if unit else None) or (
            line.description if line else None
        )
        if line:
            grouped[key]["expected"] = int(line.qty_ordered)
            grouped[key]["shipped"] = int(line.qty_shipped or 0)
    return [
        {
            "upc": upc,
            "sku": meta["sku"],
            "description": meta["description"],
            "expected": meta["expected"],
            "shipped": meta["shipped"],
            "missing": meta["missing"],
        }
        for upc, meta in grouped.items()
    ]


def render_packing_list_pdf(order: Order, ticket: PickTicket | None = None) -> bytes:
    ticket = ticket or PickTicket.query.filter_by(order_id=order.id).order_by(PickTicket.ticket_sequence.desc()).first()
    if order.status != OrderStatus.CLOSED and not (ticket and ticket.status == "CLOSED"):
        raise ValueError("Packing List is generated only after a fulfillment wave is closed.")
    cartons_q = Carton.query.filter_by(order_id=order.id)
    if ticket:
        cartons_q = cartons_q.filter((Carton.pick_ticket_id == ticket.id) | (Carton.pick_ticket_id.is_(None)))
    cartons = cartons_q.order_by(Carton.id).all()
    if not cartons:
        raise ValueError("Packing List requires at least one carton.")
    s = pdf_styles()
    generated = generated_now()
    ticket_number = ticket.pick_ticket_number if ticket else "—"
    exceptions = fulfillment_exceptions(order, ticket)
    short_closed = bool(
        getattr(order, "short_closed", False) or (ticket and getattr(ticket, "short_closed", False))
    )
    header_status = "CLOSED SHORT" if short_closed else (
        OrderStatus.CLOSED if order.status == OrderStatus.CLOSED else "SHIPPED"
    )
    story = []
    page_footers = []
    total = len(cartons)
    for index, carton in enumerate(cartons, start=1):
        if index > 1:
            story.append(PageBreak())
        lines = carton_product_lines(carton)
        units_in_carton = sum(line["qty"] for line in lines)
        if units_in_carton != CartonContent.query.filter_by(carton_id=carton.id).count():
            raise ValueError("Packing List carton quantity does not reconcile.")
        block = [
            header_block(
                s,
                title="PACKING LIST",
                ident=order.wms_order_id,
                status=header_status,
            ),
            Spacer(1, 8),
            section_title(s, "ORDER INFORMATION"),
            Spacer(1, 3),
            kv_table(
                s,
                [
                    ("Client", order.client.client_code),
                    ("Division", order.division.code),
                    ("Warehouse", order.warehouse.warehouse_code),
                    ("WMS Order ID", order.wms_order_id),
                    ("Client Order Number", order.client_order_number),
                    ("Pick Ticket", ticket_number),
                    ("Customer", order.customer),
                    ("Customer Address", order.customer_address),
                    ("Customer Phone", order.customer_phone),
                    ("Carrier", order.carrier),
                    ("Shipping Service", order.shipping_service),
                    ("Closed Date", format_ts(order.closed_at, with_time=False)),
                    ("Closed By", order.closed_by_username),
                ],
            ),
            Spacer(1, 8),
            section_title(s, "CARTON INFORMATION"),
            Spacer(1, 3),
            kv_table(
                s,
                [
                    ("Carton Number", carton.carton_number),
                    ("Carton", f"{index} of {total}"),
                    ("Length", carton.length),
                    ("Width", carton.width),
                    ("Height", carton.height),
                    ("Dimension Unit", carton.dimension_unit),
                    ("Weight", f"{carton.weight:g}" if carton.weight is not None else None),
                    ("Weight Unit", carton.weight_unit),
                    ("Units in Carton", units_in_carton),
                ],
            ),
            Spacer(1, 8),
            section_title(s, "UNITS IN THIS CARTON"),
            Spacer(1, 3),
            data_table(
                s,
                ["UPC", "SKU", "Description", "Style", "Color", "Size", "Qty in Carton"],
                [
                    [
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
                    1.05 * inch,
                    0.85 * inch,
                    2.0 * inch,
                    0.75 * inch,
                    0.7 * inch,
                    0.55 * inch,
                    0.9 * inch,
                ],
                numeric_last=True,
            ),
        ]
        if index == total and exceptions:
            block.extend(
                [
                    Spacer(1, 10),
                    section_title(s, "FULFILLMENT EXCEPTIONS"),
                    Spacer(1, 3),
                    data_table(
                        s,
                        ["UPC", "SKU", "Description", "Expected", "Shipped", "Missing"],
                        [
                            [
                                row["upc"],
                                row["sku"],
                                row["description"],
                                row["expected"],
                                row["shipped"],
                                row["missing"],
                            ]
                            for row in exceptions
                        ],
                        col_widths=[
                            1.2 * inch,
                            0.9 * inch,
                            2.2 * inch,
                            0.8 * inch,
                            0.8 * inch,
                            0.8 * inch,
                        ],
                        numeric_last=True,
                    ),
                    Spacer(1, 6),
                    Paragraph("Order closed short with confirmed missing inventory.", s["body"]),
                ]
            )
        story.append(KeepTogether(block))
        page_footers.append(
            {
                "left": f"{order.wms_order_id}  ·  {ticket_number}  ·  {carton.carton_number}",
                "mid": (
                    "Order closed short with confirmed missing inventory."
                    if short_closed
                    else f"Closed by {order.closed_by_username or '—'}"
                ),
                "generated": f"Generated {format_ts(generated)}",
            }
        )
    return build_pdf(
        story,
        footer={
            "left": f"{order.wms_order_id}  ·  {ticket_number}",
            "generated": f"Generated {format_ts(generated)}",
            "title": "PACKING LIST",
        },
        page_footers=page_footers,
    )


def persist_packing_list_pdf(order: Order, *, reuse: bool = True, ticket: PickTicket | None = None) -> Document:
    existing = existing_packing_list_document(order, ticket) if reuse else None
    if existing and _stored_pdf_exists(existing):
        return existing
    data = render_packing_list_pdf(order, ticket=ticket)
    store = get_store()
    filename = f"{order.wms_order_id}-packing-list.pdf"
    key = store.save(filename, data, "application/pdf")
    uid, uname = current_actor()
    first_generation = existing is None or not _stored_pdf_exists(existing)
    if existing:
        existing.filename = filename
        existing.storage_key = key
        document = existing
    else:
        document = Document(
            client_id=order.client_id,
            order_id=order.id,
            type="PACKING_LIST",
            filename=filename,
            storage_key=key,
            created_by_user_id=uid,
            created_by_username=uname,
        )
        db.session.add(document)
    db.session.flush()
    if first_generation:
        record_audit(
            "PACKING_LIST_GENERATED",
            module="Processing",
            entity_type="document",
            entity_id=document.id,
            client_id=order.client_id,
            detail=f"{filename} {order.wms_order_id}",
        )
        record_audit(
            "PDF_GENERATED",
            module="Processing",
            entity_type="document",
            entity_id=document.id,
            client_id=order.client_id,
            detail=filename,
        )
    return document
