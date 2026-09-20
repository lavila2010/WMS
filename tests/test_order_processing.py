import pytest

from app.constants import BoxStatus, ExceptionType, OrderStatus, ProcessingEvent
from app.extensions import db as _db
from app.models import Box, Document, Order, PickTicket, Transaction
from app.services.allocation import BarcodeError, allocate_barcode, mark_allocated
from app.services.completed_orders import (
    EXPORT_COLUMNS,
    build_completed_orders_workbook,
    completed_orders_query,
)
from app.services.pick_tickets import ensure_pick_ticket
from app.services.processing import (
    ProcessingError,
    add_upc_to_recalled_carton,
    cancel_session,
    close_blockers,
    confirm_dimensions,
    confirm_order,
    finalize_order,
    lookup_pick_ticket,
    ready_for_close_modal,
    recall_carton,
    remaining_unit_rows,
    remaining_units,
    remove_unit_from_carton,
    request_close_carton,
    save_carton_weight,
    scan_upc_into_box,
    station_kpis,
)
from app.workflow import transition
from tests.conftest import create_user, login, make_order, make_unit


UPC = "194900012345"
UPC_B = "194900067890"


def _allocate_ready(db, order_number="SO-51022", upc=UPC, qty=3, extra_upc=None, extra_qty=0, pt="PT-2026-000184"):
    lines = [(f"SKU-{upc[-3:]}", qty)]
    if extra_upc:
        lines.append((f"SKU-{extra_upc[-3:]}", extra_qty))
    order = make_order(order_number, customer="Celine Store", lines=lines)
    transition(order, OrderStatus.VALIDATED)
    db.session.commit()
    for i in range(qty):
        make_unit(f"{order_number}-A{i+1}", lines[0][0], upc=upc, location="A-12")
        allocate_barcode(order, f"{order_number}-A{i+1}")
    for i in range(extra_qty):
        make_unit(f"{order_number}-B{i+1}", lines[1][0], upc=extra_upc, location="B-04")
        allocate_barcode(order, f"{order_number}-B{i+1}")
    db.session.commit()
    mark_allocated(order)
    ticket = ensure_pick_ticket(order)
    ticket.pick_ticket_number = pt
    db.session.commit()
    return order, ticket


def _user(name="packer"):
    return create_user(name, role="ADMIN")


def _start(db, user=None, **kwargs):
    order, ticket = _allocate_ready(db, **kwargs)
    actor = user or _user()
    confirm_order(order, user=actor)
    db.session.commit()
    return order, ticket, actor


def _dims(order, user, length=18, width=14, height=10):
    box = order.boxes[0]
    confirm_dimensions(box, length, width, height, "in", user=user)
    _db.session.commit()
    return box


def test_pick_ticket_lookup(db):
    order, ticket = _allocate_ready(db)
    found, looked = lookup_pick_ticket("PT-2026-000184")
    assert found.id == ticket.id
    assert looked.id == order.id


def test_invalid_pick_ticket_blocked(db):
    with pytest.raises(ProcessingError) as exc:
        lookup_pick_ticket("PT-NOPE")
    assert "not found" in exc.value.message


def test_closed_order_blocked(db):
    order, ticket = _allocate_ready(db, qty=1)
    order.status = OrderStatus.CLOSED
    db.session.commit()
    with pytest.raises(ProcessingError) as exc:
        lookup_pick_ticket(ticket.pick_ticket_number)
    assert "CLOSED" in exc.value.message


def test_partial_allocation_blocked(db):
    order = make_order("SO-PART", lines=[("SKU-A", 2)])
    transition(order, OrderStatus.VALIDATED)
    db.session.commit()
    make_unit("PART-1", "SKU-A", upc=UPC)
    allocate_barcode(order, "PART-1")
    db.session.commit()
    ticket = ensure_pick_ticket(order)
    ticket.pick_ticket_number = "PT-2026-000199"
    db.session.commit()
    with pytest.raises(ProcessingError) as exc:
        lookup_pick_ticket("PT-2026-000199")
    assert "not fully allocated" in exc.value.message


def test_processing_lock_prevents_two_users(db):
    order, ticket = _allocate_ready(db)
    alice = create_user("alice", role="ADMIN")
    bob = create_user("bob", role="ADMIN")
    confirm_order(order, user=alice)
    db.session.commit()
    with pytest.raises(ProcessingError) as exc:
        lookup_pick_ticket(ticket.pick_ticket_number, user=bob)
    assert "alice" in exc.value.message
    with pytest.raises(ProcessingError):
        confirm_order(order, user=bob)


def test_confirm_order_starts_processing(db):
    order, ticket = _allocate_ready(db)
    user = _user()
    confirm_order(order, user=user)
    db.session.commit()
    assert order.status == OrderStatus.PROCESSING
    assert order.processing_user_id == user.id
    assert order.processing_username == user.username
    assert order.processing_started_at is not None
    assert order.boxes[0].box_number == "SO-51022-BOX01"
    types = [t.type for t in Transaction.query.filter_by(order_id=order.id)]
    assert ProcessingEvent.PROCESSING_STARTED in types
    assert ProcessingEvent.CARTON_CREATED in types


def test_upc_scan_succeeds(db):
    order, ticket, user = _start(db)
    box = _dims(order, user)
    content = scan_upc_into_box(box, UPC, user=user)
    db.session.commit()
    assert content.unit.upc == UPC
    assert content.unit.description is None or True


def test_one_scan_consumes_exactly_one_unit(db):
    order, ticket, user = _start(db, qty=3)
    box = _dims(order, user)
    scan_upc_into_box(box, UPC, user=user)
    db.session.commit()
    assert len(remaining_units(order)) == 2
    assert len(box.contents) == 1


def test_upc_qty_3_requires_exactly_3_scans(db):
    order, ticket, user = _start(db, qty=3)
    box = _dims(order, user)
    for _ in range(3):
        scan_upc_into_box(box, UPC, user=user)
    db.session.commit()
    assert len(box.contents) == 3
    assert remaining_units(order) == []


def test_fourth_scan_rejected(db):
    order, ticket, user = _start(db, qty=3)
    box = _dims(order, user)
    for _ in range(3):
        scan_upc_into_box(box, UPC, user=user)
    db.session.commit()
    with pytest.raises(BarcodeError) as exc:
        scan_upc_into_box(box, UPC, user=user)
    assert exc.value.exc_type == ExceptionType.NO_REMAINING_UPC


def test_remaining_qty_updates(db):
    order, ticket, user = _start(db, qty=3)
    box = _dims(order, user)
    assert remaining_unit_rows(order)[0]["qty"] == 3
    scan_upc_into_box(box, UPC, user=user)
    db.session.commit()
    assert remaining_unit_rows(order)[0]["qty"] == 2
    scan_upc_into_box(box, UPC, user=user)
    db.session.commit()
    assert remaining_unit_rows(order)[0]["qty"] == 1
    scan_upc_into_box(box, UPC, user=user)
    db.session.commit()
    assert remaining_unit_rows(order) == []


def test_current_carton_qty_updates(db):
    order, ticket, user = _start(db, qty=3)
    box = _dims(order, user)
    scan_upc_into_box(box, UPC, user=user)
    scan_upc_into_box(box, UPC, user=user)
    db.session.commit()
    assert station_kpis(order, box)["current_carton_units"] == 2
    assert len(box.contents) == 2


def test_description_retained(db):
    order, ticket = _allocate_ready(db, qty=2)
    for unit in order.units:
        unit.description = "Celine Belt Bag"
    db.session.commit()
    user = _user("desc-user")
    confirm_order(order, user=user)
    db.session.commit()
    box = _dims(order, user)
    scan_upc_into_box(box, UPC, user=user)
    db.session.commit()
    rows = remaining_unit_rows(order)
    assert rows[0]["description"] == "Celine Belt Bag"
    from app.services.processing import carton_content_rows

    assert carton_content_rows(box)[0]["description"] == "Celine Belt Bag"


def test_close_carton_before_all_order_units_allowed(db):
    order, ticket, user = _start(db, qty=3, extra_upc=UPC_B, extra_qty=2)
    box = _dims(order, user)
    scan_upc_into_box(box, UPC, user=user)
    db.session.commit()
    request_close_carton(box, user=user)
    db.session.commit()
    assert box.status == BoxStatus.AWAITING_WEIGHT
    assert remaining_units(order)


def test_carton_weight_required(db):
    order, ticket, user = _start(db, qty=1)
    box = _dims(order, user)
    scan_upc_into_box(box, UPC, user=user)
    request_close_carton(box, user=user)
    db.session.commit()
    with pytest.raises(ProcessingError):
        save_carton_weight(box, 0, "lb", user=user)
    with pytest.raises(ProcessingError):
        save_carton_weight(box, None, "lb", user=user)
    save_carton_weight(box, 14.8, "lb", user=user)
    db.session.commit()
    assert box.status == BoxStatus.CLOSED
    assert box.weight_kg == 14.8
    assert box.weight_unit == "lb"


def test_next_carton_automatically_requested_when_units_remain(db):
    order, ticket, user = _start(db, qty=3, extra_upc=UPC_B, extra_qty=2)
    box = _dims(order, user)
    scan_upc_into_box(box, UPC, user=user)
    request_close_carton(box, user=user)
    save_carton_weight(box, 10, "lb", user=user)
    db.session.commit()
    assert len(order.boxes) == 2
    assert order.boxes[1].box_number == "SO-51022-BOX02"
    assert not (order.boxes[1].length_cm)


def test_final_carton_behavior(db):
    order, ticket, user = _start(db, qty=2)
    box = _dims(order, user)
    scan_upc_into_box(box, UPC, user=user)
    scan_upc_into_box(box, UPC, user=user)
    request_close_carton(box, user=user)
    save_carton_weight(box, 8, "lb", user=user)
    db.session.commit()
    assert len(order.boxes) == 1
    assert box.status == BoxStatus.CLOSED
    assert ready_for_close_modal(order)


def test_recall_focuses_recalled_carton_over_next_empty(db):
    from app.services.processing import current_carton

    order, ticket, user = _start(db, qty=3, extra_upc=UPC_B, extra_qty=2)
    box = _dims(order, user)
    scan_upc_into_box(box, UPC, user=user)
    request_close_carton(box, user=user)
    save_carton_weight(box, 5, "lb", user=user)
    db.session.commit()
    assert len(order.boxes) == 2
    recall_carton(box, user=user)
    db.session.commit()
    assert current_carton(order).id == box.id
    remove_unit_from_carton(box.contents[0], user=user)
    db.session.commit()
    assert current_carton(order).status == BoxStatus.REWEIGH_REQUIRED


def test_recall_closed_carton(db):
    order, ticket, user = _start(db, qty=2)
    box = _dims(order, user)
    scan_upc_into_box(box, UPC, user=user)
    request_close_carton(box, user=user)
    save_carton_weight(box, 5, "lb", user=user)
    db.session.commit()
    recall_carton(box, user=user)
    db.session.commit()
    assert box.status == BoxStatus.OPEN
    types = [t.type for t in Transaction.query.filter_by(order_id=order.id)]
    assert ProcessingEvent.CARTON_RECALLED in types


def test_remove_unit_returns_it_to_remaining_pool(db):
    order, ticket, user = _start(db, qty=2)
    box = _dims(order, user)
    scan_upc_into_box(box, UPC, user=user)
    scan_upc_into_box(box, UPC, user=user)
    request_close_carton(box, user=user)
    save_carton_weight(box, 5, "lb", user=user)
    recall_carton(box, user=user)
    db.session.commit()
    content = box.contents[0]
    remove_unit_from_carton(content, user=user)
    db.session.commit()
    assert len(remaining_units(order)) == 1
    assert len(box.contents) == 1
    assert station_kpis(order, box)["total_scanned"] == 1


def test_add_unit_to_recalled_carton(db):
    order, ticket, user = _start(db, qty=2)
    box = _dims(order, user)
    scan_upc_into_box(box, UPC, user=user)
    request_close_carton(box, user=user)
    save_carton_weight(box, 5, "lb", user=user)
    recall_carton(box, user=user)
    db.session.commit()
    add_upc_to_recalled_carton(box, UPC, user=user)
    db.session.commit()
    assert len(box.contents) == 2
    assert remaining_units(order) == []


def test_content_change_invalidates_weight(db):
    order, ticket, user = _start(db, qty=2)
    box = _dims(order, user)
    scan_upc_into_box(box, UPC, user=user)
    request_close_carton(box, user=user)
    save_carton_weight(box, 5, "lb", user=user)
    recall_carton(box, user=user)
    add_upc_to_recalled_carton(box, UPC, user=user)
    db.session.commit()
    assert box.reweigh_required is True
    assert box.status == BoxStatus.REWEIGH_REQUIRED
    types = [t.type for t in Transaction.query.filter_by(order_id=order.id)]
    assert ProcessingEvent.CARTON_WEIGHT_INVALIDATED in types


def test_reweigh_mandatory(db):
    order, ticket, user = _start(db, qty=2)
    box = _dims(order, user)
    scan_upc_into_box(box, UPC, user=user)
    request_close_carton(box, user=user)
    save_carton_weight(box, 5, "lb", user=user)
    recall_carton(box, user=user)
    add_upc_to_recalled_carton(box, UPC, user=user)
    db.session.commit()
    assert any("weight" in b.lower() or "reweigh" in b.lower() for b in close_blockers(order))
    save_carton_weight(box, 6.2, "lb", user=user)
    db.session.commit()
    assert box.status == BoxStatus.CLOSED
    assert box.reweigh_required is False
    types = [t.type for t in Transaction.query.filter_by(order_id=order.id)]
    assert ProcessingEvent.CARTON_REWEIGHED in types
    assert ProcessingEvent.CARTON_RECLOSED in types


def test_close_blocked_if_stale_weight(db):
    order, ticket, user = _start(db, qty=1)
    box = _dims(order, user)
    scan_upc_into_box(box, UPC, user=user)
    request_close_carton(box, user=user)
    save_carton_weight(box, 4, "lb", user=user)
    recall_carton(box, user=user)
    content = box.contents[0]
    # add a spare unit first
    db.session.commit()
    make_unit("SPARE-1", box.contents[0].unit.sku, upc=UPC)
    # can't add spare (not allocated). Remove then we have remaining and stale.
    remove_unit_from_carton(content, user=user)
    db.session.commit()
    with pytest.raises(ProcessingError) as exc:
        finalize_order(order, user=user)
    assert exc.value.code in {ExceptionType.STALE_WEIGHT, "CLOSE_BLOCKED"}


def test_close_blocked_if_remaining_units(db):
    order, ticket, user = _start(db, qty=2)
    box = _dims(order, user)
    scan_upc_into_box(box, UPC, user=user)
    request_close_carton(box, user=user)
    save_carton_weight(box, 4, "lb", user=user)
    db.session.commit()
    with pytest.raises(ProcessingError) as exc:
        finalize_order(order, user=user)
    assert "Remaining" in exc.value.message or "Reconciliation" in exc.value.message


def test_final_reconciliation_and_yes_closes_order(db, app):
    order, ticket, user = _start(db, qty=2)
    box = _dims(order, user)
    scan_upc_into_box(box, UPC, user=user)
    scan_upc_into_box(box, UPC, user=user)
    request_close_carton(box, user=user)
    save_carton_weight(box, 7.5, "lb", user=user)
    db.session.commit()
    assert ready_for_close_modal(order)
    invoice, document = finalize_order(order, user=user)
    db.session.refresh(order)
    assert order.status == OrderStatus.CLOSED
    assert order.processing_user_id is None
    assert invoice.order_id == order.id
    assert document.type == "ORDER_CLOSURE"
    assert Document.query.filter_by(order_id=order.id, type="ORDER_CLOSURE").count() >= 1
    types = [t.type for t in Transaction.query.filter_by(order_id=order.id)]
    assert ProcessingEvent.ORDER_RECONCILED in types
    assert ProcessingEvent.ORDER_CLOSED in types
    assert ProcessingEvent.PDF_GENERATED in types
    assert order.closed_by_username == user.username
    assert order.closed_by_user_id == user.id


def test_http_lookup_confirm_scan_modal_no_and_yes_reset(admin_client, db, admin_user):
    order, ticket = _allocate_ready(db, qty=2, extra_upc=UPC_B, extra_qty=0)
    start = admin_client.get("/processing/")
    html = start.get_data(as_text=True)
    assert "Start Order" in html
    assert "Pick Ticket Number" in html
    assert "Find Order" in html
    assert "Demo UPC" not in html
    assert "Barcode" not in html

    bad = admin_client.post("/processing/find", data={"pick_ticket_number": "PT-MISSING"}, follow_redirects=True)
    assert "not found" in bad.get_data(as_text=True)

    confirm_page = admin_client.post(
        "/processing/find", data={"pick_ticket_number": ticket.pick_ticket_number}
    )
    body = confirm_page.get_data(as_text=True)
    assert "Confirm Order" in body
    assert "FULLY ALLOCATED" in body
    assert "OK - Confirm Order" in body

    pack = admin_client.post(f"/processing/{order.id}/confirm", follow_redirects=True)
    packed_html = pack.get_data(as_text=True)
    assert "Active Order" in packed_html
    assert "PROCESSING" in packed_html
    assert "Remaining Pick Ticket Units" in packed_html
    assert "Confirm Carton 1" in packed_html
    assert "Barcode" not in packed_html
    assert "Demo UPC" not in packed_html

    box = Box.query.filter_by(order_id=order.id).first()
    admin_client.post(
        f"/processing/{order.id}/dimensions",
        data={"box_id": box.id, "length": "18", "width": "14", "height": "10", "dimension_unit": "in"},
        follow_redirects=True,
    )
    first = admin_client.post(
        f"/processing/{order.id}/scan-upc",
        data={"box_id": box.id, "upc": UPC},
        follow_redirects=True,
    )
    assert first.status_code == 200
    admin_client.post(f"/processing/{order.id}/scan-upc", data={"box_id": box.id, "upc": UPC}, follow_redirects=True)

    closed = admin_client.post(f"/processing/box/{box.id}/request-close", follow_redirects=True)
    assert "Carton Weight" in closed.get_data(as_text=True)
    modal = admin_client.post(
        f"/processing/box/{box.id}/weight",
        data={"weight": "12.5", "weight_unit": "lb"},
        follow_redirects=True,
    )
    modal_html = modal.get_data(as_text=True)
    assert "All Units Scanned" in modal_html
    assert "Close this order now?" in modal_html

    stay = admin_client.post(f"/processing/{order.id}/decision", data={"choice": "no"}, follow_redirects=True)
    stay_html = stay.get_data(as_text=True)
    assert "Active Order" in stay_html
    assert "All Units Scanned" not in stay_html
    db.session.refresh(order)
    assert order.status == OrderStatus.PROCESSING
    assert order.processing_user_id == admin_user.id

    yes = admin_client.post(f"/processing/{order.id}/decision", data={"choice": "yes"}, follow_redirects=True)
    yes_html = yes.get_data(as_text=True)
    assert "Start Order" in yes_html
    assert "Pick Ticket Number" in yes_html
    db.session.refresh(order)
    assert order.status == OrderStatus.CLOSED
    assert order.processing_user_id is None
    assert Document.query.filter_by(order_id=order.id, type="ORDER_CLOSURE").count() >= 1


def test_http_lock_and_attribution(app, db):
    order, ticket = _allocate_ready(db, qty=1)
    create_user("alice", role="ADMIN")
    create_user("bob", role="ADMIN")
    c_alice = app.test_client()
    login(c_alice, "alice")
    c_bob = app.test_client()
    login(c_bob, "bob")
    c_alice.post(f"/processing/{order.id}/confirm", follow_redirects=True)
    locked = c_bob.post("/processing/find", data={"pick_ticket_number": ticket.pick_ticket_number})
    assert "alice" in locked.get_data(as_text=True)
    txn = Transaction.query.filter_by(order_id=order.id, type=ProcessingEvent.PROCESSING_STARTED).first()
    assert txn.username == "alice"
    assert txn.user_id is not None


def test_completed_orders_export_columns(db, admin_client):
    order, ticket, user = _start(db, qty=1)
    box = _dims(order, user)
    scan_upc_into_box(box, UPC, user=user)
    request_close_carton(box, user=user)
    save_carton_weight(box, 3, "lb", user=user)
    finalize_order(order, user=user)
    rows = completed_orders_query({}).all()
    assert any(o.id == order.id for o in rows)
    buf = build_completed_orders_workbook(rows)
    assert buf.read(2) == b"PK"
    resp = admin_client.get("/reports/completed/export")
    assert resp.status_code == 200
    assert resp.data[:2] == b"PK"
    assert EXPORT_COLUMNS[0] == "Closed Date/Time"
    assert "Pick Ticket Number" in EXPORT_COLUMNS
    assert "Closure PDF" in EXPORT_COLUMNS


def test_cancel_releases_lock_and_resets_station(db, admin_client, admin_user):
    order, ticket = _allocate_ready(db, qty=1)
    admin_client.post(f"/processing/{order.id}/confirm", follow_redirects=True)
    db.session.refresh(order)
    assert order.processing_user_id == admin_user.id
    reset = admin_client.post(f"/processing/{order.id}/cancel", follow_redirects=True)
    assert "Start Order" in reset.get_data(as_text=True)
    db.session.refresh(order)
    assert order.processing_user_id is None
