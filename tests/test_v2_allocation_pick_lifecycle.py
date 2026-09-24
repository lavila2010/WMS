"""Partial pick-ticket eligibility and Allocation action-queue lifecycle."""

from __future__ import annotations

from app.constants import AllocationStatus, OrderStatus, PickTicketStatus, UnitStatus
from app.extensions import db
from app.models import Allocation, InventoryUnit, Order, PickTicket
from app.schema import ensure_v2_schema
from app.services.allocation import allocate_order, approve_partial_allocation
from app.services.allocation_exceptions import list_daily_exceptions
from app.services.fulfillment import (
    allocation_page_visible,
    order_quantities,
    pick_ticket_eligible,
    pick_ticket_handoff_diagnostic,
    repair_zero_current_wave_numbers,
)
from app.services.pick_tickets import create_pick_ticket, list_eligible_pick_ticket_orders, ticket_lines
from app.services.processing import acquire_lock
from tests.conftest import create_user, login
from tests.test_v2_allocation_pick_control import _order, _pack_ticket, _stock, _world
from tests.test_v2_phase05_pick_tickets import _ready_order


def _partial_seven_of_ten(admin_user, db, number=9101):
    w = _world(db)
    _stock(w["celine"], w["cel_ny"], "UPC-A", 10)
    order = _order(admin_user, w, number, 10)
    for unit in InventoryUnit.query.filter_by(upc="UPC-A").limit(3):
        unit.status = UnitStatus.SHIPPED
    db.session.commit()
    allocate_order(Order.query.get(order.id), user=admin_user)
    return w, Order.query.get(order.id)


def _legacy_zero_wave(order):
    order.current_wave_number = 0
    db.session.commit()
    db.session.refresh(order)
    return order


def _eligible_ids(w):
    return {row["order"].id for row in list_eligible_pick_ticket_orders(client_id=w["celine"].id)}


def _alloc_html(admin_client):
    return admin_client.get("/allocation/").get_data(as_text=True)


def test_01_legacy_zero_wave_normalizes_on_approve(app, db, admin_user):
    _w, order = _partial_seven_of_ten(admin_user, db, 9101)
    _legacy_zero_wave(order)
    order.partial_approved_wave = 1
    db.session.commit()
    before = pick_ticket_handoff_diagnostic(Order.query.get(order.id))
    print("DIAGNOSTIC_LEGACY_BEFORE", before)
    assert before["current_wave_number"] == 0
    assert before["partial_approved_wave"] == 1
    assert before["pick_ticket_eligible"] is False
    order = Order.query.get(order.id)
    order.partial_approved_wave = None
    db.session.commit()
    approve_partial_allocation(Order.query.get(order.id), user=admin_user)
    db.session.expire_all()
    order = Order.query.get(order.id)
    after = pick_ticket_handoff_diagnostic(order)
    print("DIAGNOSTIC_LEGACY_AFTER", after)
    assert order.current_wave_number == 1
    assert order.partial_approved_wave == 1
    assert after["current_wave_unticketed_count"] == 7
    assert after["pick_ticket_eligible"] is True
    assert after["allocation_page_visible"] is True


def test_01b_repair_zero_wave_single_unticketed_wave(app, db, admin_user):
    _w, order = _partial_seven_of_ten(admin_user, db, 9102)
    _legacy_zero_wave(order)
    report = repair_zero_current_wave_numbers()
    assert report["repaired"] >= 1
    db.session.expire_all()
    order = Order.query.get(order.id)
    assert order.current_wave_number == 1
    assert Allocation.query.filter_by(order_id=order.id).count() == 7
    assert {a.wave_number for a in Allocation.query.filter_by(order_id=order.id)} == {1}
    again = repair_zero_current_wave_numbers()
    assert again["repaired"] == 0


def test_01c_repair_skips_conflicting_unticketed_waves(app, db, admin_user):
    _w, order = _partial_seven_of_ten(admin_user, db, 9103)
    first = Allocation.query.filter_by(order_id=order.id).order_by(Allocation.id).first()
    first.wave_number = 2
    _legacy_zero_wave(order)
    report = repair_zero_current_wave_numbers()
    db.session.expire_all()
    order = Order.query.get(order.id)
    assert order.current_wave_number == 0
    assert any(row["wms_order_id"] == order.wms_order_id for row in report["skipped_conflicts"])
    assert Allocation.query.filter_by(id=first.id).one().wave_number == 2


def test_02_approved_partial_on_pick_ticket_list(app, db, admin_user, admin_client):
    w, order = _partial_seven_of_ten(admin_user, db, 9104)
    approve_partial_allocation(order, user=admin_user)
    rows = list_eligible_pick_ticket_orders(client_id=w["celine"].id)
    match = [row for row in rows if row["order"].id == order.id]
    assert match
    assert match[0]["allocation_type"] == "PARTIAL APPROVED"
    assert match[0]["allocated_current_wave"] == 7
    assert match[0]["remaining"] == 3
    html = admin_client.get(f"/orders/pick-tickets?client_id={w['celine'].id}").get_data(as_text=True)
    assert order.wms_order_id in html
    assert "PARTIAL APPROVED" in html
    assert "Create Pick Ticket" in html


def test_03_04_ticket_qty_seven_remaining_three(app, db, admin_user):
    _w, order = _partial_seven_of_ten(admin_user, db, 9105)
    approve_partial_allocation(order, user=admin_user)
    ticket = create_pick_ticket(Order.query.get(order.id))
    lines = ticket_lines(Order.query.get(order.id), ticket)
    assert sum(line["qty"] for line in lines) == 7
    qty = order_quantities(Order.query.get(order.id))
    assert qty["remaining"] == 3
    assert qty["currently_allocated"] == 7
    assert Allocation.query.filter_by(pick_ticket_id=ticket.id, status=AllocationStatus.ACTIVE).count() == 7


def test_05_06_07_hide_while_open_or_processing(app, db, admin_user, admin_client):
    w, order = _partial_seven_of_ten(admin_user, db, 9106)
    html = _alloc_html(admin_client)
    assert order.wms_order_id in html
    assert "Approve Partial" in html
    approve_partial_allocation(order, user=admin_user)
    html = _alloc_html(admin_client)
    assert order.wms_order_id in html
    assert "Ready for Pick Ticket" in html
    ticket = create_pick_ticket(Order.query.get(order.id))
    db.session.expire_all()
    order = Order.query.get(order.id)
    snap = pick_ticket_handoff_diagnostic(order)
    assert snap["open_pick_ticket_count"] >= 1
    assert snap["allocation_page_visible"] is False
    assert allocation_page_visible(order) is False
    html = _alloc_html(admin_client)
    assert order.wms_order_id not in html
    acquire_lock(order, admin_user)
    order = Order.query.get(order.id)
    assert order.status == OrderStatus.PROCESSING
    assert allocation_page_visible(order) is False
    html = _alloc_html(admin_client)
    assert order.wms_order_id not in html
    assert PickTicket.query.get(ticket.id).status == PickTicketStatus.OPEN


def test_08_reappear_after_partial_ticket_closes(app, db, admin_user, admin_client):
    _w, order = _partial_seven_of_ten(admin_user, db, 9107)
    approve_partial_allocation(order, user=admin_user)
    create_pick_ticket(Order.query.get(order.id))
    _pack_ticket(admin_user, Order.query.get(order.id), qty=7)
    order = Order.query.get(order.id)
    qty = order_quantities(order)
    assert qty["remaining"] == 3
    assert qty["shipped"] == 7
    assert order.status == OrderStatus.PARTIALLY_FULFILLED
    assert PickTicket.query.filter_by(order_id=order.id).one().status == PickTicketStatus.CLOSED
    assert allocation_page_visible(order) is True
    html = _alloc_html(admin_client)
    assert order.wms_order_id in html
    assert "Allocate" in html


def test_09_full_allocation_hides_after_ticket(app, db, admin_user, admin_client):
    w, order = _ready_order(db, admin_user, order_number="9108", qty=2)
    assert pick_ticket_eligible(order) is True
    html = _alloc_html(admin_client)
    assert order.wms_order_id in html
    assert "Ready for Pick Ticket" in html
    create_pick_ticket(order)
    html = _alloc_html(admin_client)
    assert order.wms_order_id not in html
    assert allocation_page_visible(Order.query.get(order.id)) is False


def test_10_daily_exceptions_still_shows_ticketed_partial(app, db, admin_user):
    _w, order = _partial_seven_of_ten(admin_user, db, 9109)
    approve_partial_allocation(order, user=admin_user)
    create_pick_ticket(Order.query.get(order.id))
    assert allocation_page_visible(Order.query.get(order.id)) is False
    payload = list_daily_exceptions(admin=True)
    match = [row for row in payload["rows"] if row["order"].id == order.id]
    assert match
    assert match[0]["remaining"] == 3


def test_11_future_wave_not_on_previous_ticket(app, db, admin_user):
    _w, order = _partial_seven_of_ten(admin_user, db, 9110)
    approve_partial_allocation(order, user=admin_user)
    ticket1 = create_pick_ticket(Order.query.get(order.id))
    for unit in InventoryUnit.query.filter_by(upc="UPC-A", status=UnitStatus.SHIPPED):
        if unit.allocated_order_id is None:
            unit.status = UnitStatus.AVAILABLE
    db.session.commit()
    allocate_order(Order.query.get(order.id), user=admin_user)
    order = Order.query.get(order.id)
    assert order.current_wave_number == 2
    wave2 = Allocation.query.filter_by(order_id=order.id, wave_number=2, status=AllocationStatus.ACTIVE).all()
    assert len(wave2) == 3
    assert all(row.pick_ticket_id is None for row in wave2)
    assert Allocation.query.filter_by(pick_ticket_id=ticket1.id).count() == 7


def test_12_full_allocation_workflow_unchanged(app, db, admin_user):
    _w, order = _ready_order(db, admin_user, order_number="9111", qty=2)
    assert order.status == OrderStatus.ALLOCATED
    assert pick_ticket_eligible(order) is True
    ticket = create_pick_ticket(order)
    assert sum(line["qty"] for line in ticket_lines(Order.query.get(order.id), ticket)) == 2
    qty = order_quantities(Order.query.get(order.id))
    assert qty["remaining"] == 0


def test_13_tenant_isolation_unchanged(app, db, admin_user, client):
    w, order = _partial_seven_of_ten(admin_user, db, 9112)
    create_user("dio-life", perms=["ALLOCATION_VIEW", "PICK_TICKET_VIEW"], clients=[w["dior"].id])
    login(client, "dio-life")
    html = client.get("/allocation/").get_data(as_text=True)
    assert order.wms_order_id not in html
    assert client.get(f"/allocation/{order.id}").status_code == 404


def test_repair_does_not_touch_positive_current_wave(app, db, admin_user):
    _w, order = _partial_seven_of_ten(admin_user, db, 9113)
    assert order.current_wave_number == 1
    report = repair_zero_current_wave_numbers()
    assert order.wms_order_id not in report.get("repaired_ids", [])
    db.session.expire_all()
    assert Order.query.get(order.id).current_wave_number == 1


def test_ensure_v2_schema_runs_repair(app, db, admin_user):
    _w, order = _partial_seven_of_ten(admin_user, db, 9114)
    order_id = order.id
    _legacy_zero_wave(order)
    db.session.commit()
    db.session.remove()
    ensure_v2_schema()
    restored = db.session.get(Order, order_id)
    assert restored is not None
    assert restored.current_wave_number == 1
