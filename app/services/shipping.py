"""Carton tracking independent of warehouse fulfillment status."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import or_

from ..auth import record_audit
from ..constants import (
    CartonStatus,
    OrderStatus,
    PickTicketStatus,
    ShippingLabelStatus,
    ShippingStatus,
    TRACKING_NUMBER_MAX_LEN,
    TrackingCarrier,
)
from ..extensions import db
from ..models import Carton, CartonContent, Order, PickTicket, User
from .pick_tickets import desired_ticket_status


class ShippingError(ValueError):
    pass


def normalize_tracking_number(value: str) -> str:
    text = (value or "").strip()
    if not text:
        raise ShippingError("Tracking number is required.")
    if len(text) > TRACKING_NUMBER_MAX_LEN:
        raise ShippingError(f"Tracking number must be {TRACKING_NUMBER_MAX_LEN} characters or fewer.")
    return text


def normalize_carrier(value: str) -> str:
    carrier = (value or "").strip().upper()
    if carrier not in TrackingCarrier.ALL:
        raise ShippingError("Carrier must be UPS, FEDEX, or OTHER.")
    return carrier


def derived_shipping_status(order: Order, cartons: list[Carton] | None = None) -> str:
    if order is None or order.status != OrderStatus.CLOSED:
        return ShippingStatus.NOT_READY
    cartons = cartons if cartons is not None else Carton.query.filter_by(order_id=order.id).all()
    if not cartons:
        return ShippingStatus.PENDING_TRACKING
    complete = all(
        carton.shipping_label_status == ShippingLabelStatus.VALIDATED
        and (carton.tracking_number or "").strip()
        and (carton.tracking_carrier or "").strip()
        for carton in cartons
    )
    return ShippingStatus.TRACKING_COMPLETE if complete else ShippingStatus.PENDING_TRACKING


def sync_shipping_status(order: Order, cartons: list[Carton] | None = None) -> str:
    desired = derived_shipping_status(order, cartons)
    if order.shipping_status != desired:
        order.shipping_status = desired
    return desired


def _username(user_id) -> str | None:
    if not user_id:
        return None
    user = db.session.get(User, user_id)
    return user.username if user else None


def save_carton_tracking(order: Order, carton: Carton, *, tracking_number: str, carrier: str, user: User, reason=""):
    if carton.order_id != order.id:
        raise ShippingError("Carton does not belong to this order.")
    number = normalize_tracking_number(tracking_number)
    carrier = normalize_carrier(carrier)
    old_number = carton.tracking_number
    old_carrier = carton.tracking_carrier
    first_entry = not (old_number or "").strip()
    changed = (old_number or "") != number or (old_carrier or "") != carrier
    if not changed:
        return carton
    carton.tracking_number = number
    carton.tracking_carrier = carrier
    carton.tracking_entered_at = datetime.utcnow()
    carton.tracking_entered_by_user_id = user.id
    if carton.shipping_label_status == ShippingLabelStatus.VALIDATED and changed:
        # Correction of a confirmed number stays on the carton; confirmation remains until re-validated as a set.
        pass
    if first_entry:
        record_audit(
            "CARTON_TRACKING_ENTERED",
            module="Shipping",
            entity_type="carton",
            entity_id=carton.id,
            client_id=order.client_id,
            detail=f"{order.wms_order_id} {carton.carton_number} {carrier} {number}",
        )
    else:
        record_audit(
            "CARTON_TRACKING_UPDATED",
            module="Shipping",
            entity_type="carton",
            entity_id=carton.id,
            client_id=order.client_id,
            detail=(
                f"{order.wms_order_id} {carton.carton_number} "
                f"{old_carrier or '—'} {old_number or '—'} -> {carrier} {number}"
                + (f" reason={reason}" if reason else "")
            ),
        )
    sync_shipping_status(order)
    db.session.flush()
    return carton


def confirm_shipping(order: Order, user: User):
    if order.status != OrderStatus.CLOSED:
        raise ShippingError("Shipping can be confirmed only for a CLOSED order.")
    ticket = PickTicket.query.filter_by(order_id=order.id).first()
    if ticket and desired_ticket_status(order) != PickTicketStatus.CLOSED:
        raise ShippingError("Pick Ticket must remain closed for a closed order.")
    cartons = Carton.query.filter_by(order_id=order.id).order_by(Carton.id).all()
    if not cartons:
        raise ShippingError("Order has no cartons.")
    for carton in cartons:
        if carton.status != CartonStatus.CLOSED:
            raise ShippingError("Every carton must be CLOSED before shipping confirmation.")
        if not (carton.tracking_number or "").strip() or not (carton.tracking_carrier or "").strip():
            raise ShippingError("Every carton must have a tracking carrier and tracking number.")
    now = datetime.utcnow()
    for carton in cartons:
        carton.shipping_label_status = ShippingLabelStatus.VALIDATED
        carton.tracking_validated_at = now
        carton.tracking_validated_by_user_id = user.id
        record_audit(
            "CARTON_TRACKING_VALIDATED",
            module="Shipping",
            entity_type="carton",
            entity_id=carton.id,
            client_id=order.client_id,
            detail=f"{order.wms_order_id} {carton.carton_number} {carton.tracking_carrier} {carton.tracking_number}",
        )
    order.shipping_status = ShippingStatus.TRACKING_COMPLETE
    # Fulfillment and pick-ticket status must not change.
    record_audit(
        "ORDER_SHIPPING_CONFIRMED",
        module="Shipping",
        entity_type="order",
        entity_id=order.id,
        client_id=order.client_id,
        detail=f"{order.wms_order_id} {ticket.pick_ticket_number if ticket else ''}".strip(),
    )
    db.session.commit()
    return order


def carton_display(carton: Carton) -> dict:
    return {
        "carton": carton,
        "dims": f"{carton.length or '—'} x {carton.width or '—'} x {carton.height or '—'}",
        "weight": f"{carton.weight:g} {carton.weight_unit}" if carton.weight is not None else "—",
        "units": CartonContent.query.filter_by(carton_id=carton.id).count(),
        "entered_by": _username(carton.tracking_entered_by_user_id),
        "validated_by": _username(carton.tracking_validated_by_user_id),
    }


def list_closed_shipping_orders(
    *,
    client_id=None,
    division_id=None,
    warehouse_id=None,
    closed_date="",
    shipping_status="",
    carrier="",
    q="",
    accessible_client_ids=None,
    admin=False,
):
    from .order_visibility import apply_operational_order_visibility

    query = apply_operational_order_visibility(Order.query.filter(Order.status == OrderStatus.CLOSED))
    if client_id:
        query = query.filter(Order.client_id == client_id)
    elif not admin:
        query = query.filter(Order.client_id.in_(accessible_client_ids or [-1]))
    if division_id:
        query = query.filter(Order.division_id == division_id)
    if warehouse_id:
        query = query.filter(Order.warehouse_id == warehouse_id)
    if shipping_status in ShippingStatus.ALL:
        query = query.filter(Order.shipping_status == shipping_status)
    if carrier or q:
        query = query.outerjoin(Carton, Carton.order_id == Order.id)
    if q:
        query = query.outerjoin(PickTicket, PickTicket.order_id == Order.id)
    if carrier:
        token = carrier.strip()
        query = query.filter(or_(Order.carrier.ilike(f"%{token}%"), Carton.tracking_carrier == token.upper()))
    if closed_date:
        day = datetime.strptime(closed_date, "%Y-%m-%d")
        query = query.filter(Order.closed_at >= day, Order.closed_at < day + timedelta(days=1))
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(
            or_(
                Order.wms_order_id.ilike(like),
                Order.client_order_number.ilike(like),
                Order.customer.ilike(like),
                PickTicket.pick_ticket_number.ilike(like),
                Carton.tracking_number.ilike(like),
            )
        )
    orders = query.distinct().order_by(Order.closed_at.desc(), Order.id.desc()).limit(300).all()
    for order in orders:
        sync_shipping_status(order)
    return orders
