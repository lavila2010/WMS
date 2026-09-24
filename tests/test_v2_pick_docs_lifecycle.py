"""Pick Ticket lifecycle, document presentation, and close-order PDF flow."""

from app.constants import CLOSED_PICK_TICKET_MESSAGE, OrderStatus, PickTicketStatus
from app.models import AuditEvent, Document, InventoryUnit, Order, PickTicket
from app.services.allocation import allocate_order
from app.services.documents import get_store, persist_closure_pdf, render_closure_pdf
from app.services.inventory_ledger import create_available_unit
from app.services.pick_tickets import create_pick_ticket, list_pick_tickets, record_print, render_pdf, ticket_lines
from app.services.processing import (
    ProcessingError,
    acquire_lock,
    close_order,
    ensure_open_carton,
    find_ticket,
    request_close,
    scan_upc,
    set_dimensions,
    set_weight,
)
from tests.conftest import form_data
from tests.pdf_support import extract_pdf_text
from tests.test_v2_phase03_orders import _line
from tests.test_v2_phase04_allocation import _import_order, _world
from tests.test_v2_phase05_pick_tickets import _ready_order
from tests.test_v2_phase06_processing import _processing_order
from tests.test_v2_phase07_reports import _closed

import pytest


def _enrich_units(order, **attrs):
    for unit in InventoryUnit.query.filter_by(allocated_order_id=order.id):
        for key, value in attrs.items():
            setattr(unit, key, value)
    db_session().commit()


def db_session():
    from app.extensions import db

    return db.session


def _pack_close(admin_user, order, upc="UPC-A", weight=4.3):
    acquire_lock(order, admin_user)
    carton = ensure_open_carton(order, admin_user)
    set_dimensions(carton, 12, 12, 12)
    db_session().commit()
    qty = sum(line.qty_ordered for line in order.lines)
    for _ in range(qty):
        scan_upc(order, carton, upc, admin_user)
    request_close(carton, admin_user)
    set_weight(carton, weight, user=admin_user)
    close_order(Order.query.get(order.id), admin_user)
    return Order.query.get(order.id)


def _assert_contains(text: str, *needles):
    lowered = text.lower()
    missing = [n for n in needles if n.lower() not in lowered]
    assert not missing, f"Missing {missing} in PDF text:\n{text}"


def test_a_new_eligible_order_creates_open_ticket(app, db, admin_user):
    _, order = _ready_order(db, admin_user, order_number="3101")
    ticket = create_pick_ticket(order)
    assert ticket.status == PickTicketStatus.OPEN
    assert ticket.pick_ticket_number.endswith("-01")
    assert PickTicket.query.filter_by(order_id=order.id).count() == 1
    assert AuditEvent.query.filter_by(event_type="PICK_TICKET_CREATED").count() >= 1


def test_b_open_ticket_starts_processing(app, db, admin_user):
    _, order = _ready_order(db, admin_user, order_number="3102")
    ticket = create_pick_ticket(order)
    found = find_ticket(ticket.pick_ticket_number)
    assert found.id == ticket.id
    lock = acquire_lock(order, admin_user)
    assert lock
    assert Order.query.get(order.id).status == OrderStatus.PROCESSING
    assert AuditEvent.query.filter_by(event_type="PROCESSING_STARTED").count() >= 1


def test_c_order_close_makes_ticket_closed(app, db, admin_user):
    _, order = _closed(db, admin_user)
    ticket = PickTicket.query.filter_by(order_id=order.id).one()
    assert order.status == OrderStatus.CLOSED
    assert ticket.status == PickTicketStatus.CLOSED
    assert AuditEvent.query.filter_by(event_type="PICK_TICKET_CLOSED").count() >= 1
    assert AuditEvent.query.filter_by(event_type="ORDER_RECONCILED").count() >= 1
    assert AuditEvent.query.filter_by(event_type="ORDER_CLOSED").count() >= 1


def test_d_closed_ticket_cannot_start_processing(app, db, admin_user):
    _, order = _closed(db, admin_user)
    ticket = PickTicket.query.filter_by(order_id=order.id).one()
    with pytest.raises(ProcessingError, match="closed because the associated order is complete"):
        find_ticket(ticket.pick_ticket_number)


def test_e_direct_post_closed_ticket_rejected(app, db, admin_user, admin_client):
    _, order = _closed(db, admin_user)
    ticket = PickTicket.query.filter_by(order_id=order.id).one()
    find_resp = admin_client.post(
        "/processing/find",
        data=form_data(admin_client, {"pick_ticket_number": ticket.pick_ticket_number}),
        follow_redirects=True,
    )
    assert find_resp.status_code == 200
    assert CLOSED_PICK_TICKET_MESSAGE in find_resp.get_data(as_text=True)
    confirm_resp = admin_client.post(
        f"/processing/{order.id}/confirm",
        data=form_data(admin_client),
        follow_redirects=True,
    )
    assert CLOSED_PICK_TICKET_MESSAGE in confirm_resp.get_data(as_text=True)


def test_f_closed_ticket_cannot_acquire_lock(app, db, admin_user):
    _, order = _closed(db, admin_user)
    ticket = PickTicket.query.filter_by(order_id=order.id).one()
    with pytest.raises(ProcessingError, match="closed because the associated order is complete"):
        acquire_lock(order, admin_user)
    assert ticket.pick_ticket_number
    assert Order.query.get(order.id).processing_lock_id is None
    assert Order.query.get(order.id).status == OrderStatus.CLOSED


def test_g_closed_ticket_remains_viewable(app, db, admin_user, admin_client):
    _, order = _closed(db, admin_user)
    ticket = PickTicket.query.filter_by(order_id=order.id).one()
    html = admin_client.get(f"/orders/pick-tickets/{ticket.id}").get_data(as_text=True)
    assert ticket.pick_ticket_number in html
    assert "CLOSED" in html
    assert "Process Order" not in html
    pdf = admin_client.get(f"/orders/pick-tickets/{ticket.id}/pdf")
    assert pdf.status_code == 200
    assert pdf.data[:4] == b"%PDF"


def test_h_i_reprint_permission_keeps_number(app, db, admin_user, admin_client):
    _, order = _closed(db, admin_user)
    ticket = PickTicket.query.filter_by(order_id=order.id).one()
    number = ticket.pick_ticket_number
    resp = admin_client.post(
        f"/orders/pick-tickets/{ticket.id}/print",
        data=form_data(admin_client),
        follow_redirects=True,
    )
    assert resp.status_code == 200
    again = create_pick_ticket(Order.query.get(order.id))
    assert again.pick_ticket_number == number
    assert PickTicket.query.filter_by(order_id=order.id).count() == 1
    assert PickTicket.query.get(ticket.id).print_count >= 1
    assert AuditEvent.query.filter_by(event_type="PICK_TICKET_PRINTED").count() >= 1


def test_j_k_l_m_pick_ticket_filters_and_sort(app, db, admin_user, admin_client):
    w = _world(db)
    create_available_unit(client_id=w["celine"].id, warehouse_id=w["cel_ny"].id, upc="UPC-A", location="A-01")
    create_available_unit(client_id=w["celine"].id, warehouse_id=w["cel_nj"].id, upc="UPC-B", location="B-01")
    db.session.commit()
    order_open = _import_order(
        admin_user, w["celine"], w["cel_ecom"], [_line(order="3201", warehouse="NY", upc="UPC-A", qty=1)]
    )
    order_other_wh = _import_order(
        admin_user, w["celine"], w["cel_ecom"], [_line(order="3202", warehouse="NJ", upc="UPC-B", qty=1)]
    )
    allocate_order(order_open, user=admin_user)
    allocate_order(order_other_wh, user=admin_user)
    open_ticket = create_pick_ticket(Order.query.get(order_open.id))
    nj_ticket = create_pick_ticket(Order.query.get(order_other_wh.id))
    closed_order = _pack_close(admin_user, Order.query.get(order_open.id), upc="UPC-A")
    closed_ticket = PickTicket.query.filter_by(order_id=closed_order.id).one()
    open_nj = PickTicket.query.get(nj_ticket.id)

    default_html = admin_client.get(f"/orders/pick-tickets?client_id={w['celine'].id}").get_data(as_text=True)
    assert open_nj.pick_ticket_number in default_html
    assert closed_ticket.pick_ticket_number not in default_html
    assert 'option value="OPEN" selected' in default_html or "value=\"OPEN\" selected" in default_html
    assert "Process Order" in default_html

    closed_html = admin_client.get(
        f"/orders/pick-tickets?client_id={w['celine'].id}&status=CLOSED"
    ).get_data(as_text=True)
    assert closed_ticket.pick_ticket_number in closed_html
    assert open_nj.pick_ticket_number not in closed_html
    assert "Process Order" not in closed_html
    assert "Reprint" in closed_html

    wh_html = admin_client.get(
        f"/orders/pick-tickets?client_id={w['celine'].id}&warehouse_id={w['cel_nj'].id}&status=ALL"
    ).get_data(as_text=True)
    assert nj_ticket.pick_ticket_number in wh_html
    assert closed_ticket.pick_ticket_number not in wh_html

    div_html = admin_client.get(
        f"/orders/pick-tickets?client_id={w['celine'].id}&division_id={w['cel_ecom'].id}&status=OPEN"
    ).get_data(as_text=True)
    assert open_nj.pick_ticket_number in div_html
    assert closed_ticket.pick_ticket_number not in div_html

    rows = list_pick_tickets(client_id=w["celine"].id, status="ALL", sort="status", direction="asc")
    statuses = [row["status"] for row in rows]
    assert statuses == sorted(statuses)
    unit_rows = list_pick_tickets(client_id=w["celine"].id, status="ALL", sort="units", direction="desc")
    assert [row["units"] for row in unit_rows] == sorted((row["units"] for row in unit_rows), reverse=True)


def test_n_o_p_close_generates_and_opens_pdf(app, db, admin_user, admin_client):
    _, order, ticket, carton = _processing_order(db, admin_user, qty=1)
    scan_upc(order, carton, "UPC-A", admin_user)
    request_close(carton, admin_user)
    set_weight(carton, 4.3, user=admin_user)
    resp = admin_client.post(
        f"/processing/{order.id}/decision",
        data=form_data(admin_client, {"choice": "yes"}),
        follow_redirects=False,
    )
    assert resp.status_code in {302, 303}
    location = resp.headers["Location"]
    assert "pdf_id=" in location
    assert location.endswith("/processing/") or "/processing/?" in location or location.startswith("/processing")
    page = admin_client.get(location)
    html = page.get_data(as_text=True)
    assert "window.open" in html
    assert 'id="pick-ticket"' in html
    assert "Start Order" in html
    document = Document.query.filter_by(order_id=order.id, type="ORDER_CLOSURE").one()
    pdf_resp = admin_client.get(f"/processing/documents/{document.id}")
    assert pdf_resp.data[:4] == b"%PDF"
    assert AuditEvent.query.filter_by(event_type="PDF_GENERATED").count() >= 1
    reused = persist_closure_pdf(Order.query.get(order.id))
    assert reused.id == document.id
    assert Document.query.filter_by(order_id=order.id, type="ORDER_CLOSURE").count() == 1


def test_q_r_pdf_contents(app, db, admin_user):
    w, order = _ready_order(db, admin_user, order_number="3301", qty=1, location="L-09")
    _enrich_units(order, description="Navy Coat", style="COAT", color="NAVY", size="M", sku="SKU-A")
    ticket = create_pick_ticket(Order.query.get(order.id))
    pick_pdf = render_pdf(ticket)
    pick_text = extract_pdf_text(pick_pdf)
    _assert_contains(
        pick_text,
        "WMS SYSTEM",
        "PICK TICKET",
        ticket.pick_ticket_number,
        "Client",
        "Division",
        "Warehouse",
        "WMS Order ID",
        "Client Order Number",
        "Customer",
        "Location",
        "UPC",
        "SKU",
        "Description",
        "Style",
        "Color",
        "Size",
        "Qty",
        "L-09",
        "Navy Coat",
        "COAT",
        "NAVY",
        "M",
    )
    closed_order = _pack_close(admin_user, Order.query.get(order.id), upc="UPC-A")
    document = Document.query.filter_by(order_id=closed_order.id, type="ORDER_CLOSURE").one()
    closure_pdf = get_store().open(document.storage_key)
    closure_text = extract_pdf_text(closure_pdf)
    closed_ticket = PickTicket.query.filter_by(order_id=closed_order.id).one()
    _assert_contains(
        closure_text,
        "WMS SYSTEM",
        "ORDER CLOSURE REPORT",
        closed_order.wms_order_id,
        closed_order.client_order_number,
        closed_ticket.pick_ticket_number,
        "Client",
        "Division",
        "Warehouse",
        "Customer",
        "Carton",
        "Weight",
        "UPC",
        "SKU",
        "Description",
        "Location",
        "SHIPPED",
        "Navy Coat",
        "RECONCILED / PASS",
    )


def test_cancelled_ticket_cannot_process(app, db, admin_user):
    _, order = _ready_order(db, admin_user, order_number="3401")
    ticket = create_pick_ticket(order)
    order.status = OrderStatus.CANCELLED
    ticket.status = PickTicketStatus.CANCELLED
    db.session.commit()
    with pytest.raises(ProcessingError, match="cancelled"):
        find_ticket(ticket.pick_ticket_number)
    rows = list_pick_tickets(client_id=order.client_id, status=PickTicketStatus.CANCELLED)
    assert any(row["ticket"].id == ticket.id for row in rows)
