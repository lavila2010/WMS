"""Order Processing station: pick-ticket lookup, UPC packing, cartons, close.

UPC is the operational scan key. One UPC scan consumes exactly one remaining
allocated unit. Inventory scope is Client + Warehouse only (never Order Type).
"""

from __future__ import annotations

from datetime import datetime

from ..auth import current_actor
from ..constants import (
    AllocationStatus,
    BoxStatus,
    ExceptionType,
    MovementType,
    OrderStatus,
    PickTicketStatus,
    ProcessingEvent,
    UnitStatus,
)
from ..extensions import db
from ..models import (
    Allocation,
    Box,
    BoxContent,
    InventoryUnit,
    Order,
    PickTicket,
    Transaction,
)
from ..workflow import transition
from .allocation import (
    BarcodeError,
    active_allocations,
    allocation_progress,
    is_fully_allocated,
)
from .movements import record_movement
from .packing import (
    create_box,
    packed_count,
    reconcile,
    scan_into_box,
)
from .pick_tickets import unique_locations


_PROCESSABLE = {
    OrderStatus.ALLOCATED,
    OrderStatus.READY_TO_PICK,
    OrderStatus.PROCESSING,
}

_SCANNABLE = {BoxStatus.OPEN, BoxStatus.REWEIGH_REQUIRED}


class ProcessingError(Exception):
    """Operational rejection on the processing station."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _actor(user=None):
    if user is not None:
        return user.id, user.username
    return current_actor()


def _user_id(user=None):
    if user is not None:
        return user.id
    uid, _ = current_actor()
    return uid


def record_event(order: Order, event_type: str, *, box=None, unit=None, detail=None, user=None):
    uid, uname = _actor(user)
    db.session.add(
        Transaction(
            type=event_type,
            order_id=order.id,
            inventory_unit_id=unit.id if unit is not None else None,
            barcode=unit.barcode if unit is not None else None,
            quantity=1 if unit is not None else None,
            detail=detail,
            user_id=uid,
            username=uname,
        )
    )
    db.session.flush()


def normalize_upc(raw: str) -> str:
    upc = (raw or "").strip()
    if not upc:
        raise ProcessingError(ExceptionType.UNKNOWN_UPC, "Enter or scan a UPC.")
    return upc


def _fmt_num(value) -> str:
    if value is None:
        return "—"
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:g}"


def carton_index(box: Box) -> int:
    try:
        return int(box.box_number.rsplit("BOX", 1)[1])
    except (IndexError, ValueError):
        return len(box.order.boxes)


def next_carton_number(order: Order) -> str:
    return f"{order.order_number}-BOX{len(order.boxes) + 1:02d}"


def dims_confirmed(box: Box) -> bool:
    return (
        box.length_cm is not None
        and box.width_cm is not None
        and box.height_cm is not None
        and box.length_cm > 0
        and box.width_cm > 0
        and box.height_cm > 0
    )


def format_dims(box: Box) -> str:
    if not dims_confirmed(box):
        return "—"
    unit = box.dimension_unit or "in"
    return f"{_fmt_num(box.length_cm)} x {_fmt_num(box.width_cm)} x {_fmt_num(box.height_cm)} {unit}"


def format_weight(box: Box) -> str:
    if box.weight_kg is None:
        return "—"
    return f"{_fmt_num(box.weight_kg)} {box.weight_unit or 'lb'}"


def order_snapshot(order: Order) -> dict:
    progress = allocation_progress(order)
    ticket = getattr(order, "pick_ticket", None)
    return {
        "pick_ticket": ticket.pick_ticket_number if ticket else "—",
        "order_number": order.order_number,
        "client": order.client.code if order.client else "—",
        "warehouse": order.warehouse.code if order.warehouse else "—",
        "order_type": order.order_type.code if order.order_type else "—",
        "customer": order.customer or "—",
        "carrier": order.carrier or "—",
        "shipping_service": order.shipping_service or "—",
        "total_units": progress["total_ordered"],
        "allocated_units": progress["total_allocated"],
        "locations": unique_locations(order),
        "status": order.status,
    }


def locked_by_other(order: Order, user=None) -> bool:
    if not order.processing_user_id:
        return False
    return order.processing_user_id != _user_id(user)


def assert_lock(order: Order, user=None) -> None:
    if not order.processing_user_id:
        raise ProcessingError(
            ExceptionType.ORDER_LOCKED,
            "This order is not in an active processing session.",
        )
    if locked_by_other(order, user):
        raise ProcessingError(
            ExceptionType.ORDER_LOCKED,
            f"Order {order.order_number} is locked by {order.processing_username}.",
        )


def acquire_lock(order: Order, user=None) -> None:
    if locked_by_other(order, user):
        raise ProcessingError(
            ExceptionType.ORDER_LOCKED,
            f"Order {order.order_number} is already being processed by "
            f"{order.processing_username}.",
        )
    uid, uname = _actor(user)
    order.processing_user_id = uid
    order.processing_username = uname
    if order.processing_started_at is None:
        order.processing_started_at = datetime.utcnow()


def release_lock(order: Order) -> None:
    order.processing_user_id = None
    order.processing_username = None
    order.processing_started_at = None


def lookup_pick_ticket(number: str, user=None) -> tuple[PickTicket, Order]:
    number = (number or "").strip()
    if not number:
        raise ProcessingError("INVALID_PICK_TICKET", "Enter a Pick Ticket number.")

    ticket = PickTicket.query.filter_by(pick_ticket_number=number).first()
    if ticket is None:
        raise ProcessingError(
            "INVALID_PICK_TICKET",
            f"Pick Ticket {number} was not found.",
        )
    if ticket.status != PickTicketStatus.ACTIVE:
        raise ProcessingError(
            "INACTIVE_PICK_TICKET",
            f"Pick Ticket {ticket.pick_ticket_number} is not active.",
        )
    order = ticket.order
    if order is None:
        raise ProcessingError(
            "MISSING_ORDER",
            f"Pick Ticket {ticket.pick_ticket_number} has no associated order.",
        )
    if order.status == OrderStatus.CLOSED:
        raise ProcessingError(
            "ORDER_CLOSED",
            f"Order {order.order_number} is CLOSED and cannot be processed.",
        )
    if not is_fully_allocated(order):
        raise ProcessingError(
            "PARTIAL_ALLOCATION",
            f"Order {order.order_number} is not fully allocated and cannot be processed.",
        )
    if order.status not in _PROCESSABLE:
        raise ProcessingError(
            "NOT_ELIGIBLE",
            f"Order {order.order_number} is not eligible for processing "
            f"(status {order.status}).",
        )
    if locked_by_other(order, user):
        raise ProcessingError(
            ExceptionType.ORDER_LOCKED,
            f"Order {order.order_number} is locked by {order.processing_username}.",
        )
    return ticket, order


def _walk_to_processing(order: Order) -> None:
    if order.status == OrderStatus.ALLOCATED:
        transition(order, OrderStatus.READY_TO_PICK, "Released to picking.")
    if order.status == OrderStatus.READY_TO_PICK:
        transition(order, OrderStatus.PROCESSING, "Processing started.")
    if order.status != OrderStatus.PROCESSING:
        raise ProcessingError(
            "NOT_ELIGIBLE",
            f"Order {order.order_number} cannot enter processing from {order.status}.",
        )


def create_processing_carton(order: Order, user=None) -> Box:
    box = create_box(order, next_carton_number(order))
    box.dimension_unit = box.dimension_unit or "in"
    box.weight_unit = box.weight_unit or "lb"
    box.reweigh_required = False
    record_event(
        order,
        ProcessingEvent.CARTON_CREATED,
        box=box,
        detail=box.box_number,
        user=user,
    )
    return box


def confirm_order(order: Order, user=None) -> Order:
    if order.status == OrderStatus.CLOSED:
        raise ProcessingError("ORDER_CLOSED", f"Order {order.order_number} is CLOSED.")
    if not is_fully_allocated(order):
        raise ProcessingError(
            "PARTIAL_ALLOCATION",
            f"Order {order.order_number} is not fully allocated.",
        )
    acquire_lock(order, user)
    _walk_to_processing(order)
    if not order.boxes:
        create_processing_carton(order, user=user)
    record_event(
        order,
        ProcessingEvent.PROCESSING_STARTED,
        detail=f"Locked by {order.processing_username}",
        user=user,
    )
    db.session.flush()
    return order


def cancel_session(order: Order, user=None) -> None:
    assert_lock(order, user)
    record_event(
        order,
        ProcessingEvent.PROCESSING_CANCELLED,
        detail="Processing session cancelled; lock released.",
        user=user,
    )
    release_lock(order)
    db.session.flush()


def packed_unit_ids(order: Order) -> set[int]:
    rows = (
        db.session.query(BoxContent.inventory_unit_id)
        .join(Box, BoxContent.box_id == Box.id)
        .filter(Box.order_id == order.id)
        .all()
    )
    return {row[0] for row in rows}


def remaining_units(order: Order) -> list[InventoryUnit]:
    packed = packed_unit_ids(order)
    units = []
    for alloc in (
        Allocation.query.filter_by(order_id=order.id, status=AllocationStatus.ACTIVE)
        .all()
    ):
        if alloc.inventory_unit_id not in packed:
            units.append(alloc.unit)
    units.sort(key=lambda u: (u.location or "", u.sku, u.upc or "", u.barcode, u.id))
    return units


def remaining_unit_rows(order: Order) -> list[dict]:
    groups: dict[tuple, list[InventoryUnit]] = {}
    for unit in remaining_units(order):
        key = (unit.location or "", unit.sku, unit.upc or "", unit.description or "")
        groups.setdefault(key, []).append(unit)
    rows = []
    for (location, sku, upc, description), items in groups.items():
        rows.append(
            {
                "location": location,
                "sku": sku,
                "upc": upc,
                "description": description,
                "qty": len(items),
            }
        )
    rows.sort(key=lambda r: (r["location"], r["sku"], r["upc"]))
    return rows


def find_one_remaining_by_upc(order: Order, upc: str) -> InventoryUnit:
    matches = [unit for unit in remaining_units(order) if (unit.upc or "") == upc]
    if matches:
        return matches[0]
    allocated_upcs = {(alloc.unit.upc or "") for alloc in active_allocations(order)}
    if upc in allocated_upcs:
        raise BarcodeError(
            ExceptionType.NO_REMAINING_UPC,
            f"UPC {upc} has no remaining allocated units on this order.",
        )
    raise BarcodeError(
        ExceptionType.UNKNOWN_UPC,
        f"UPC {upc} is not among the remaining allocated units for this order.",
    )


def current_carton(order: Order, focus_box_id: int | None = None) -> Box | None:
    if focus_box_id:
        box = Box.query.filter_by(id=focus_box_id, order_id=order.id).first()
        if box is not None:
            return box
    incomplete = [b for b in sorted(order.boxes, key=lambda b: b.id) if b.status != BoxStatus.CLOSED]
    if not incomplete:
        return None
    reweigh = [b for b in incomplete if b.status == BoxStatus.REWEIGH_REQUIRED]
    if reweigh:
        return reweigh[-1]
    awaiting = [b for b in incomplete if b.status == BoxStatus.AWAITING_WEIGHT]
    if awaiting:
        return awaiting[-1]
    recalled = [
        b for b in incomplete
        if b.status == BoxStatus.OPEN and (b.weight_kg is not None or b.contents)
    ]
    if recalled:
        return recalled[-1]
    return incomplete[-1]


def carton_content_rows(box: Box) -> list[dict]:
    groups: dict[tuple, list[BoxContent]] = {}
    for content in box.contents:
        unit = content.unit
        key = (unit.upc or "", unit.sku, unit.description or "", unit.location or "")
        groups.setdefault(key, []).append(content)
    rows = []
    for (upc, sku, description, location), items in groups.items():
        items.sort(key=lambda c: c.id)
        rows.append(
            {
                "upc": upc,
                "sku": sku,
                "description": description,
                "location": location,
                "qty": len(items),
                "remove_id": items[-1].id,
            }
        )
    rows.sort(key=lambda r: (r["upc"], r["sku"]))
    return rows


def station_kpis(order: Order, current: Box | None = None) -> dict:
    packed = packed_count(order)
    ordered = order.ordered_quantity
    remaining = max(0, ordered - packed)
    closed = [b for b in order.boxes if b.status == BoxStatus.CLOSED]
    return {
        "total_scanned": packed,
        "remaining": remaining,
        "closed_cartons": len(closed),
        "units_in_closed": sum(len(b.contents) for b in closed),
        "current_carton_units": len(current.contents) if current is not None else 0,
        "current_remaining": remaining,
    }


def confirm_dimensions(box: Box, length, width, height, unit: str = "in", user=None) -> Box:
    assert_lock(box.order, user)
    try:
        length_f = float(length)
        width_f = float(width)
        height_f = float(height)
    except (TypeError, ValueError) as exc:
        raise ProcessingError("DIMENSIONS_REQUIRED", "Length, width and height are required.") from exc
    if length_f <= 0 or width_f <= 0 or height_f <= 0:
        raise ProcessingError("DIMENSIONS_REQUIRED", "Length, width and height must be greater than zero.")
    box.length_cm = length_f
    box.width_cm = width_f
    box.height_cm = height_f
    box.dimension_unit = (unit or "in").strip() or "in"
    db.session.flush()
    return box


def _assert_scan_ready(box: Box, user=None) -> Order:
    order = box.order
    if order is None:
        raise ProcessingError("MISSING_ORDER", "Carton is not attached to an order.")
    assert_lock(order, user)
    if box.status not in _SCANNABLE:
        raise BarcodeError(
            ExceptionType.CARTON_NOT_OPEN,
            f"Carton {box.box_number} is not open for scanning.",
        )
    if not dims_confirmed(box):
        raise BarcodeError(
            ExceptionType.CARTON_NEEDS_DIMENSIONS,
            f"Confirm dimensions for Carton {carton_index(box)} before scanning.",
        )
    return order


def scan_upc_into_box(box: Box, raw_upc: str, user=None, *, correction: bool = False) -> BoxContent:
    """Consume exactly one remaining allocated unit for ``raw_upc``."""
    order = _assert_scan_ready(box, user)
    upc = normalize_upc(raw_upc)
    unit = find_one_remaining_by_upc(order, upc)
    if unit.client_id != order.client_id:
        raise BarcodeError(ExceptionType.WRONG_CLIENT, "UPC belongs to a different client.")
    if unit.warehouse_id != order.warehouse_id:
        raise BarcodeError(ExceptionType.WRONG_WAREHOUSE, "UPC belongs to a different warehouse.")
    content = scan_into_box(box, unit.barcode)
    event = ProcessingEvent.CARTON_UNIT_ADDED if (correction or box.weight_kg is not None) else ProcessingEvent.UNIT_SCAN
    record_event(
        order,
        event,
        box=box,
        unit=unit,
        detail=f"UPC {upc} → {box.box_number}",
        user=user,
    )
    if correction or box.weight_kg is not None:
        _invalidate_weight(box, user=user)
    db.session.flush()
    return content


def request_close_carton(box: Box, user=None) -> Box:
    assert_lock(box.order, user)
    if not box.contents:
        raise ProcessingError("EMPTY_CARTON", "A carton must contain at least one unit before it can be closed.")
    if box.status not in {BoxStatus.OPEN, BoxStatus.REWEIGH_REQUIRED}:
        raise ProcessingError("CARTON_NOT_OPEN", f"Carton {box.box_number} cannot be closed from {box.status}.")
    if box.reweigh_required or box.status == BoxStatus.REWEIGH_REQUIRED:
        box.status = BoxStatus.REWEIGH_REQUIRED
        box.reweigh_required = True
    else:
        box.status = BoxStatus.AWAITING_WEIGHT
        record_event(
            box.order,
            ProcessingEvent.CARTON_CLOSED,
            box=box,
            detail=f"{box.box_number} awaiting weight ({len(box.contents)} units)",
            user=user,
        )
    db.session.flush()
    return box


def save_carton_weight(box: Box, weight, unit: str = "lb", user=None) -> Box:
    assert_lock(box.order, user)
    try:
        weight_f = float(weight)
    except (TypeError, ValueError) as exc:
        raise ProcessingError("WEIGHT_REQUIRED", "A positive carton weight is required.") from exc
    if weight_f <= 0:
        raise ProcessingError("WEIGHT_REQUIRED", "A positive carton weight is required.")
    if not box.contents:
        raise ProcessingError("EMPTY_CARTON", "Cannot record weight for an empty carton.")
    if box.status not in {BoxStatus.AWAITING_WEIGHT, BoxStatus.REWEIGH_REQUIRED}:
        raise ProcessingError(
            "WEIGHT_NOT_EXPECTED",
            f"Carton {box.box_number} is not waiting for a weight.",
        )
    was_reweigh = box.status == BoxStatus.REWEIGH_REQUIRED or box.reweigh_required
    uid, uname = _actor(user)
    box.weight_kg = weight_f
    box.weight_unit = (unit or "lb").strip() or "lb"
    box.reweigh_required = False
    box.status = BoxStatus.CLOSED
    box.closed_at = datetime.utcnow()
    box.closed_by_user_id = uid
    box.closed_by_username = uname
    if was_reweigh:
        record_event(
            box.order,
            ProcessingEvent.CARTON_REWEIGHED,
            box=box,
            detail=f"{box.box_number} reweighed {format_weight(box)}",
            user=user,
        )
        record_event(
            box.order,
            ProcessingEvent.CARTON_RECLOSED,
            box=box,
            detail=box.box_number,
            user=user,
        )
    else:
        record_event(
            box.order,
            ProcessingEvent.CARTON_WEIGHT_RECORDED,
            box=box,
            detail=f"{box.box_number} {format_weight(box)}",
            user=user,
        )
    _maybe_open_next_carton(box.order, user=user)
    db.session.flush()
    return box


def _maybe_open_next_carton(order: Order, user=None) -> Box | None:
    if remaining_units(order) and not any(b.status != BoxStatus.CLOSED for b in order.boxes):
        return create_processing_carton(order, user=user)
    return None


def recall_carton(box: Box, user=None) -> Box:
    assert_lock(box.order, user)
    if box.order.status == OrderStatus.CLOSED:
        raise ProcessingError("ORDER_CLOSED", "A closed order cannot be recalled.")
    if box.status != BoxStatus.CLOSED:
        raise ProcessingError("NOT_CLOSED", f"Carton {box.box_number} is not closed.")
    box.status = BoxStatus.OPEN
    record_event(
        box.order,
        ProcessingEvent.CARTON_RECALLED,
        box=box,
        detail=box.box_number,
        user=user,
    )
    db.session.flush()
    return box


def _invalidate_weight(box: Box, user=None) -> None:
    if box.weight_kg is not None or box.status == BoxStatus.CLOSED:
        record_event(
            box.order,
            ProcessingEvent.CARTON_WEIGHT_INVALIDATED,
            box=box,
            detail=f"{box.box_number} weight invalidated",
            user=user,
        )
    box.reweigh_required = True
    box.status = BoxStatus.REWEIGH_REQUIRED
    db.session.flush()


def remove_unit_from_carton(content: BoxContent, user=None) -> InventoryUnit:
    box = content.box
    order = box.order
    assert_lock(order, user)
    if box.status not in {BoxStatus.OPEN, BoxStatus.REWEIGH_REQUIRED}:
        raise ProcessingError(
            "CARTON_NOT_OPEN",
            "Recall the carton before removing a unit.",
        )
    unit = content.unit
    db.session.delete(content)
    prev = unit.status
    unit.status = UnitStatus.ALLOCATED
    record_movement(
        unit,
        from_status=prev,
        to_status=UnitStatus.ALLOCATED,
        movement_type=MovementType.PACK,
        order_id=order.id,
        reason=f"Removed from carton {box.box_number}",
    )
    record_event(
        order,
        ProcessingEvent.CARTON_UNIT_REMOVED,
        box=box,
        unit=unit,
        detail=f"UPC {unit.upc} removed from {box.box_number}",
        user=user,
    )
    _invalidate_weight(box, user=user)
    db.session.flush()
    return unit


def add_upc_to_recalled_carton(box: Box, raw_upc: str, user=None) -> BoxContent:
    if box.status not in {BoxStatus.OPEN, BoxStatus.REWEIGH_REQUIRED}:
        raise ProcessingError("CARTON_NOT_OPEN", "Recall the carton before adding a unit.")
    return scan_upc_into_box(box, raw_upc, user=user, correction=True)


def close_blockers(order: Order) -> list[str]:
    rec = reconcile(order)
    blockers = []
    if rec["ordered"] != rec["allocated"] or rec["allocated"] != rec["packed"]:
        blockers.append(
            f"Reconciliation failed: ordered={rec['ordered']} "
            f"allocated={rec['allocated']} packed={rec['packed']}."
        )
    if remaining_units(order):
        blockers.append("Remaining units must be 0.")
    if rec["packed"] == 0:
        blockers.append("No units have been packed.")
    for box in order.boxes:
        if box.reweigh_required or box.status == BoxStatus.REWEIGH_REQUIRED:
            blockers.append(f"{box.box_number} requires an updated weight.")
        elif box.status != BoxStatus.CLOSED:
            blockers.append(f"{box.box_number} is not CLOSED.")
        if not dims_confirmed(box):
            blockers.append(f"{box.box_number} is missing dimensions.")
        if box.weight_kg is None or box.weight_kg <= 0:
            blockers.append(f"{box.box_number} is missing a current weight.")
    return blockers


def ready_for_close_modal(order: Order) -> bool:
    return not close_blockers(order)


def finalize_order(order: Order, user=None):
    """Atomically reconcile, close, invoice, generate the closure PDF, release lock."""
    from .documents import generate_order_closure
    from .packing import close_order, mark_processed, mark_ready_to_close

    assert_lock(order, user)
    blockers = close_blockers(order)
    if blockers:
        if any("weight" in b.lower() or "reweigh" in b.lower() for b in blockers):
            raise ProcessingError(ExceptionType.STALE_WEIGHT, blockers[0])
        raise ProcessingError("CLOSE_BLOCKED", blockers[0])

    uid, uname = _actor(user)
    rec = reconcile(order)
    record_event(
        order,
        ProcessingEvent.ORDER_RECONCILED,
        detail=f"ordered={rec['ordered']} allocated={rec['allocated']} packed={rec['packed']}",
        user=user,
    )
    if order.status == OrderStatus.PROCESSING:
        mark_processed(order)
    if order.status == OrderStatus.PROCESSED:
        mark_ready_to_close(order)
    closed, invoice = close_order(order, created_by=uname)
    closed.closed_by_user_id = uid
    closed.closed_by_username = uname
    record_event(
        order,
        ProcessingEvent.ORDER_CLOSED,
        detail=f"Invoice {invoice.invoice_number}",
        user=user,
    )
    release_lock(order)
    db.session.commit()

    document = generate_order_closure(order)
    record_event(
        order,
        ProcessingEvent.PDF_GENERATED,
        detail=document.filename,
        user=user,
    )
    db.session.commit()
    return invoice, document


def closed_carton_summaries(order: Order) -> list[dict]:
    rows = []
    for box in sorted(order.boxes, key=lambda b: b.id):
        if box.status == BoxStatus.OPEN and not box.contents and not dims_confirmed(box):
            continue
        rows.append(
            {
                "box": box,
                "index": carton_index(box),
                "status": box.status,
                "units": len(box.contents),
                "dims": format_dims(box),
                "weight": format_weight(box),
            }
        )
    return rows
