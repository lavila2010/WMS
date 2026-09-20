import pytest

from app.constants import OrderStatus
from app.models import Order, PickTicket, PickTicketPrintEvent
from app.services.allocation import allocate_order
from app.services.inventory_ledger import create_available_unit
from app.services.pick_tickets import PickTicketError, create_pick_ticket, record_print, ticket_lines
from app.services.tenant import require_entity_client
from tests.conftest import create_user, login
from tests.test_v2_phase03_orders import _line, _xlsx
from tests.test_v2_phase04_allocation import _import_order, _world


def _ready_order(db, admin_user, order_number="1251", upc="UPC-A", qty=2, location="B-02"):
    w = _world(db)
    for _ in range(qty):
        create_available_unit(
            client_id=w["celine"].id,
            warehouse_id=w["cel_ny"].id,
            upc=upc,
            location=location,
            sku="SKU-A",
        )
    db.session.commit()
    order = _import_order(
        admin_user,
        w["celine"],
        w["cel_ecom"],
        [_line(order=order_number, upc=upc, qty=qty)],
    )
    allocate_order(order, user=admin_user)
    return w, Order.query.get(order.id)


def test_p5_01_only_fully_allocated(app, db, admin_user):
    w = _world(db)
    order = _import_order(admin_user, w["celine"], w["cel_ecom"], [_line(qty=1)])
    with pytest.raises(PickTicketError):
        create_pick_ticket(order)


def test_p5_02_reprint_keeps_number(app, db, admin_user):
    _, order = _ready_order(db, admin_user)
    first = create_pick_ticket(order)
    number = first.pick_ticket_number
    again = create_pick_ticket(Order.query.get(order.id))
    record_print(again, source="TEST")
    assert again.pick_ticket_number == number
    assert PickTicket.query.filter_by(order_id=order.id).count() == 1


def test_p5_03_format_sequence_01(app, db, admin_user):
    _, order = _ready_order(db, admin_user)
    ticket = create_pick_ticket(order)
    assert ticket.pick_ticket_number == "01-CEL-1251-01"
    assert ticket.ticket_sequence == 1
    assert Order.query.get(order.id).status == OrderStatus.PICK_TICKET_READY


def test_p5_04_other_client_cannot_open(app, db, admin_user, client):
    w, order = _ready_order(db, admin_user)
    ticket = create_pick_ticket(order)
    create_user("dio", perms=["PICK_TICKET_VIEW", "DASHBOARD_VIEW"], clients=[w["dior"].id])
    login(client, "dio")
    assert client.get(f"/orders/pick-tickets/{ticket.id}").status_code == 404


def test_p5_05_lines_show_location_upc_qty(app, db, admin_user):
    _, order = _ready_order(db, admin_user, qty=3, location="C-09")
    create_pick_ticket(order)
    lines = ticket_lines(order)
    assert lines == [
        {"location": "C-09", "upc": "UPC-A", "sku": "SKU-A", "description": None, "qty": 3}
    ]


def test_p5_06_print_event_and_count(app, db, admin_user):
    _, order = _ready_order(db, admin_user)
    ticket = create_pick_ticket(order)
    record_print(ticket, source="UI")
    record_print(ticket, source="UI")
    assert ticket.print_count == 2
    assert PickTicketPrintEvent.query.filter_by(pick_ticket_id=ticket.id).count() == 2
