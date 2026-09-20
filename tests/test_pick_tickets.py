"""Pick ticket numbering, eligibility, reprint, filters, and print history."""

from datetime import datetime

from app.constants import OrderStatus, PrintSource
from app.models import Document, PickTicket, PickTicketPrintEvent
from app.services.allocation import allocate_barcode, mark_allocated
from app.services.documents import generate_pick_ticket
from app.services.pick_tickets import (
    ensure_pick_ticket,
    is_eligible,
    pick_lines,
    record_print,
)
from app.workflow import transition
from tests.conftest import create_user, login, make_order, make_unit


def _fully_allocate(db, order_number="SO-PT", barcode="PT-1", sku="SKU-A"):
    order = make_order(order_number, lines=[(sku, 1)])
    transition(order, OrderStatus.VALIDATED)
    db.session.commit()
    make_unit(barcode, sku, location="B02-01")
    allocate_barcode(order, barcode)
    db.session.commit()
    mark_allocated(order)
    db.session.commit()
    return order


def test_pick_ticket_assigned_on_full_allocation(db):
    order = _fully_allocate(db)
    ticket = order.pick_ticket
    assert ticket is not None
    assert ticket.pick_ticket_number.startswith("PT-")
    assert PickTicket.query.filter_by(order_id=order.id).count() == 1


def test_one_pick_ticket_per_order_and_unique_number(db):
    a = _fully_allocate(db, "SO-PT-A", "PT-A")
    b = _fully_allocate(db, "SO-PT-B", "PT-B")
    first = ensure_pick_ticket(a)
    again = ensure_pick_ticket(a)
    assert first.id == again.id
    assert first.pick_ticket_number == again.pick_ticket_number
    assert a.pick_ticket.pick_ticket_number != b.pick_ticket.pick_ticket_number
    assert PickTicket.query.filter_by(pick_ticket_number=first.pick_ticket_number).count() == 1


def test_reprint_keeps_same_pick_ticket_number(db):
    order = _fully_allocate(db)
    ticket = order.pick_ticket
    number = ticket.pick_ticket_number
    doc1 = generate_pick_ticket(order, pick_ticket=ticket)
    ticket.document_id = doc1.id
    db.session.commit()
    doc2 = generate_pick_ticket(order, pick_ticket=ticket)
    ticket.document_id = doc2.id
    db.session.commit()
    assert order.pick_ticket.pick_ticket_number == number
    assert PickTicket.query.filter_by(order_id=order.id).count() == 1


def test_partial_and_closed_orders_not_eligible(db):
    partial = make_order("SO-PART", lines=[("SKU-A", 2)])
    transition(partial, OrderStatus.VALIDATED)
    db.session.commit()
    make_unit("PART-1", "SKU-A")
    allocate_barcode(partial, "PART-1")
    db.session.commit()
    assert is_eligible(partial) is False

    closed = _fully_allocate(db, "SO-CL", "CL-1")
    closed.status = OrderStatus.CLOSED
    db.session.commit()
    assert is_eligible(closed) is False


def test_pick_lines_show_barcode_location_sorted(db):
    order = make_order("SO-SORT", lines=[("SKU-A", 2)])
    transition(order, OrderStatus.VALIDATED)
    db.session.commit()
    make_unit("ZZ-9", "SKU-A", location="C-03")
    make_unit("AA-1", "SKU-A", location="A-01")
    allocate_barcode(order, "ZZ-9")
    allocate_barcode(order, "AA-1")
    db.session.commit()
    mark_allocated(order)
    db.session.commit()
    lines = pick_lines(order)
    assert [row["barcode"] for row in lines] == ["AA-1", "ZZ-9"]
    assert lines[0]["location"] == "A-01"
    assert lines[1]["location"] == "C-03"
    assert all(row["qty"] == 1 for row in lines)


def test_print_history_records_user_and_timestamp(admin_client, db, admin_user):
    order = _fully_allocate(db)
    ticket = order.pick_ticket
    ev = record_print(ticket, PrintSource.SCREEN)
    db.session.commit()
    assert ev.username == "admin"
    assert ev.user_id == admin_user.id
    assert ev.source == PrintSource.SCREEN
    assert isinstance(ev.printed_at, datetime)
    assert PickTicketPrintEvent.query.filter_by(pick_ticket_id=ticket.id).count() == 1


def test_pick_ticket_filters_never_printed_printed_and_printed_by(admin_client, db):
    never = _fully_allocate(db, "SO-NV", "NV-1")
    printed = _fully_allocate(db, "SO-PR", "PR-1")
    record_print(printed.pick_ticket, PrintSource.SCREEN)
    db.session.commit()
    client_id = never.client_id
    html = admin_client.get(
        f"/orders/pick-tickets?client_id={client_id}&print_status=never"
    ).get_data(as_text=True)
    assert never.pick_ticket.pick_ticket_number in html
    assert printed.pick_ticket.pick_ticket_number not in html
    html = admin_client.get(
        f"/orders/pick-tickets?client_id={client_id}&print_status=printed"
    ).get_data(as_text=True)
    assert printed.pick_ticket.pick_ticket_number in html
    assert never.pick_ticket.pick_ticket_number not in html
    html = admin_client.get(
        f"/orders/pick-tickets?client_id={client_id}&print_status=printed&printed_by=admin"
    ).get_data(as_text=True)
    assert printed.pick_ticket.pick_ticket_number in html
    html = admin_client.get(
        f"/orders/pick-tickets?client_id={client_id}&print_status=printed&printed_by=nobody"
    ).get_data(as_text=True)
    assert printed.pick_ticket.pick_ticket_number not in html


def test_generate_creates_one_pdf_per_order(admin_client, db):
    a = _fully_allocate(db, "SO-PDF-A", "PDF-A")
    b = _fully_allocate(db, "SO-PDF-B", "PDF-B")
    resp = admin_client.post(
        "/orders/pick-tickets/generate",
        data={"ticket_id": [str(a.pick_ticket.id), str(b.pick_ticket.id)]},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    docs = Document.query.filter_by(type="PICK_TICKET").all()
    assert len(docs) == 2
    assert {d.order_id for d in docs} == {a.id, b.id}


def test_regular_user_cannot_generate_pick_ticket_by_default(app, db):
    create_user("picker", role="USER")
    order = _fully_allocate(db, "SO-NG", "NG-1")
    c = app.test_client()
    login(c, "picker")
    resp = c.post("/orders/pick-tickets/generate", data={"ticket_id": [str(order.pick_ticket.id)]})
    assert resp.status_code == 403


def test_pick_ticket_pdf_contains_header_bytes(db):
    order = _fully_allocate(db, "SO-PDFH", "PDFH-1")
    doc = generate_pick_ticket(order, pick_ticket=order.pick_ticket)
    with open(doc.path, "rb") as fh:
        data = fh.read()
    assert data[:5] == b"%PDF-"
    # Text may be compressed; still a valid PDF document record.
    assert doc.type == "PICK_TICKET"
