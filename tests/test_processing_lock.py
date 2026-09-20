"""Exclusive database-backed Order Processing lock."""

from __future__ import annotations

import threading

import pytest

from app.constants import OrderStatus
from app.extensions import db
from app.models import Order
from app.services.processing import (
    LOCKED_BY_OTHER_MESSAGE,
    ProcessingError,
    acquire_lock,
    cancel_session,
    confirm_order,
    finalize_order,
    release_lock,
)
from tests.conftest import create_user, login
from tests.test_order_processing import UPC, _allocate_ready, _dims, _start
from app.services.processing import (
    request_close_carton,
    save_carton_weight,
    scan_upc_into_box,
)


def test_a_user_acquires_order_one(db):
    order, _ = _allocate_ready(db, order_number="SO-L1", pt="PT-L1")
    alice = create_user("alice", role="ADMIN")
    confirm_order(order, user=alice)
    db.session.commit()
    db.session.refresh(order)
    assert order.processing_user_id == alice.id
    assert order.processing_username == "alice"
    assert order.processing_started_at is not None
    assert order.processing_lock_id


def test_b_user_cannot_acquire_same_order(db):
    order, _ = _allocate_ready(db, order_number="SO-L2", pt="PT-L2")
    alice = create_user("alice", role="ADMIN")
    bob = create_user("bob", role="ADMIN")
    confirm_order(order, user=alice)
    db.session.commit()
    with pytest.raises(ProcessingError) as exc:
        confirm_order(order, user=bob)
    assert exc.value.message == LOCKED_BY_OTHER_MESSAGE
    db.session.refresh(order)
    assert order.processing_user_id == alice.id
    assert order.processing_username == "alice"


def test_c_user_can_acquire_different_order(db):
    order1, _ = _allocate_ready(db, order_number="SO-L3A", pt="PT-L3A")
    order2, _ = _allocate_ready(db, order_number="SO-L3B", pt="PT-L3B", upc="194900099001")
    alice = create_user("alice", role="ADMIN")
    bob = create_user("bob", role="ADMIN")
    confirm_order(order1, user=alice)
    confirm_order(order2, user=bob)
    db.session.commit()
    db.session.refresh(order1)
    db.session.refresh(order2)
    assert order1.processing_user_id == alice.id
    assert order2.processing_user_id == bob.id
    assert order1.processing_lock_id != order2.processing_lock_id


def test_d_simultaneous_acquisition_one_winner(app, db):
    order, _ = _allocate_ready(db, order_number="SO-RACE", pt="PT-RACE")
    alice = create_user("alice", role="ADMIN")
    bob = create_user("bob", role="ADMIN")
    order_id = order.id
    alice_id = alice.id
    bob_id = bob.id
    db.session.commit()
    db.session.remove()

    barrier = threading.Barrier(2)
    outcomes = []

    def worker(user_id):
        with app.app_context():
            from app.models import User

            user = User.query.get(user_id)
            try:
                barrier.wait(timeout=5)
                target = Order.query.get(order_id)
                acquire_lock(target, user=user)
                db.session.commit()
                outcomes.append(("ok", user_id))
            except ProcessingError as exc:
                db.session.rollback()
                outcomes.append(("err", user_id, exc.message))
            except Exception as exc:  # noqa: BLE001
                db.session.rollback()
                outcomes.append(("exc", user_id, str(exc)))

    threads = [
        threading.Thread(target=worker, args=(alice_id,)),
        threading.Thread(target=worker, args=(bob_id,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    wins = [row for row in outcomes if row[0] == "ok"]
    losses = [row for row in outcomes if row[0] == "err"]
    assert len(outcomes) == 2, outcomes
    assert len(wins) == 1, outcomes
    assert len(losses) == 1, outcomes
    assert losses[0][2] == LOCKED_BY_OTHER_MESSAGE
    locked = Order.query.get(order_id)
    assert locked.processing_user_id in {alice_id, bob_id}
    assert locked.processing_lock_id


def test_e_lock_survives_refresh_and_new_request(app, db):
    order, ticket = _allocate_ready(db, order_number="SO-L5", pt="PT-L5", qty=1)
    create_user("alice", role="ADMIN")
    client = app.test_client()
    login(client, "alice")
    confirm = client.post(f"/processing/{order.id}/confirm", follow_redirects=True)
    assert confirm.status_code == 200
    db.session.refresh(order)
    lock_id = order.processing_lock_id
    owner = order.processing_user_id
    started = order.processing_started_at
    assert lock_id and owner and started

    refresh = client.get(f"/processing/{order.id}")
    assert refresh.status_code == 200
    assert "Active Order" in refresh.get_data(as_text=True)
    db.session.refresh(order)
    assert order.processing_user_id == owner
    assert order.processing_lock_id == lock_id
    assert order.processing_started_at == started
    assert order.status == OrderStatus.PROCESSING


def test_f_successful_close_releases_lock(db):
    order, _ticket, user = _start(db, qty=1, order_number="SO-L6", pt="PT-L6")
    box = _dims(order, user)
    scan_upc_into_box(box, UPC, user=user)
    request_close_carton(box, user=user)
    save_carton_weight(box, 3, "lb", user=user)
    finalize_order(order, user=user)
    db.session.refresh(order)
    assert order.status == OrderStatus.CLOSED
    assert order.processing_user_id is None
    assert order.processing_username is None
    assert order.processing_started_at is None
    assert order.processing_lock_id is None


def test_g_authorized_cancel_releases_lock(db):
    order, _ = _allocate_ready(db, order_number="SO-L7", pt="PT-L7")
    alice = create_user("alice", role="ADMIN")
    confirm_order(order, user=alice)
    db.session.commit()
    cancel_session(order, user=alice)
    db.session.commit()
    db.session.refresh(order)
    assert order.processing_user_id is None
    assert order.processing_lock_id is None
    assert order.processing_started_at is None


def test_h_unauthorized_user_cannot_release_lock(app, db):
    order, _ = _allocate_ready(db, order_number="SO-L8", pt="PT-L8")
    alice = create_user("alice", role="ADMIN")
    bob = create_user("bob", role="ADMIN")
    confirm_order(order, user=alice)
    db.session.commit()
    lock_id = order.processing_lock_id

    with pytest.raises(ProcessingError) as exc:
        cancel_session(order, user=bob)
    assert exc.value.message == LOCKED_BY_OTHER_MESSAGE
    with pytest.raises(ProcessingError):
        release_lock(order, user=bob, require_owner=True)
    db.session.refresh(order)
    assert order.processing_user_id == alice.id
    assert order.processing_lock_id == lock_id

    client = app.test_client()
    login(client, "bob")
    resp = client.post(f"/processing/{order.id}/cancel", follow_redirects=True)
    assert LOCKED_BY_OTHER_MESSAGE in resp.get_data(as_text=True)
    db.session.refresh(order)
    assert order.processing_user_id == alice.id
    assert order.processing_lock_id == lock_id


def test_http_second_user_blocked_from_station(app, db):
    order, _ = _allocate_ready(db, order_number="SO-L9", pt="PT-L9", qty=1)
    create_user("alice", role="ADMIN")
    create_user("bob", role="ADMIN")
    c_alice = app.test_client()
    login(c_alice, "alice")
    c_bob = app.test_client()
    login(c_bob, "bob")
    c_alice.post(f"/processing/{order.id}/confirm", follow_redirects=True)
    blocked = c_bob.post(f"/processing/{order.id}/confirm", follow_redirects=True)
    assert LOCKED_BY_OTHER_MESSAGE in blocked.get_data(as_text=True)
    station = c_bob.get(f"/processing/{order.id}", follow_redirects=True)
    assert LOCKED_BY_OTHER_MESSAGE in station.get_data(as_text=True)
    db.session.refresh(order)
    assert order.processing_username == "alice"
