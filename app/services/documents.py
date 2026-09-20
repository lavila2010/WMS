"""Document storage abstraction and closure PDF."""

from __future__ import annotations

import os
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from flask import current_app
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from ..auth import current_actor, record_audit
from ..extensions import db
from ..models import Carton, CartonContent, Document, InventoryUnit, Order, PickTicket


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


def render_closure_pdf(order: Order) -> bytes:
    ticket = PickTicket.query.filter_by(order_id=order.id).first()
    cartons = Carton.query.filter_by(order_id=order.id).order_by(Carton.id).all()
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter
    y = height - 56
    pdf.setFont("Helvetica-Bold", 14)
    pdf.drawString(56, y, f"Order Closure {order.wms_order_id}")
    y -= 18
    pdf.setFont("Helvetica", 10)
    for line in (
        f"Client {order.client.client_code}  Division {order.division.code}  Warehouse {order.warehouse.warehouse_code}",
        f"Pick Ticket {ticket.pick_ticket_number if ticket else '—'}  Customer {order.customer or '—'}",
        f"Carrier {order.carrier or '—'}  Service {order.shipping_service or '—'}",
        f"Closed {order.closed_at}  by {order.closed_by_username or '—'}",
    ):
        pdf.drawString(56, y, line)
        y -= 14
    y -= 8
    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(56, y, "Cartons")
    y -= 14
    pdf.setFont("Helvetica", 9)
    for carton in cartons:
        units = CartonContent.query.filter_by(carton_id=carton.id).count()
        pdf.drawString(
            56,
            y,
            f"{carton.carton_number}  {carton.length}x{carton.width}x{carton.height} {carton.dimension_unit}  "
            f"{carton.weight} {carton.weight_unit}  units={units}",
        )
        y -= 12
        if y < 72:
            pdf.showPage()
            y = height - 56
    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(56, y, "Units")
    y -= 14
    pdf.setFont("Helvetica", 9)
    for unit in InventoryUnit.query.filter_by(allocated_order_id=order.id).order_by(InventoryUnit.id):
        pdf.drawString(56, y, f"{unit.upc}  {unit.sku or '—'}  {unit.location}  {unit.status}")
        y -= 11
        if y < 72:
            pdf.showPage()
            y = height - 56
    pdf.save()
    return buffer.getvalue()


def persist_closure_pdf(order: Order) -> Document:
    store = get_store()
    data = render_closure_pdf(order)
    filename = f"{order.wms_order_id}-closure.pdf"
    key = store.save(filename, data, "application/pdf")
    uid, uname = current_actor()
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
    record_audit(
        "PDF_GENERATED",
        module="Processing",
        entity_type="document",
        entity_id=document.id,
        client_id=order.client_id,
        detail=filename,
    )
    return document
