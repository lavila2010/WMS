"""Order processing: exclusive lock, cartons, UPC scans, recall, close."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from uuid import uuid4

from sqlalchemy import select, text

from ..auth import current_actor, record_audit
from ..constants import (
    CLOSED_PICK_TICKET_MESSAGE,
    AllocationStatus,
    CartonStatus,
    InventoryIssueStatus,
    InventoryIssueType,
    LedgerType,
    OrderStatus,
    PickTicketStatus,
    ShippingLabelStatus,
    ShippingStatus,
    UnitStatus,
)
from ..extensions import db
from ..models import (
    Allocation,
    Carton,
    CartonContent,
    InventoryIssue,
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
from .fulfillment import open_ticket_for_order, order_quantities, refresh_order_status
from .packing_list import persist_packing_list_pdf
from .pick_tickets import desired_ticket_status, sync_ticket_status

LOCK_MESSAGE = "Order is currently being processed by another user."
CANCELLED_TICKET_MESSAGE = "Pick ticket is cancelled."
PROCESSABLE_ORDER_STATUSES = {OrderStatus.PICK_TICKET_READY, OrderStatus.PROCESSING}
SHORT_CLOSE_REASON = "Confirmed missing during Processing Close Short"
SHORT_CLOSE_CONFIRM_TEXT = (
    "I confirm that the listed units could not be physically located and "
    "this Pick Ticket should be closed short."
)


class ProcessingError(ValueError):
    pass


def assert_ticket_processable(ticket: PickTicket, order: Order | None = None) -> Order:
    order = order or db.session.get(Order, ticket.order_id)
    if ticket is None or order is None:
        raise ProcessingError("Pick ticket not found.")
    stored = ticket.status if ticket.status != "ACTIVE" else PickTicketStatus.OPEN
    derived = desired_ticket_status(order, ticket)
    if stored == PickTicketStatus.CLOSED or derived == PickTicketStatus.CLOSED:
        if order.status == OrderStatus.CLOSED:
            raise ProcessingError(CLOSED_PICK_TICKET_MESSAGE)
        raise ProcessingError(CLOSED_PICK_TICKET_MESSAGE)
    if (
        derived == PickTicketStatus.CANCELLED
        or stored == PickTicketStatus.CANCELLED
        or order.status == OrderStatus.CANCELLED
    ):
        raise ProcessingError(CANCELLED_TICKET_MESSAGE)
    if stored != PickTicketStatus.OPEN:
        raise ProcessingError(CLOSED_PICK_TICKET_MESSAGE)
    if order.status == OrderStatus.CLOSED:
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
    ticket = open_ticket_for_order(order) or PickTicket.query.filter_by(order_id=order.id).first()
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
    ticket = open_ticket_for_order(order) or PickTicket.query.filter_by(order_id=order.id).first()
    if ticket:
        assert_ticket_processable(ticket, order)
    current = (
        Carton.query.filter_by(order_id=order.id)
        .filter(Carton.status != CartonStatus.CLOSED)
        .order_by(Carton.id.desc())
        .first()
    )
    if current:
        if ticket and not current.pick_ticket_id:
            current.pick_ticket_id = ticket.id
        return current
    uid, uname = current_actor()
    carton = Carton(
        client_id=order.client_id,
        order_id=order.id,
        pick_ticket_id=ticket.id if ticket else None,
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
    units = reserved_ticket_units(order)
    grouped = defaultdict(
        lambda: {
            "qty": 0,
            "sku": None,
            "description": None,
            "style": None,
            "color": None,
            "size": None,
            "unit_ids": [],
        }
    )
    for unit in units:
        line = OrderLine.query.filter_by(order_id=order.id, upc=unit.upc).first()
        key = (unit.location, unit.upc, unit.sku, unit.style, unit.color, unit.size)
        grouped[key]["qty"] += 1
        grouped[key]["sku"] = unit.sku
        grouped[key]["description"] = unit.description or (line.description if line else None)
        grouped[key]["style"] = unit.style
        grouped[key]["color"] = unit.color
        grouped[key]["size"] = unit.size
        grouped[key]["unit_ids"].append(unit.id)
    return [
        {
            "location": loc,
            "upc": upc,
            "sku": meta["sku"],
            "description": meta["description"],
            "style": meta["style"],
            "color": meta["color"],
            "size": meta["size"],
            "qty": meta["qty"],
            "unit_ids": meta["unit_ids"],
        }
        for (loc, upc, _sku, _style, _color, _size), meta in sorted(grouped.items())
    ]


def reserved_ticket_units(order: Order, ticket: PickTicket | None = None) -> list[InventoryUnit]:
    ticket = ticket or open_ticket_for_order(order)
    query = InventoryUnit.query.filter_by(
        allocated_order_id=order.id, status=UnitStatus.RESERVED
    )
    if ticket:
        query = query.join(Allocation, Allocation.inventory_unit_id == InventoryUnit.id).filter(
            Allocation.pick_ticket_id == ticket.id,
            Allocation.status == AllocationStatus.ACTIVE,
        )
    return query.order_by(InventoryUnit.location.asc(), InventoryUnit.upc.asc(), InventoryUnit.id.asc()).all()


def ticket_packed_count(order: Order, ticket: PickTicket | None = None) -> int:
    ticket = ticket or open_ticket_for_order(order)
    query = (
        db.session.query(InventoryUnit)
        .join(Allocation, Allocation.inventory_unit_id == InventoryUnit.id)
        .filter(
            Allocation.status == AllocationStatus.ACTIVE,
            InventoryUnit.status == UnitStatus.PACKED,
        )
    )
    if ticket:
        query = query.filter(Allocation.pick_ticket_id == ticket.id)
    else:
        query = query.filter(InventoryUnit.allocated_order_id == order.id)
    return query.count()


def shortage_summary(order: Order) -> dict:
    ticket = open_ticket_for_order(order)
    missing_units = reserved_ticket_units(order, ticket)
    packed = ticket_packed_count(order, ticket)
    expected = packed + len(missing_units)
    qty = order_quantities(order)
    remaining_after = qty["remaining"]
    return {
        "ticket": ticket,
        "expected": expected,
        "packed": packed,
        "missing": len(missing_units),
        "rows": remaining_rows(order),
        "units": missing_units,
        "close_entire_order": remaining_after == 0,
        "remaining_after": remaining_after,
        "ordered": qty["ordered"],
        "shipped": qty["shipped"],
        "short": qty["short"],
    }


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
    ticket = open_ticket_for_order(order) or PickTicket.query.filter_by(order_id=order.id).first()
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
    if ticket:
        stmt = (
            select(InventoryUnit)
            .join(Allocation, Allocation.inventory_unit_id == InventoryUnit.id)
            .where(
                InventoryUnit.client_id == order.client_id,
                InventoryUnit.warehouse_id == order.warehouse_id,
                InventoryUnit.allocated_order_id == order.id,
                InventoryUnit.upc == upc,
                InventoryUnit.status == UnitStatus.RESERVED,
                Allocation.pick_ticket_id == ticket.id,
                Allocation.status == AllocationStatus.ACTIVE,
            )
            .order_by(InventoryUnit.location.asc(), InventoryUnit.id.asc())
            .with_for_update(of=InventoryUnit)
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
    ticket = open_ticket_for_order(order)
    if ticket is None:
        return False, "No open pick ticket."
    reserved = (
        db.session.query(InventoryUnit)
        .join(Allocation, Allocation.inventory_unit_id == InventoryUnit.id)
        .filter(
            Allocation.pick_ticket_id == ticket.id,
            Allocation.status == AllocationStatus.ACTIVE,
            InventoryUnit.status == UnitStatus.RESERVED,
        )
        .count()
    )
    if reserved:
        return False, "Remaining reserved units must be zero."
    packed = (
        db.session.query(InventoryUnit)
        .join(Allocation, Allocation.inventory_unit_id == InventoryUnit.id)
        .filter(
            Allocation.pick_ticket_id == ticket.id,
            Allocation.status == AllocationStatus.ACTIVE,
            InventoryUnit.status == UnitStatus.PACKED,
        )
        .count()
    )
    if packed <= 0:
        return False, "No packed units on this pick ticket."
    cartons = Carton.query.filter(
        Carton.order_id == order.id,
        (Carton.pick_ticket_id == ticket.id) | (Carton.pick_ticket_id.is_(None)),
    ).all()
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


def packed_cartons_ready(order: Order, ticket: PickTicket | None = None) -> tuple[bool, str]:
    ticket = ticket or open_ticket_for_order(order)
    cartons = Carton.query.filter(
        Carton.order_id == order.id,
        (Carton.pick_ticket_id == ticket.id) | (Carton.pick_ticket_id.is_(None)) if ticket else Carton.order_id == order.id,
    ).all()
    packed_cartons = [carton for carton in cartons if CartonContent.query.filter_by(carton_id=carton.id).count() > 0]
    if not packed_cartons:
        return False, "No cartons."
    for carton in packed_cartons:
        if carton.status != CartonStatus.CLOSED:
            return False, "All cartons must be closed."
        if not dims_ready(carton) or carton.weight is None:
            return False, "All cartons need dimensions and current weights."
        if carton.reweigh_required:
            return False, "A carton requires reweigh."
    return True, ""


def can_close_short(order: Order, user: User | None = None) -> tuple[bool, str]:
    if order.status == OrderStatus.CLOSED:
        return False, CLOSED_PICK_TICKET_MESSAGE
    if user is not None:
        try:
            require_owner(order, user)
        except ProcessingError as exc:
            return False, str(exc)
    ticket = open_ticket_for_order(order)
    if ticket is None:
        return False, "No open pick ticket."
    stored = ticket.status if ticket.status != "ACTIVE" else PickTicketStatus.OPEN
    if stored != PickTicketStatus.OPEN:
        return False, CLOSED_PICK_TICKET_MESSAGE
    packed = ticket_packed_count(order, ticket)
    if packed <= 0:
        return False, "At least one unit must be packed before closing short."
    reserved = reserved_ticket_units(order, ticket)
    if not reserved:
        return False, "No reserved units remain to close short."
    for unit in reserved:
        if unit.status != UnitStatus.RESERVED:
            return False, "Missing units must still be reserved on this pick ticket."
        if unit.carton_id:
            return False, "Do not allow short closure if the remaining unit is packed in another carton."
        if unit.client_id != order.client_id or unit.warehouse_id != order.warehouse_id:
            return False, "Missing units must belong to this client and warehouse."
        if unit.allocated_order_id != order.id:
            return False, "Missing units must belong to this order."
    ok, reason = packed_cartons_ready(order, ticket)
    if not ok:
        return False, reason
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
    ticket = open_ticket_for_order(order)
    for line in order.lines:
        line.qty_shipped = line.qty_packed
    if ticket:
        ticket.status = PickTicketStatus.CLOSED
        record_audit(
            "PICK_TICKET_CLOSED",
            module="Processing",
            entity_type="pick_ticket",
            entity_id=ticket.id,
            client_id=order.client_id,
            detail=f"{ticket.pick_ticket_number} {order.wms_order_id}",
        )
    qty = order_quantities(order)
    remaining = qty["remaining"]
    short = qty["short"]
    fully = qty["ordered"] > 0 and qty["shipped"] >= qty["ordered"] and remaining == 0 and short == 0
    short_complete = (
        qty["ordered"] > 0
        and remaining == 0
        and (qty["shipped"] + short) >= qty["ordered"]
        and short > 0
    )
    close_now = fully or short_complete
    release_lock(order, user, force=True)
    if close_now:
        order.status = OrderStatus.CLOSED
        order.closed_at = datetime.utcnow()
        order.closed_by_user_id = user.id
        order.closed_by_username = user.username
        order.shipping_status = ShippingStatus.PENDING_TRACKING
        if short_complete:
            order.short_closed = True
            order.short_qty = short
            order.short_closed_at = order.closed_at
            order.short_closed_by_user_id = user.id
            order.short_close_reason = order.short_close_reason or SHORT_CLOSE_REASON
        for carton in Carton.query.filter_by(order_id=order.id):
            if not carton.shipping_label_status:
                carton.shipping_label_status = ShippingLabelStatus.PENDING
        if Invoice.query.filter_by(order_id=order.id).first() is None:
            invoice = Invoice(
                invoice_number=f"INV-{order.wms_order_id}",
                order_id=order.id,
                client_id=order.client_id,
                warehouse_id=order.warehouse_id,
                division_id=order.division_id,
                customer=order.customer,
                carrier=order.carrier,
                shipping_service=order.shipping_service,
                total_units=sum(l.qty_shipped for l in order.lines) if short_complete else sum(l.qty_ordered for l in order.lines),
                total_cartons=Carton.query.filter_by(order_id=order.id).count(),
                total_weight=sum((c.weight or 0) for c in Carton.query.filter_by(order_id=order.id)),
                created_by_user_id=user.id,
                created_by_username=user.username,
            )
            db.session.add(invoice)
        record_audit(
            "ORDER_RECONCILED",
            module="Processing",
            entity_type="order",
            entity_id=order.id,
            client_id=order.client_id,
            detail=f"{order.wms_order_id} {ticket.pick_ticket_number if ticket else ''}".strip(),
        )
        if fully:
            record_audit(
                "ORDER_FULLY_FULFILLED",
                module="Processing",
                entity_type="order",
                entity_id=order.id,
                client_id=order.client_id,
                detail=order.wms_order_id,
            )
        else:
            record_audit(
                "ORDER_CLOSED_SHORT",
                module="Processing",
                entity_type="order",
                entity_id=order.id,
                client_id=order.client_id,
                detail=f"{order.wms_order_id} shipped={qty['shipped']} short={short}",
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
    else:
        order.status = OrderStatus.PARTIALLY_FULFILLED
        refresh_order_status(order)
        record_audit(
            "PARTIAL_FULFILLMENT_COMPLETED",
            module="Processing",
            entity_type="order",
            entity_id=order.id,
            client_id=order.client_id,
            detail=f"{order.wms_order_id} shipped={qty['shipped']} remaining={qty['remaining']}",
        )
        document = None
    packing_doc = Document(
        client_id=order.client_id,
        order_id=order.id,
        pick_ticket_id=ticket.id if ticket else None,
        type="PACKING_LIST",
        filename=f"{order.wms_order_id}-{(ticket.pick_ticket_number if ticket else 'packing')}-packing-list.pdf",
        storage_key=f"pending-packing-{order.id}-{ticket.id if ticket else 0}",
        created_by_user_id=user.id,
        created_by_username=user.username,
    )
    db.session.add(packing_doc)
    db.session.commit()
    if close_now:
        document = persist_closure_pdf(order)
        persist_packing_list_pdf(order, ticket=ticket)
    else:
        persist_packing_list_pdf(order, ticket=ticket, reuse=False)
    db.session.commit()
    return document


def close_order_short(
    order: Order,
    user: User,
    *,
    confirmed: bool = False,
    close_entire_order: bool | None = None,
    _fail_after: int | None = None,
):
    """Explicit exception path: convert remaining RESERVED ticket units to MISSING and close short."""
    if not confirmed:
        raise ProcessingError("Close Short requires explicit confirmation.")
    try:
        return _close_order_short_inner(
            order,
            user,
            close_entire_order=close_entire_order,
            _fail_after=_fail_after,
        )
    except Exception:
        db.session.rollback()
        raise


def _close_order_short_inner(
    order: Order,
    user: User,
    *,
    close_entire_order: bool | None = None,
    _fail_after: int | None = None,
):
    require_owner(order, user)
    locked_order = db.session.execute(
        select(Order).where(Order.id == order.id).with_for_update()
    ).scalar_one()
    ticket = open_ticket_for_order(locked_order)
    if ticket is None:
        raise ProcessingError("No open pick ticket.")
    locked_ticket = db.session.execute(
        select(PickTicket).where(PickTicket.id == ticket.id).with_for_update()
    ).scalar_one()
    ok, reason = can_close_short(locked_order, user)
    if not ok:
        raise ProcessingError(reason)

    missing_pairs = db.session.execute(
        select(InventoryUnit, Allocation)
        .join(Allocation, Allocation.inventory_unit_id == InventoryUnit.id)
        .where(
            Allocation.pick_ticket_id == locked_ticket.id,
            Allocation.status == AllocationStatus.ACTIVE,
            InventoryUnit.status == UnitStatus.RESERVED,
            InventoryUnit.allocated_order_id == locked_order.id,
            InventoryUnit.client_id == locked_order.client_id,
            InventoryUnit.warehouse_id == locked_order.warehouse_id,
        )
        .order_by(InventoryUnit.id.asc())
        .with_for_update(of=[InventoryUnit, Allocation])
    ).all()
    if not missing_pairs:
        raise ProcessingError("No reserved units remain to close short.")

    packed_units = db.session.execute(
        select(InventoryUnit)
        .join(Allocation, Allocation.inventory_unit_id == InventoryUnit.id)
        .where(
            Allocation.pick_ticket_id == locked_ticket.id,
            Allocation.status == AllocationStatus.ACTIVE,
            InventoryUnit.status == UnitStatus.PACKED,
        )
        .order_by(InventoryUnit.id.asc())
        .with_for_update(of=InventoryUnit)
    ).scalars().all()
    if not packed_units:
        raise ProcessingError("At least one unit must be packed before closing short.")

    uid, uname = current_actor()
    now = datetime.utcnow()
    reference = f"processing-close-short:{locked_ticket.pick_ticket_number}"
    converted = 0
    missing_ids = []
    for unit, allocation in missing_pairs:
        if unit.status != UnitStatus.RESERVED or allocation.status != AllocationStatus.ACTIVE:
            raise ProcessingError("A reserved unit changed before Close Short could complete.")
        if unit.carton_id or CartonContent.query.filter_by(inventory_unit_id=unit.id).first():
            raise ProcessingError("Do not allow short closure if the remaining unit is packed in another carton.")
        if (
            unit.client_id != locked_order.client_id
            or unit.warehouse_id != locked_order.warehouse_id
            or unit.allocated_order_id != locked_order.id
            or allocation.order_id != locked_order.id
            or allocation.pick_ticket_id != locked_ticket.id
        ):
            raise ProcessingError("Missing units must belong to this pick ticket.")
        line = db.session.get(OrderLine, allocation.order_line_id)
        if line is None:
            line = OrderLine.query.filter_by(order_id=locked_order.id, upc=unit.upc).first()
        if line is None:
            raise ProcessingError("Missing unit has no order line.")
        transition_unit(
            unit,
            to_status=UnitStatus.ISSUE_HOLD,
            transaction_type=LedgerType.ISSUE_HOLD,
            user_id=uid,
            order_id=locked_order.id,
            order_line_id=line.id,
            allocation_id=allocation.id,
            pick_ticket_id=locked_ticket.id,
            reference=reference,
        )
        transition_unit(
            unit,
            to_status=UnitStatus.MISSING,
            transaction_type=LedgerType.MISSING_CONFIRMED,
            user_id=uid,
            order_id=locked_order.id,
            order_line_id=line.id,
            allocation_id=allocation.id,
            pick_ticket_id=locked_ticket.id,
            clear_reserved_order=True,
            reference=reference,
        )
        allocation.status = AllocationStatus.MISSING
        line.qty_short = int(line.qty_short or 0) + 1
        issue = InventoryIssue(
            client_id=unit.client_id,
            warehouse_id=unit.warehouse_id,
            inventory_unit_id=unit.id,
            upc=unit.upc,
            original_location=unit.location,
            current_location=unit.location,
            source_order_id=locked_order.id,
            source_order_line_id=line.id,
            source_pick_ticket_id=locked_ticket.id,
            issue_type=InventoryIssueType.PICK_UNIT_NOT_FOUND,
            status=InventoryIssueStatus.MISSING,
            reported_by_user_id=user.id,
            reported_at=now,
            resolved_by_user_id=user.id,
            resolved_at=now,
            resolution_note=SHORT_CLOSE_REASON,
        )
        db.session.add(issue)
        db.session.flush()
        record_audit(
            "INVENTORY_ISSUE_CREATED",
            module="Inventory",
            entity_type="inventory_issue",
            entity_id=issue.id,
            client_id=locked_order.client_id,
            detail=f"unit={unit.id} upc={unit.upc} ticket={locked_ticket.pick_ticket_number}",
        )
        record_audit(
            "INVENTORY_UNIT_MISSING",
            module="Inventory",
            entity_type="inventory_unit",
            entity_id=unit.id,
            client_id=locked_order.client_id,
            detail=f"issue={issue.id} ticket={locked_ticket.pick_ticket_number}",
        )
        converted += 1
        missing_ids.append(unit.id)
        if _fail_after is not None and converted >= _fail_after:
            raise ProcessingError("injected failure")

    for unit in packed_units:
        transition_unit(
            unit,
            to_status=UnitStatus.SHIPPED,
            transaction_type=LedgerType.SHIP,
            user_id=uid,
            order_id=locked_order.id,
            carton_id=unit.carton_id,
            reference=f"close:{locked_order.wms_order_id}",
        )
    for line in locked_order.lines:
        line.qty_shipped = line.qty_packed

    empty_cartons = [
        carton
        for carton in Carton.query.filter_by(order_id=locked_order.id).all()
        if CartonContent.query.filter_by(carton_id=carton.id).count() == 0
        and carton.status != CartonStatus.CLOSED
    ]
    for carton in empty_cartons:
        db.session.delete(carton)

    expected_qty = len(packed_units) + converted
    locked_ticket.status = PickTicketStatus.CLOSED
    locked_ticket.expected_qty = expected_qty
    locked_ticket.packed_qty = len(packed_units)
    locked_ticket.short_qty = converted
    locked_ticket.short_closed = True
    locked_order.short_qty = int(locked_order.short_qty or 0) + converted
    record_audit(
        "PICK_TICKET_CLOSED_SHORT",
        module="Processing",
        entity_type="pick_ticket",
        entity_id=locked_ticket.id,
        client_id=locked_order.client_id,
        detail=f"ticket={locked_ticket.pick_ticket_number} expected={expected_qty} shipped={len(packed_units)} short={converted}",
    )
    record_audit(
        "PROCESSING_SHORT_CLOSE_CONFIRMED",
        module="Processing",
        entity_type="order",
        entity_id=locked_order.id,
        client_id=locked_order.client_id,
        detail=f"{locked_order.wms_order_id} {locked_ticket.pick_ticket_number} short={converted}",
    )

    db.session.flush()
    qty = order_quantities(locked_order)
    remaining = qty["remaining"]
    short_complete = remaining == 0 and (qty["shipped"] + qty["short"]) >= qty["ordered"]
    if close_entire_order is False and short_complete:
        raise ProcessingError("Remaining ordered quantity is zero; Close Entire Order Short is required.")
    if close_entire_order is True and not short_complete:
        raise ProcessingError("Unallocated remaining quantity exists; close only the current Pick Ticket short.")
    close_order_now = short_complete if close_entire_order is None else bool(close_entire_order)

    release_lock(locked_order, user, force=True)
    document = None
    if close_order_now:
        locked_order.status = OrderStatus.CLOSED
        locked_order.closed_at = now
        locked_order.closed_by_user_id = user.id
        locked_order.closed_by_username = user.username
        locked_order.shipping_status = ShippingStatus.PENDING_TRACKING
        locked_order.short_closed = True
        locked_order.short_qty = qty["short"]
        locked_order.short_closed_at = now
        locked_order.short_closed_by_user_id = user.id
        locked_order.short_close_reason = SHORT_CLOSE_REASON
        for carton in Carton.query.filter_by(order_id=locked_order.id):
            if not carton.shipping_label_status:
                carton.shipping_label_status = ShippingLabelStatus.PENDING
        if Invoice.query.filter_by(order_id=locked_order.id).first() is None:
            invoice = Invoice(
                invoice_number=f"INV-{locked_order.wms_order_id}",
                order_id=locked_order.id,
                client_id=locked_order.client_id,
                warehouse_id=locked_order.warehouse_id,
                division_id=locked_order.division_id,
                customer=locked_order.customer,
                carrier=locked_order.carrier,
                shipping_service=locked_order.shipping_service,
                total_units=sum(l.qty_shipped for l in locked_order.lines),
                total_cartons=Carton.query.filter_by(order_id=locked_order.id).count(),
                total_weight=sum((c.weight or 0) for c in Carton.query.filter_by(order_id=locked_order.id)),
                created_by_user_id=user.id,
                created_by_username=user.username,
            )
            db.session.add(invoice)
        record_audit(
            "ORDER_CLOSED_SHORT",
            module="Processing",
            entity_type="order",
            entity_id=locked_order.id,
            client_id=locked_order.client_id,
            detail=f"{locked_order.wms_order_id} shipped={qty['shipped']} short={qty['short']}",
        )
        record_audit(
            "ORDER_CLOSED",
            module="Processing",
            entity_type="order",
            entity_id=locked_order.id,
            client_id=locked_order.client_id,
            detail=f"{locked_order.wms_order_id} {locked_ticket.pick_ticket_number}",
        )
        document = Document(
            client_id=locked_order.client_id,
            order_id=locked_order.id,
            type="ORDER_CLOSURE",
            filename=f"{locked_order.wms_order_id}-closure.pdf",
            storage_key=f"pending-closure-{locked_order.id}",
            created_by_user_id=user.id,
            created_by_username=user.username,
        )
        db.session.add(document)
    else:
        locked_order.status = OrderStatus.PARTIALLY_FULFILLED
        refresh_order_status(locked_order)
        record_audit(
            "PARTIAL_FULFILLMENT_COMPLETED",
            module="Processing",
            entity_type="order",
            entity_id=locked_order.id,
            client_id=locked_order.client_id,
            detail=f"{locked_order.wms_order_id} shipped={qty['shipped']} remaining={qty['remaining']} short={qty['short']}",
        )

    packing_doc = Document(
        client_id=locked_order.client_id,
        order_id=locked_order.id,
        pick_ticket_id=locked_ticket.id,
        type="PACKING_LIST",
        filename=f"{locked_order.wms_order_id}-{locked_ticket.pick_ticket_number}-packing-list.pdf",
        storage_key=f"pending-packing-{locked_order.id}-{locked_ticket.id}",
        created_by_user_id=user.id,
        created_by_username=user.username,
    )
    db.session.add(packing_doc)
    db.session.commit()
    if close_order_now:
        document = persist_closure_pdf(locked_order)
        persist_packing_list_pdf(locked_order, ticket=locked_ticket)
    else:
        persist_packing_list_pdf(locked_order, ticket=locked_ticket, reuse=False)
    db.session.commit()
    return document
