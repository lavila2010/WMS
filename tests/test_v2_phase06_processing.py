import threading

import pytest

from app.constants import CartonStatus, LedgerType, OrderStatus, UnitStatus
from app.extensions import db
from app.models import Carton, CartonContent, Document, InventoryTransaction, InventoryUnit, Order, User
from app.services.allocation import allocate_order
from app.services.inventory_ledger import create_available_unit
from app.services.pick_tickets import create_pick_ticket
from app.services.processing import (
    LOCK_MESSAGE,
    ProcessingError,
    acquire_lock,
    close_order,
    ensure_open_carton,
    recall_carton,
    release_lock,
    remove_unit,
    request_close,
    scan_upc,
    set_dimensions,
    set_weight,
)
from tests.conftest import create_user, login
from tests.test_v2_phase03_orders import _line
from tests.test_v2_phase04_allocation import _import_order, _world
from tests.test_v2_phase05_pick_tickets import _ready_order


def _processing_order(db, admin_user, qty=3, upc="UPC-A"):
    w, order = _ready_order(db, admin_user, qty=qty, upc=upc)
    ticket = create_pick_ticket(order)
    acquire_lock(order, admin_user)
    carton = ensure_open_carton(order, admin_user)
    set_dimensions(carton, 10, 8, 6)
    db.session.commit()
    return w, Order.query.get(order.id), ticket, Carton.query.get(carton.id)


def test_p6_01_one_scan_one_unit(app, db, admin_user):
    _, order, _, carton = _processing_order(db, admin_user)
    scan_upc(order, carton, "UPC-A", admin_user)
    assert InventoryUnit.query.filter_by(allocated_order_id=order.id, status=UnitStatus.PACKED).count() == 1
    assert InventoryTransaction.query.filter_by(transaction_type=LedgerType.PACK).count() == 1


def test_p6_02_excess_scan_blocked(app, db, admin_user):
    _, order, _, carton = _processing_order(db, admin_user, qty=3)
    for _ in range(3):
        scan_upc(order, carton, "UPC-A", admin_user)
    with pytest.raises(ProcessingError):
        scan_upc(order, carton, "UPC-A", admin_user)
    assert InventoryUnit.query.filter_by(status=UnitStatus.PACKED).count() == 3


def test_p6_03_wrong_upc_client_warehouse_order(app, db, admin_user):
    w, order, _, carton = _processing_order(db, admin_user)
    with pytest.raises(ProcessingError):
        scan_upc(order, carton, "NOPE", admin_user)
    other = _import_order(admin_user, w["celine"], w["cel_ecom"], [_line(order="999", upc="UPC-Z", qty=1)])
    create_available_unit(client_id=w["dior"].id, warehouse_id=w["dio_ny"].id, upc="UPC-A", location="Z")
    db.session.commit()
    with pytest.raises(ProcessingError):
        scan_upc(order, carton, "UPC-Z", admin_user)
    assert other.status == OrderStatus.UNALLOCATED


def test_p6_04_two_users_one_lock_winner(app, db, admin_user):
    w, order = _ready_order(db, admin_user)
    create_pick_ticket(order)
    other = create_user("packer", role="USER", perms=["PROCESSING_VIEW", "PROCESSING_EXECUTE"])
    oid = order.id
    db.session.commit()
    db.session.expire_all()
    winners = []

    def worker(username):
        with app.app_context():
            user = User.query.filter_by(username=username).one()
            target = db.session.get(Order, oid)
            try:
                acquire_lock(target, user)
                winners.append(username)
            except ProcessingError as exc:
                assert str(exc) == LOCK_MESSAGE

    threads = [threading.Thread(target=worker, args=("admin",)), threading.Thread(target=worker, args=("packer",))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(winners) == 1
    db.session.expire_all()
    locked = db.session.get(Order, oid)
    assert locked.processing_username == winners[0]


def test_p6_05_refresh_keeps_lock(app, db, admin_user, admin_client):
    _, order, ticket, _ = _processing_order(db, admin_user)
    first = admin_client.get(f"/processing/{order.id}")
    assert first.status_code == 200
    db.session.refresh(order)
    assert order.processing_user_id == admin_user.id
    assert order.processing_lock_id


def test_p6_06_owner_cancel_other_denied(app, db, admin_user):
    _, order, _, _ = _processing_order(db, admin_user)
    other = create_user("intruder", role="USER", perms=["PROCESSING_EXECUTE"])
    with pytest.raises(ProcessingError, match="another user"):
        release_lock(order, other, to_status=OrderStatus.PICK_TICKET_READY)
    release_lock(order, admin_user, to_status=OrderStatus.PICK_TICKET_READY)
    db.session.commit()
    assert Order.query.get(order.id).processing_user_id is None


def test_p6_07_recall_unpack_reweigh(app, db, admin_user):
    _, order, _, carton = _processing_order(db, admin_user, qty=2)
    scan_upc(order, carton, "UPC-A", admin_user)
    request_close(carton, admin_user)
    set_weight(carton, 1.5, user=admin_user)
    carton = Carton.query.get(carton.id)
    recall_carton(carton, admin_user)
    content = CartonContent.query.filter_by(carton_id=carton.id).one()
    remove_unit(content, admin_user)
    carton = Carton.query.get(carton.id)
    assert carton.reweigh_required is True
    assert InventoryTransaction.query.filter_by(transaction_type=LedgerType.UNPACK).count() == 1
    assert InventoryUnit.query.filter_by(status=UnitStatus.RESERVED).count() == 2


def test_p6_08_atomic_close_ships_and_releases(app, db, admin_user):
    _, order, _, carton = _processing_order(db, admin_user, qty=2)
    scan_upc(order, carton, "UPC-A", admin_user)
    scan_upc(order, carton, "UPC-A", admin_user)
    request_close(carton, admin_user)
    set_weight(carton, 2.2, user=admin_user)
    document = close_order(Order.query.get(order.id), admin_user)
    order = Order.query.get(order.id)
    assert order.status == OrderStatus.CLOSED
    assert order.processing_user_id is None
    assert InventoryUnit.query.filter_by(allocated_order_id=order.id, status=UnitStatus.SHIPPED).count() == 2
    assert InventoryTransaction.query.filter_by(transaction_type=LedgerType.SHIP).count() == 2
    assert document.id
    assert Document.query.filter_by(order_id=order.id, type="ORDER_CLOSURE").count() == 1


def test_p6_09_close_blocked_when_remaining_or_reweigh(app, db, admin_user):
    _, order, _, carton = _processing_order(db, admin_user, qty=2)
    scan_upc(order, carton, "UPC-A", admin_user)
    with pytest.raises(ProcessingError):
        close_order(order, admin_user)
    scan_upc(order, carton, "UPC-A", admin_user)
    request_close(carton, admin_user)
    set_weight(carton, 1.0, user=admin_user)
    recall_carton(Carton.query.get(carton.id), admin_user)
    with pytest.raises(ProcessingError):
        close_order(Order.query.get(order.id), admin_user)
