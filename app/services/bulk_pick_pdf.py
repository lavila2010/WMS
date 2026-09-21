"""Combined pick-ticket print package. Does not create logical tickets."""

from __future__ import annotations

from datetime import datetime
from io import BytesIO

from reportlab.platypus import PageBreak, Spacer

from ..auth import current_actor, record_audit
from ..extensions import db
from ..models import Document, Order, PickTicket, PickTicketPrintBatch, PickTicketPrintBatchItem
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
from .documents import get_store
from .pick_tickets import record_print, render_pdf, ticket_lines, ticket_summary


class BulkPickPdfError(ValueError):
    pass


def sort_tickets(tickets: list[PickTicket], sort: str, direction: str) -> list[PickTicket]:
    descending = (direction or "asc").lower() == "desc"

    def key(ticket: PickTicket):
        order = ticket.order or db.session.get(Order, ticket.order_id)
        mapping = {
            "ticket": ticket.pick_ticket_number,
            "wms_order": order.wms_order_id if order else "",
            "customer": (order.customer or "") if order else "",
            "created": ticket.created_at or datetime.min,
            "client": (
                order.client.client_code if order and order.client else "",
                order.division.code if order and order.division else "",
                order.warehouse.warehouse_code if order and order.warehouse else "",
                ticket.pick_ticket_number,
            ),
            "division": (
                order.division.code if order and order.division else "",
                ticket.pick_ticket_number,
            ),
            "warehouse": (
                order.warehouse.warehouse_code if order and order.warehouse else "",
                ticket.pick_ticket_number,
            ),
        }
        return mapping.get(sort, mapping["client"])

    return sorted(tickets, key=key, reverse=descending)


def render_bulk_pdf(tickets: list[PickTicket], *, sort="client", direction="asc") -> bytes:
    if not tickets:
        raise BulkPickPdfError("Select at least one pick ticket.")
    ordered = sort_tickets(tickets, sort, direction)
    rendered = []
    summaries = []
    total_units = 0
    for ticket in ordered:
        try:
            data = render_pdf(ticket)
        except Exception as exc:  # noqa: BLE001
            raise BulkPickPdfError(f"Pick ticket {ticket.pick_ticket_number} could not be rendered.") from exc
        if not data.startswith(b"%PDF"):
            raise BulkPickPdfError(f"Pick ticket {ticket.pick_ticket_number} could not be rendered.")
        order = ticket.order or db.session.get(Order, ticket.order_id)
        lines = ticket_lines(order, ticket)
        summary = ticket_summary(order, lines)
        total_units += summary["total_units"]
        summaries.append((ticket, order, summary))
        rendered.append(data)
    cover = _cover_pdf(summaries, total_units)
    return _merge_pdfs([cover, *rendered])


def publish_bulk_pdf(tickets: list[PickTicket], *, sort="client", direction="asc", filter_context="") -> dict:
    print_counts = {ticket.id: ticket.print_count for ticket in tickets}
    data = render_bulk_pdf(tickets, sort=sort, direction=direction)
    uid, uname = current_actor()
    store = get_store()
    filename = f"bulk-pick-tickets-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.pdf"
    key = store.save(filename, data, "application/pdf")
    document = Document(
        client_id=tickets[0].client_id,
        type="BULK_PICK_TICKET_PACKAGE",
        filename=filename,
        storage_key=key,
        created_by_user_id=uid,
        created_by_username=uname,
    )
    db.session.add(document)
    db.session.flush()
    units = 0
    for ticket in sort_tickets(tickets, sort, direction):
        order = ticket.order or db.session.get(Order, ticket.order_id)
        units += ticket_summary(order, ticket_lines(order, ticket))["total_units"]
    batch = PickTicketPrintBatch(
        created_by_user_id=uid,
        ticket_count=len(tickets),
        total_units=units,
        sort_order=f"{sort}:{direction}",
        filter_context=(filter_context or "")[:512],
        document_id=document.id,
    )
    db.session.add(batch)
    db.session.flush()
    for index, ticket in enumerate(sort_tickets(tickets, sort, direction), start=1):
        db.session.add(
            PickTicketPrintBatchItem(batch_id=batch.id, pick_ticket_id=ticket.id, sequence_in_batch=index)
        )
        event_name = "PICK_TICKET_PRINTED" if (print_counts.get(ticket.id) or 0) == 0 else "PICK_TICKET_REPRINTED"
        record_print(ticket, source="BULK")
        record_audit(
            event_name,
            module="Orders",
            entity_type="pick_ticket",
            entity_id=ticket.id,
            client_id=ticket.client_id,
            detail=f"{ticket.pick_ticket_number} batch={batch.id}",
        )
    record_audit(
        "PICK_TICKET_BULK_PRINT_CREATED",
        module="Orders",
        entity_type="pick_ticket_print_batch",
        entity_id=batch.id,
        client_id=tickets[0].client_id,
        detail=f"tickets={len(tickets)} units={units}",
    )
    db.session.commit()
    return {"pdf": data, "batch": batch, "document": document, "filename": filename}


def _cover_pdf(summaries, total_units: int) -> bytes:
    uid, uname = current_actor()
    s = pdf_styles()
    generated = generated_now()
    story = [
        header_block(s, title="BULK PICK TICKET PACKAGE", ident="WMS SYSTEM", status="PRINT PACKAGE"),
        Spacer(1, 10),
        section_title(s, "PACKAGE"),
        Spacer(1, 4),
        kv_table(
            s,
            [
                ("Generated", format_ts(generated)),
                ("Generated By", uname or "SYSTEM"),
                ("Number of Pick Tickets", len(summaries)),
                ("Total Units", total_units),
            ],
        ),
        Spacer(1, 10),
        section_title(s, "SUMMARY"),
        Spacer(1, 4),
        data_table(
            s,
            ["Pick Ticket #", "Client", "Division", "Warehouse", "WMS Order ID", "Customer", "Units", "Status"],
            [
                [
                    ticket.pick_ticket_number,
                    order.client.client_code if order.client else "",
                    order.division.code if order.division else "",
                    order.warehouse.warehouse_code if order.warehouse else "",
                    order.wms_order_id,
                    order.customer,
                    summary["total_units"],
                    ticket.status,
                ]
                for ticket, order, summary in summaries
            ],
        ),
    ]
    return build_pdf(
        story,
        footer={"left": "WMS SYSTEM  ·  BULK PICK TICKET PACKAGE", "generated": f"Generated {format_ts(generated)}", "title": "PRINT PACKAGE"},
    )


def _merge_pdfs(blobs: list[bytes]) -> bytes:
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:  # pragma: no cover
        from PyPDF2 import PdfReader, PdfWriter
    writer = PdfWriter()
    for blob in blobs:
        reader = PdfReader(BytesIO(blob))
        for page in reader.pages:
            writer.add_page(page)
    out = BytesIO()
    writer.write(out)
    return out.getvalue()
