"""Order processing: exclusive lock, cartons, UPC scans, recall, close."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from uuid import uuid4

from sqlalchemy import select, text

from ..auth import current_actor, record_audit
from ..constants import (
    CLOSED_PICK_TICKET_MESSAGE,
    CartonStatus,
    LedgerType,
    OrderStatus,
    PickTicketStatus,
    UnitStatus,
)
from ..extensions import db
from ..models import (
    Allocation,
    Carton,
    CartonContent,
    InventoryUnit,
    Invoice,
    Order,
    OrderLine,
    PickTicket,
    User,
    Document,
)
from .documents import persist_closure_pdf
from .inventory_ledger import LedgerError, transition_unit
from .order_visibility import order_is_operational
from .pick_tickets import desired_ticket_status, sync_ticket_status

LOCK_MESSAGE = "Order is currently being processed by another user."
CANCELLED_TICKET_MESSAGE = "Pick ticket is cancelled."
PROCESSABLE_ORDER_STATUSES = {OrderStatus.PICK_TICKET_READY, OrderStatus.PROCESSING}


class ProcessingError(ValueError):
    pass


def assert_ticket_processable(ticket: PickTicket, order: Order | None = None) -> Order:
    order = order or db.session.get(Order, ticket.order_id)
    if ticket is None or order is None:
        raise ProcessingError("Pick ticket not found.")
    derived = desired_ticket_status(order)
    stored = ticket.status if ticket.status != "ACTIVE" else PickTicketStatus.OPEN
    if (
        derived == PickTicketStatus.CLOSED
        or stored == PickTicketStatus.CLOSED
        or order.status == OrderStatus.CLOSED
    ):
        raise ProcessingError(CLOSED_PICK_TICKET_MESSAGE)
    if (
        derived == PickTicketStatus.CANCELLED
        or stored == PickTicketStatus.CANCELLED
        or order.status == OrderStatus.CANCELLED
    ):
        raise ProcessingError(CANCELLED_TICKET_MESSAGE)
    if stored != PickTicketStatus.OPEN:
        raise ProcessingError(CLOSED_PICK_TICKET_MESSAGE)
    if order.status not in PROCESSABLE_ORDER_STATUSES:
        raise ProcessingError("Order is not eligible for processing.")
    return order


def find_ticket(number: str) -> PickTicket:
    ticket = PickTicket.query.filter_by(pick_ticket_number=(number or "").strip()).first()
    if ticket is None:
        raise ProcessingError("Pick ticket not found.")
    order = db.session.get(Order, ticket.order_id)
    if order is not None and not order_is_operational(order):
        raise ProcessingError("Pick ticket not found.")
    assert_ticket_processable(ticket, order)
    return ticket


def acquire_lock(order: Order, user: User) -> str:
    ticket = PickTicket.query.filter_by(order_id=order.id).first()
    if ticket is None:
        raise ProcessingError("Pick ticket not found.")
    assert_ticket_processable(ticket, order)
    lock_id = uuid4().hex
    result = db.session.execute(
        text(
            """
            UPDATE orders
            SET processing_user_id = :uid,
                processing_username = :uname,
                processing_started_at = COALESCE(processing_started_at, CURRENT_TIMESTAMP),
                processing_lock_id = :lid,
                status = :status
            WHERE id = :oid
              AND (processing_user_id IS NULL OR processing_user_id = :uid)
              AND status IN ('PICK_TICKET_READY', 'PROCESSING')
            """
        ),
        {
            "uid": user.id,
            "uname": user.username,
            "lid": lock_id,
            "status": OrderStatus.PROCESSING,
            "oid": order.id,
        },
    )
    if result.rowcount != 1:
        raise ProcessingError(LOCK_MESSAGE)
    record_audit(
        "PROCESSING_STARTED",
        module="Processing",
        entity_type="order",
        entity_id=order.id,
        client_id=order.client_id,
        detail=order.wms_order_id,
    )
    db.session.commit()
    db.session.refresh(order)
    return order.processing_lock_id


def release_lock(order: Order, user: User, *, force=False, to_status=None):
    if not force and order.processing_user_id not in (None, user.id) and not user.is_admin():
        raise ProcessingError(LOCK_MESSAGE)
    order.processing_user_id = None
    order.processing_username = None
    order.processing_started_at = None
    order.processing_lock_id = None
    if to_status:
        order.status = to_status
    db.session.flush()


def require_owner(order: Order, user: User):
    if order.processing_user_id != user.id and not user.is_admin():
        raise ProcessingError(LOCK_MESSAGE)


def next_carton_number(order: Order) -> str:
    count = Carton.query.filter_by(order_id=order.id).count()
    return f"{order.client_order_number}-BOX{count + 1:02d}"


def ensure_open_carton(order: Order, user: User) -> Carton:
    ticket = PickTicket.query.filter_by(order_id=order.id).first()
    if ticket:
        assert_ticket_processable(ticket, order)
    current = (
        Carton.query.filter_by(order_id=order.id)
        .filter(Carton.status != CartonStatus.CLOSED)
        .order_by(Carton.id.desc())
        .first()
    )
    if current:
        return current
    uid, uname = current_actor()
    carton = Carton(
        client_id=order.client_id,
        order_id=order.id,
        carton_number=next_carton_number(order),
        status=CartonStatus.OPEN,
        created_by_user_id=uid,
        created_by_username=uname,
    )
    db.session.add(carton)
    db.session.flush()
    record_audit(
        "CARTON_CREATED",
        module="Processing",
        entity_type="carton",
        entity_id=carton.id,
        client_id=order.client_id,
        detail=carton.carton_number,
    )
    return carton


def dims_ready(carton: Carton) -> bool:
    return bool(carton.length and carton.width and carton.height)


def set_dimensions(carton: Carton, length, width, height, unit="in"):
    try:
        carton.length = float(length)
        carton.width = float(width)
        carton.height = float(height)
    except (TypeError, ValueError) as exc:
        raise ProcessingError("Carton dimensions are required.") from exc
    if min(carton.length, carton.width, carton.height) <= 0:
        raise ProcessingError("Carton dimensions must be greater than 0.")
    carton.dimension_unit = unit or "in"
    db.session.flush()


def remaining_rows(order: Order) -> list[dict]:
    units = InventoryUnit.query.filter_by(
        allocated_order_id=order.id, status=UnitStatus.RESERVED
    ).all()
    grouped = defaultdict(lambda: {"qty": 0, "sku": None, "description": None})
    for unit in units:
        line = OrderLine.query.filter_by(order_id=order.id, upc=unit.upc).first()
        key = (unit.location, unit.upc)
        grouped[key]["qty"] += 1
        grouped[key]["sku"] = unit.sku
        grouped[key]["description"] = unit.description or (line.description if line else None)
    return [
        {
            "location": loc,
            "upc": upc,
            "sku": meta["sku"],
            "description": meta["description"],
            "qty": meta["qty"],
        }
        for (loc, upc), meta in sorted(grouped.items())
    ]


def carton_contents(carton: Carton) -> list[dict]:
    grouped = defaultdict(lambda: {"qty": 0, "sku": None, "description": None, "ids": []})
    for content in carton.contents:
        unit = db.session.get(InventoryUnit, content.inventory_unit_id)
        line = OrderLine.query.filter_by(order_id=carton.order_id, upc=content.upc).first()
        grouped[content.upc]["qty"] += 1
        grouped[content.upc]["sku"] = unit.sku if unit else None
        grouped[content.upc]["description"] = (unit.description if unit else None) or (
            line.description if line else None
        )
        grouped[content.upc]["ids"].append(content.id)
    return [
        {
            "upc": upc,
            "sku": meta["sku"],
            "description": meta["description"],
            "qty": meta["qty"],
            "remove_id": meta["ids"][-1],
        }
        for upc, meta in grouped.items()
    ]


def scan_upc(order: Order, carton: Carton, upc: str, user: User) -> InventoryUnit:
    require_owner(order, user)
    ticket = PickTicket.query.filter_by(order_id=order.id).first()
    if ticket:
        assert_ticket_processable(ticket, order)
    upc = (upc or "").strip()
    if not upc:
        raise ProcessingError("UPC is required.")
    if not dims_ready(carton):
        raise ProcessingError("Enter carton dimensions before scanning.")
    if carton.status == CartonStatus.CLOSED:
        raise ProcessingError("Carton is closed.")
    stmt = (
        select(InventoryUnit)
        .where(
            InventoryUnit.client_id == order.client_id,
            InventoryUnit.warehouse_id == order.warehouse_id,
            InventoryUnit.allocated_order_id == order.id,
            InventoryUnit.upc == upc,
            InventoryUnit.status == UnitStatus.RESERVED,
        )
        .order_by(InventoryUnit.location.asc(), InventoryUnit.id.asc())
        .with_for_update()
        .limit(1)
    )
    unit = db.session.execute(stmt).scalar_one_or_none()
    if unit is None:
        raise ProcessingError("No reserved unit available for this UPC on the order.")
    uid, _ = current_actor()
    content = CartonContent(carton_id=carton.id, inventory_unit_id=unit.id, upc=upc)
    db.session.add(content)
    db.session.flush()
    transition_unit(
        unit,
        to_status=UnitStatus.PACKED,
        transaction_type=LedgerType.PACK,
        user_id=uid,
        order_id=order.id,
        carton_id=carton.id,
        reference=f"scan:{order.wms_order_id}",
    )
    line = OrderLine.query.filter_by(order_id=order.id, upc=upc).first()
    if line:
        line.qty_packed += 1
    if carton.weight is not None or carton.status == CartonStatus.CLOSED:
        carton.previous_weight = carton.weight
        carton.weight = None
        carton.reweigh_required = True
        carton.status = CartonStatus.REWEIGH_REQUIRED
        record_audit(
            "CARTON_WEIGHT_INVALIDATED",
            module="Processing",
            entity_type="carton",
            entity_id=carton.id,
            client_id=order.client_id,
        )
    record_audit(
        "UNIT_SCAN",
        module="Processing",
        entity_type="inventory_unit",
        entity_id=unit.id,
        client_id=order.client_id,
        detail=upc,
    )
    db.session.commit()
    return unit


def remove_unit(content: CartonContent, user: User) -> None:
    carton = db.session.get(Carton, content.carton_id)
    order = db.session.get(Order, carton.order_id)
    require_owner(order, user)
    unit = db.session.get(InventoryUnit, content.inventory_unit_id)
    uid, _ = current_actor()
    transition_unit(
        unit,
        to_status=UnitStatus.RESERVED,
        transaction_type=LedgerType.UNPACK,
        user_id=uid,
        order_id=order.id,
        carton_id=carton.id,
        clear_carton=True,
        reference=f"unpack:{order.wms_order_id}",
    )
    line = OrderLine.query.filter_by(order_id=order.id, upc=unit.upc).first()
    if line and line.qty_packed > 0:
        line.qty_packed -= 1
    db.session.delete(content)
    if carton.weight is not None or carton.status == CartonStatus.CLOSED:
        carton.previous_weight = carton.weight
        carton.weight = None
        carton.reweigh_required = True
        carton.status = CartonStatus.REWEIGH_REQUIRED
        record_audit(
            "CARTON_WEIGHT_INVALIDATED",
            module="Processing",
            entity_type="carton",
            entity_id=carton.id,
            client_id=order.client_id,
        )
    record_audit(
        "CARTON_UNIT_REMOVED",
        module="Processing",
        entity_type="carton",
        entity_id=carton.id,
        client_id=order.client_id,
        detail=unit.upc,
    )
    db.session.commit()


def request_close(carton: Carton, user: User):
    order = db.session.get(Order, carton.order_id)
    require_owner(order, user)
    if not carton.contents:
        raise ProcessingError("Carton has no units.")
    if not dims_ready(carton):
        raise ProcessingError("Carton dimensions are incomplete.")
    carton.status = CartonStatus.AWAITING_WEIGHT
    db.session.commit()


def set_weight(carton: Carton, weight, unit="lb", user=None):
    try:
        value = float(weight)
    except (TypeError, ValueError) as exc:
        raise ProcessingError("Carton weight is required.") from exc
    if value <= 0:
        raise ProcessingError("Carton weight must be greater than 0.")
    old = carton.weight
    carton.previous_weight = old
    carton.weight = value
    carton.weight_unit = unit or "lb"
    carton.reweigh_required = False
    carton.status = CartonStatus.CLOSED
    carton.closed_at = datetime.utcnow()
    uid, uname = current_actor()
    carton.closed_by_user_id = uid
    carton.closed_by_username = uname
    record_audit(
        "CARTON_REWEIGHED" if old is not None else "CARTON_WEIGHT_RECORDED",
        module="Processing",
        entity_type="carton",
        entity_id=carton.id,
        client_id=carton.client_id,
        detail=f"{old} -> {value}",
    )
    record_audit(
        "CARTON_CLOSED",
        module="Processing",
        entity_type="carton",
        entity_id=carton.id,
        client_id=carton.client_id,
        detail=carton.carton_number,
    )
    db.session.commit()


def recall_carton(carton: Carton, user: User):
    order = db.session.get(Order, carton.order_id)
    require_owner(order, user)
    if carton.status != CartonStatus.CLOSED:
        raise ProcessingError("Only a closed carton can be recalled.")
    carton.status = CartonStatus.REWEIGH_REQUIRED
    carton.reweigh_required = True
    record_audit(
        "CARTON_RECALLED",
        module="Processing",
        entity_type="carton",
        entity_id=carton.id,
        client_id=order.client_id,
        detail=carton.carton_number,
    )
    db.session.commit()


def snapshot(order: Order, ticket: PickTicket) -> dict:
    lines = OrderLine.query.filter_by(order_id=order.id).all()
    reserved_locations = {
        u.location
        for u in InventoryUnit.query.filter_by(allocated_order_id=order.id).all()
    }
    return {
        "pick_ticket": ticket.pick_ticket_number,
        "order_number": order.wms_order_id,
        "client": order.client.client_code,
        "warehouse": order.warehouse.warehouse_code,
        "order_type": order.division.code,
        "customer": order.customer,
        "carrier": order.carrier,
        "shipping_service": order.shipping_service,
        "total_units": sum(l.qty_ordered for l in lines),
        "allocated_units": sum(l.qty_allocated for l in lines),
        "locations": len(reserved_locations),
        "status": order.status,
    }


def kpis(order: Order, current: Carton | None) -> dict:
    packed = InventoryUnit.query.filter_by(
        allocated_order_id=order.id, status=UnitStatus.PACKED
    ).count()
    reserved = InventoryUnit.query.filter_by(
        allocated_order_id=order.id, status=UnitStatus.RESERVED
    ).count()
    closed = Carton.query.filter_by(order_id=order.id, status=CartonStatus.CLOSED).all()
    closed_units = sum(CartonContent.query.filter_by(carton_id=c.id).count() for c in closed)
    current_units = CartonContent.query.filter_by(carton_id=current.id).count() if current else 0
    return {
        "total_scanned": packed,
        "remaining": reserved,
        "closed_cartons": len(closed),
        "units_in_closed": closed_units,
        "current_carton_units": current_units,
        "current_remaining": reserved,
    }


def can_close(order: Order) -> tuple[bool, str]:
    if order.status == OrderStatus.CLOSED:
        return False, CLOSED_PICK_TICKET_MESSAGE
    lines = OrderLine.query.filter_by(order_id=order.id).all()
    if not lines:
        return False, "Order has no lines."
    for line in lines:
        if not (line.qty_ordered == line.qty_allocated == line.qty_packed):
            return False, "Ordered, allocated, and packed quantities must match."
    if InventoryUnit.query.filter_by(allocated_order_id=order.id, status=UnitStatus.RESERVED).count():
        return False, "Remaining reserved units must be zero."
    cartons = Carton.query.filter_by(order_id=order.id).all()
    if not cartons:
        return False, "No cartons."
    for carton in cartons:
        if carton.status != CartonStatus.CLOSED:
            return False, "All cartons must be closed."
        if not dims_ready(carton) or carton.weight is None:
            return False, "All cartons need dimensions and current weights."
        if carton.reweigh_required:
            return False, "A carton requires reweigh."
    return True, ""


def close_order(order: Order, user: User):
    require_owner(order, user)
    if order.status == OrderStatus.CLOSED:
        raise ProcessingError(CLOSED_PICK_TICKET_MESSAGE)
    ok, reason = can_close(order)
    if not ok:
        raise ProcessingError(reason)
    uid, uname = current_actor()
    units = InventoryUnit.query.filter_by(
        allocated_order_id=order.id, status=UnitStatus.PACKED
    ).with_for_update().all()
    for unit in units:
        transition_unit(
            unit,
            to_status=UnitStatus.SHIPPED,
            transaction_type=LedgerType.SHIP,
            user_id=uid,
            order_id=order.id,
            carton_id=unit.carton_id,
            reference=f"close:{order.wms_order_id}",
        )
    for line in order.lines:
        line.qty_shipped = line.qty_packed
    order.status = OrderStatus.CLOSED
    order.closed_at = datetime.utcnow()
    order.closed_by_user_id = user.id
    order.closed_by_username = user.username
    release_lock(order, user, force=True)
    invoice = Invoice(
        invoice_number=f"INV-{order.wms_order_id}",
        order_id=order.id,
        client_id=order.client_id,
        warehouse_id=order.warehouse_id,
        division_id=order.division_id,
        customer=order.customer,
        carrier=order.carrier,
        shipping_service=order.shipping_service,
        total_units=sum(l.qty_ordered for l in order.lines),
        total_cartons=Carton.query.filter_by(order_id=order.id).count(),
        total_weight=sum((c.weight or 0) for c in Carton.query.filter_by(order_id=order.id)),
        created_by_user_id=user.id,
        created_by_username=user.username,
    )
    db.session.add(invoice)
    ticket = PickTicket.query.filter_by(order_id=order.id).first()
    if ticket:
        sync_ticket_status(ticket, order)
        record_audit(
            "PICK_TICKET_CLOSED",
            module="Processing",
            entity_type="pick_ticket",
            entity_id=ticket.id,
            client_id=order.client_id,
            detail=f"{ticket.pick_ticket_number} {order.wms_order_id}",
        )
    record_audit(
        "ORDER_RECONCILED",
        module="Processing",
        entity_type="order",
        entity_id=order.id,
        client_id=order.client_id,
        detail=f"{order.wms_order_id} {ticket.pick_ticket_number if ticket else ''}".strip(),
    )
    record_audit(
        "ORDER_CLOSED",
        module="Processing",
        entity_type="order",
        entity_id=order.id,
        client_id=order.client_id,
        detail=f"{order.wms_order_id} {ticket.pick_ticket_number if ticket else ''}".strip(),
    )
    document = Document(
        client_id=order.client_id,
        order_id=order.id,
        type="ORDER_CLOSURE",
        filename=f"{order.wms_order_id}-closure.pdf",
        storage_key=f"pending-closure-{order.id}",
        created_by_user_id=user.id,
        created_by_username=user.username,
    )
    db.session.add(document)
    db.session.commit()
    document = persist_closure_pdf(order)
    db.session.commit()
    return document
