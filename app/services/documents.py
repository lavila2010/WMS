"""Document storage abstraction and closure PDF."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

from flask import current_app
from reportlab.lib.units import inch
from reportlab.platypus import Spacer

from ..auth import current_actor, record_audit
from ..constants import CartonStatus, InventoryIssueStatus, OrderStatus, UnitStatus
from ..extensions import db
from ..models import Carton, CartonContent, Document, InventoryIssue, InventoryUnit, Order, OrderLine, PickTicket
from .document_pdf import (
    build_pdf,
    data_table,
    display,
    format_time,
    format_ts,
    generated_now,
    header_block,
    kv_table,
    section_title,
    styles as pdf_styles,
    summary_row,
)


class DocumentStore:
    def save(self, name: str, data: bytes, content_type: str) -> str:
        raise NotImplementedError

    def open(self, storage_key: str) -> bytes:
        raise NotImplementedError

    def url_or_path(self, storage_key: str) -> str:
        raise NotImplementedError


class LocalDocumentStore(DocumentStore):
    def __init__(self, root: str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, name: str, data: bytes, content_type: str) -> str:
        key = f"{uuid4().hex}_{name}"
        path = self.root / key
        path.write_bytes(data)
        return key

    def open(self, storage_key: str) -> bytes:
        return (self.root / storage_key).read_bytes()

    def url_or_path(self, storage_key: str) -> str:
        return str(self.root / storage_key)


def get_store() -> DocumentStore:
    kind = (current_app.config.get("DOCUMENT_STORE") or os.environ.get("DOCUMENT_STORE") or "local").lower()
    if kind == "s3":
        # Credentials are optional during build; fall back to local and do not claim durable storage.
        if not os.environ.get("S3_BUCKET"):
            return LocalDocumentStore(current_app.config["DOCUMENTS_DIR"])
    return LocalDocumentStore(current_app.config["DOCUMENTS_DIR"])


def existing_closure_document(order: Order) -> Document | None:
    return (
        Document.query.filter_by(order_id=order.id, type="ORDER_CLOSURE")
        .order_by(Document.id.asc())
        .first()
    )


def _stored_pdf_exists(document: Document) -> bool:
    if not document or not document.storage_key or str(document.storage_key).startswith("pending-"):
        return False
    try:
        data = get_store().open(document.storage_key)
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return bool(data) and data[:4] == b"%PDF"


def reconciliation_snapshot(order: Order) -> dict:
    lines = list(order.lines)
    ordered = sum(line.qty_ordered for line in lines)
    allocated = sum(line.qty_allocated for line in lines)
    packed = sum(line.qty_packed for line in lines)
    shipped = sum(line.qty_shipped for line in lines)
    short = sum(int(getattr(line, "qty_short", 0) or 0) for line in lines)
    cartons = Carton.query.filter_by(order_id=order.id).all()
    total_weight = sum((carton.weight or 0) for carton in cartons)
    short_closed = bool(getattr(order, "short_closed", False))
    if short_closed:
        passed = ordered > 0 and (shipped + short) == ordered and shipped == packed and short > 0
        result = "CLOSED SHORT"
    else:
        passed = ordered == allocated == packed == shipped and ordered > 0
        result = "RECONCILED / PASS" if passed else "FAIL"
    return {
        "ordered": ordered,
        "allocated": allocated,
        "packed": packed,
        "shipped": shipped,
        "short": short,
        "carton_count": len(cartons),
        "total_weight": total_weight,
        "passed": passed,
        "short_closed": short_closed,
        "result": result,
    }


def _missing_unit_rows(order: Order) -> list[list]:
    issues = (
        InventoryIssue.query.filter_by(source_order_id=order.id, status=InventoryIssueStatus.MISSING)
        .order_by(InventoryIssue.id.asc())
        .all()
    )
    grouped = {}
    for issue in issues:
        unit = db.session.get(InventoryUnit, issue.inventory_unit_id)
        line = db.session.get(OrderLine, issue.source_order_line_id) if issue.source_order_line_id else None
        key = (
            issue.original_location,
            issue.upc,
            unit.sku if unit else (line.sku if line else None),
            (unit.description if unit else None) or (line.description if line else None),
            issue.status,
        )
        grouped.setdefault(key, 0)
        grouped[key] += 1
    return [
        [location, upc, sku, description, qty, status]
        for (location, upc, sku, description, status), qty in grouped.items()
    ]


def _carton_unit_rows(order: Order) -> list[list]:
    rows = (
        db.session.query(Carton, CartonContent, InventoryUnit)
        .join(CartonContent, CartonContent.carton_id == Carton.id)
        .join(InventoryUnit, InventoryUnit.id == CartonContent.inventory_unit_id)
        .filter(Carton.order_id == order.id)
        .order_by(Carton.id.asc(), InventoryUnit.location.asc(), InventoryUnit.upc.asc())
        .all()
    )
    detail = []
    for carton, _content, unit in rows:
        detail.append(
            [
                carton.carton_number,
                unit.location,
                unit.upc,
                unit.sku,
                unit.description,
                unit.style,
                unit.color,
                unit.size,
                UnitStatus.SHIPPED if unit.status == UnitStatus.SHIPPED else unit.status,
            ]
        )
    return detail


def render_closure_pdf(order: Order) -> bytes:
    ticket = PickTicket.query.filter_by(order_id=order.id).order_by(PickTicket.ticket_sequence.desc()).first()
    snap = reconciliation_snapshot(order)
    if order.status != OrderStatus.CLOSED:
        raise ValueError("Order Closure Report is generated only after successful reconciliation.")
    if getattr(order, "short_closed", False):
        if not snap["passed"]:
            raise ValueError("Order Closure Report is generated only after a confirmed short close.")
    elif not snap["passed"]:
        raise ValueError("Order Closure Report is generated only after successful reconciliation.")
    cartons = Carton.query.filter_by(order_id=order.id).order_by(Carton.id).all()
    s = pdf_styles()
    generated = generated_now()
    ticket_number = ticket.pick_ticket_number if ticket else "—"
    short_closed = bool(getattr(order, "short_closed", False))
    header_status = "CLOSED SHORT" if short_closed else OrderStatus.CLOSED
    summary_items = [
        ("Ordered Units", snap["ordered"]),
        ("Allocated Units", snap["allocated"]),
        ("Packed Units", snap["packed"]),
        ("Shipped Units", snap["shipped"]),
        ("Short / Missing Units", snap["short"]),
        ("Carton Count", snap["carton_count"]),
        ("Total Weight", f"{snap['total_weight']:g}" if snap["total_weight"] else "0"),
    ]
    if short_closed:
        recon_pairs = [
            ("Explicit reconciliation", "ORDERED = SHIPPED + SHORT"),
            ("Result", "CLOSED SHORT — not fully fulfilled"),
        ]
    else:
        recon_pairs = [
            ("Explicit reconciliation", "ORDERED = ALLOCATED = PACKED = SHIPPED"),
            ("Result", snap["result"]),
        ]
    story = [
        header_block(
            s,
            title="ORDER CLOSURE REPORT",
            ident=order.wms_order_id,
            status=header_status,
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
                ("Pick Ticket", ticket_number),
                ("Customer", order.customer),
                ("Customer Address", order.customer_address),
                ("Customer Phone", order.customer_phone),
                ("Carrier", order.carrier),
                ("Shipping Service", order.shipping_service),
                ("Closed Date", format_ts(order.closed_at, with_time=False)),
                ("Closed Time", format_time(order.closed_at)),
                ("Closed By", order.closed_by_username),
            ],
        ),
        Spacer(1, 10),
        section_title(s, "RECONCILIATION SUMMARY"),
        Spacer(1, 4),
        summary_row(s, summary_items),
        Spacer(1, 6),
        kv_table(s, recon_pairs, cols=2),
        Spacer(1, 10),
        section_title(s, "CARTON SUMMARY"),
        Spacer(1, 4),
        data_table(
            s,
            ["Carton #", "Length", "Width", "Height", "Dimension Unit", "Weight", "Weight Unit", "Units", "Status"],
            [
                [
                    carton.carton_number,
                    carton.length,
                    carton.width,
                    carton.height,
                    carton.dimension_unit,
                    f"{carton.weight:g}" if carton.weight is not None else None,
                    carton.weight_unit,
                    CartonContent.query.filter_by(carton_id=carton.id).count(),
                    carton.status or CartonStatus.CLOSED,
                ]
                for carton in cartons
            ],
            col_widths=[0.85 * inch, 0.55 * inch, 0.5 * inch, 0.5 * inch, 0.85 * inch, 0.55 * inch, 0.7 * inch, 0.5 * inch, 0.7 * inch],
        ),
        Spacer(1, 10),
        section_title(s, "UNIT DETAIL"),
        Spacer(1, 4),
        data_table(
            s,
            ["Carton #", "Location", "UPC", "SKU", "Description", "Style", "Color", "Size", "Status"],
            _carton_unit_rows(order),
            col_widths=[
                0.8 * inch,
                0.7 * inch,
                0.85 * inch,
                0.65 * inch,
                1.45 * inch,
                0.6 * inch,
                0.55 * inch,
                0.45 * inch,
                0.65 * inch,
            ],
        ),
    ]
    if short_closed:
        missing_rows = _missing_unit_rows(order)
        story.extend(
            [
                Spacer(1, 10),
                section_title(s, "MISSING / SHORT UNITS"),
                Spacer(1, 4),
                data_table(
                    s,
                    ["Location", "UPC", "SKU", "Description", "Qty", "Issue status"],
                    missing_rows,
                    col_widths=[0.9 * inch, 1.2 * inch, 0.9 * inch, 2.2 * inch, 0.5 * inch, 1.0 * inch],
                    numeric_last=False,
                ),
            ]
        )
    footer = {
        "left": f"{order.wms_order_id}  ·  {display(ticket_number)}",
        "mid": "WMS SYSTEM",
        "generated": f"Generated {format_ts(generated)}",
        "title": "ORDER CLOSURE REPORT",
    }
    return build_pdf(
        story,
        footer=footer,
        later_header={
            "title": "ORDER CLOSURE REPORT",
            "ident": order.wms_order_id,
            "status": header_status,
        },
    )


def persist_closure_pdf(order: Order, *, reuse: bool = True) -> Document:
    existing = existing_closure_document(order) if reuse else None
    if existing and _stored_pdf_exists(existing):
        return existing
    data = render_closure_pdf(order)
    store = get_store()
    filename = f"{order.wms_order_id}-closure.pdf"
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
            type="ORDER_CLOSURE",
            filename=filename,
            storage_key=key,
            created_by_user_id=uid,
            created_by_username=uname,
        )
        db.session.add(document)
    db.session.flush()
    if first_generation:
        record_audit(
            "PDF_GENERATED",
            module="Processing",
            entity_type="document",
            entity_id=document.id,
            client_id=order.client_id,
            detail=f"{filename} {order.wms_order_id}",
        )
    return document
