"""Partial allocation approval must land on Orders → Pick Tickets as eligible."""

from __future__ import annotations

from app.constants import AllocationStatus, OrderStatus, UnitStatus
from app.extensions import db
from app.models import Allocation, InventoryUnit, Order, PickTicket
from app.services.allocation import allocate_order, approve_partial_allocation
from app.services.fulfillment import (
    order_quantities,
    pick_ticket_eligible,
    pick_ticket_handoff_diagnostic,
)
from app.services.pick_tickets import create_pick_ticket, list_eligible_pick_ticket_orders, ticket_lines
from tests.conftest import form_data
from tests.test_v2_allocation_pick_control import _order, _stock, _world
from tests.test_v2_phase05_pick_tickets import _ready_order


def _partial_seven_of_ten(admin_user, db):
    w = _world(db)
    _stock(w["celine"], w["cel_ny"], "UPC-A", 10)
    order = _order(admin_user, w, 7101, 10)
    for unit in InventoryUnit.query.filter_by(upc="UPC-A").limit(3):
        unit.status = UnitStatus.SHIPPED
    db.session.commit()
    allocate_order(Order.query.get(order.id), user=admin_user)
    return w, Order.query.get(order.id)


def _eligible_ids(w, *, division_id=None, warehouse_id=None):
    rows = list_eligible_pick_ticket_orders(
        client_id=w["celine"].id,
        division_id=division_id,
        warehouse_id=warehouse_id,
    )
    return {row["order"].id for row in rows}


def _pick_tickets_html(admin_client, w, *, division=False, warehouse=False):
    args = f"client_id={w['celine'].id}"
    if division:
        args += f"&division_id={w['cel_ecom'].id}"
    if warehouse:
        args += f"&warehouse_id={w['cel_ny'].id}"
    return admin_client.get(f"/orders/pick-tickets?{args}").get_data(as_text=True)


def test_a_unapproved_partial_not_on_pick_tickets(app, db, admin_user, admin_client):
    w, order = _partial_seven_of_ten(admin_user, db)
    snap = pick_ticket_handoff_diagnostic(order)
    print("DIAGNOSTIC_BEFORE_APPROVAL", snap)
    assert snap["currently_allocated"] == 7
    assert snap["remaining"] == 3
    assert snap["pick_ticket_eligible"] is False
    assert order.id not in _eligible_ids(w)
    html = _pick_tickets_html(admin_client, w)
    assert order.wms_order_id not in html
    assert "Eligible Orders for Pick Ticket" in html
    assert "No fully allocated or approved partial orders are waiting for a Pick Ticket." in html


def test_b_f_approved_partial_appears_and_survives_reload(app, db, admin_user, admin_client):
    w, order = _partial_seven_of_ten(admin_user, db)
    resp = admin_client.post(
        f"/allocation/{order.id}/approve-partial",
        data=form_data(admin_client, {}),
        follow_redirects=True,
    )
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert (
        "Partial allocation approved. Pick Ticket can now be generated for the current allocated units."
        in html
    )
    assert "Go to Pick Tickets" in html or order.wms_order_id in html
    db.session.expire_all()
    order = Order.query.get(order.id)
    snap = pick_ticket_handoff_diagnostic(order)
    print("DIAGNOSTIC_AFTER_APPROVAL", snap)
    assert snap["ordered"] == 10
    assert snap["shipped"] == 0
    assert snap["currently_allocated"] == 7
    assert snap["remaining"] == 3
    assert snap["current_wave_number"] == 1
    assert snap["partial_approved_wave"] == 1
    assert snap["unticketed_allocation_count"] == 7
    assert snap["pick_ticket_eligible"] is True
    assert order.status == OrderStatus.PARTIALLY_ALLOCATED
    assert order.id in _eligible_ids(w)
    first = _pick_tickets_html(admin_client, w)
    assert order.wms_order_id in first
    assert "PARTIAL APPROVED" in first
    assert "Create Pick Ticket" in first
    reload_html = _pick_tickets_html(admin_client, w)
    assert order.wms_order_id in reload_html
    assert Order.query.get(order.id).partial_approved_wave == 1
    assert Allocation.query.filter_by(
        order_id=order.id, status=AllocationStatus.ACTIVE, pick_ticket_id=None
    ).count() == 7


def test_c_d_e_ticket_only_current_wave_units(app, db, admin_user, admin_client):
    w, order = _partial_seven_of_ten(admin_user, db)
    approve_partial_allocation(order, user=admin_user)
    resp = admin_client.post(
        f"/orders/{order.id}/pick-ticket",
        data=form_data(admin_client, {"client_id": str(w["celine"].id)}),
        follow_redirects=True,
    )
    assert resp.status_code == 200
    ticket = PickTicket.query.filter_by(order_id=order.id).one()
    lines = ticket_lines(Order.query.get(order.id), ticket)
    assert sum(line["qty"] for line in lines) == 7
    qty = order_quantities(Order.query.get(order.id))
    assert qty["remaining"] == 3
    assert qty["currently_allocated"] == 7
    assert Allocation.query.filter_by(pick_ticket_id=ticket.id).count() == 7
    assert (
        Allocation.query.filter_by(
            order_id=order.id, status=AllocationStatus.ACTIVE, pick_ticket_id=None
        ).count()
        == 0
    )
    assert Order.query.get(order.id).partial_approved_wave == 1


def test_g_h_i_scope_filters_keep_valid_partial(app, db, admin_user, admin_client):
    w, order = _partial_seven_of_ten(admin_user, db)
    approve_partial_allocation(order, user=admin_user)
    assert order.id in _eligible_ids(w, division_id=w["cel_ecom"].id, warehouse_id=w["cel_ny"].id)
    html = _pick_tickets_html(admin_client, w, division=True, warehouse=True)
    assert order.wms_order_id in html
    assert order.id not in _eligible_ids(w, warehouse_id=w["cel_nj"].id)
    other_div_html = admin_client.get(
        f"/orders/pick-tickets?client_id={w['celine'].id}&division_id={w['cel_ecom'].id}"
    ).get_data(as_text=True)
    assert order.wms_order_id in other_div_html


def test_j_fully_allocated_still_eligible(app, db, admin_user, admin_client):
    w, order = _ready_order(db, admin_user, order_number="7102", qty=2)
    assert pick_ticket_eligible(order) is True
    html = _pick_tickets_html(admin_client, w)
    assert order.wms_order_id in html
    assert "FULL" in html


def test_k_second_wave_still_possible(app, db, admin_user):
    w, order = _partial_seven_of_ten(admin_user, db)
    approve_partial_allocation(order, user=admin_user)
    create_pick_ticket(Order.query.get(order.id))
    wave_before = Order.query.get(order.id).current_wave_number
    for unit in InventoryUnit.query.filter_by(upc="UPC-A", status=UnitStatus.SHIPPED).limit(3):
        if unit.allocated_order_id is None:
            unit.status = UnitStatus.AVAILABLE
    db.session.commit()
    result = allocate_order(Order.query.get(order.id), user=admin_user)
    order = Order.query.get(order.id)
    assert result["reserved"] == 3
    assert order.current_wave_number == wave_before + 1
    assert pick_ticket_eligible(order) is True
    ticket2 = create_pick_ticket(order)
    assert ticket2.ticket_sequence == 2
    assert sum(line["qty"] for line in ticket_lines(order, ticket2)) == 3


def test_l_closed_cancelled_excluded(app, db, admin_user):
    w, order = _partial_seven_of_ten(admin_user, db)
    approve_partial_allocation(order, user=admin_user)
    order.status = OrderStatus.CLOSED
    db.session.commit()
    assert order.id not in _eligible_ids(w)
    cancelled = _order(admin_user, w, 7103, 1)
    cancelled.status = OrderStatus.CANCELLED
    db.session.commit()
    assert cancelled.id not in _eligible_ids(w)


def test_approval_does_not_mutate_wave_or_allocations(app, db, admin_user):
    _w, order = _partial_seven_of_ten(admin_user, db)
    wave = order.current_wave_number
    alloc_ids = [a.id for a in Allocation.query.filter_by(order_id=order.id).order_by(Allocation.id)]
    approve_partial_allocation(order, user=admin_user)
    db.session.expire_all()
    order = Order.query.get(order.id)
    assert order.current_wave_number == wave
    assert order.partial_approved_wave == wave
    after = Allocation.query.filter_by(order_id=order.id).order_by(Allocation.id).all()
    assert [a.id for a in after] == alloc_ids
    assert all(a.status == AllocationStatus.ACTIVE and a.pick_ticket_id is None for a in after)


def test_approval_http_lands_on_filtered_pick_tickets(app, db, admin_user, admin_client):
    w, order = _partial_seven_of_ten(admin_user, db)
    resp = admin_client.post(
        f"/allocation/{order.id}/approve-partial",
        data=form_data(admin_client, {}),
        follow_redirects=False,
    )
    assert resp.status_code == 302
    location = resp.headers["Location"]
    assert "/orders/pick-tickets" in location
    assert f"client_id={order.client_id}" in location
    assert f"division_id={order.division_id}" in location
    assert f"warehouse_id={order.warehouse_id}" in location
    landed = admin_client.get(location)
    body = landed.get_data(as_text=True)
    assert order.wms_order_id in body
    assert "PARTIAL APPROVED" in body
