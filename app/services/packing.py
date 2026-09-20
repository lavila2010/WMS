"""Packing / box workflow.

Workflow (per the approved spec):
    A. Select order
    B. Create/select box dimension
    C. Scan units into box
    D. Close box
    E. Enter box weight
    F. Repeat until all units are packed
    G. Close order only after quantity reconciliation
"""

from __future__ import annotations

from datetime import datetime

from ..constants import (
    AllocationStatus,
    BoxStatus,
    ExceptionType,
    OrderStatus,
    UnitStatus,
)
from ..extensions import db
from ..models import (
    Box,
    BoxContent,
    InventoryMovement,
    Order,
    Transaction,
)
from ..workflow import transition
from .allocation import (
    BarcodeError,
    active_allocation,
    active_allocations,
    find_unit,
    normalize_barcode,
    _record_exception,
)


def create_box(
    order: Order,
    box_number: str,
    length_cm: float | None = None,
    width_cm: float | None = None,
    height_cm: float | None = None,
) -> Box:
    if order.status == OrderStatus.READY_TO_PICK:
        transition(order, OrderStatus.PROCESSING, "Packing started.")
    box = Box(
        order_id=order.id,
        box_number=box_number,
        length_cm=length_cm,
        width_cm=width_cm,
        height_cm=height_cm,
        status=BoxStatus.OPEN,
    )
    db.session.add(box)
    db.session.flush()
    return box


def scan_into_box(box: Box, raw_barcode: str) -> BoxContent:
    """Scan a unit's barcode into ``box``.

    Enforces: box open, barcode known, unit allocated to this order, no
    duplicate, and a unit cannot be in two boxes.
    """
    if box.status != BoxStatus.OPEN:
        raise BarcodeError(
            ExceptionType.UNIT_ALREADY_BOXED,
            f"Box {box.box_number} is closed.",
        )

    barcode = normalize_barcode(raw_barcode)
    order = box.order

    unit = find_unit(barcode)
    if unit is None:
        _record_exception(
            order.id, barcode, ExceptionType.UNKNOWN_BARCODE,
            f"Barcode {barcode} is not a known inventory unit.",
        )
        raise BarcodeError(
            ExceptionType.UNKNOWN_BARCODE,
            f"Barcode {barcode} is not a known inventory unit.",
        )

    alloc = active_allocation(unit)
    if alloc is None:
        _record_exception(
            order.id, barcode, ExceptionType.WRONG_ORDER,
            f"Barcode {barcode} is not allocated; it does not belong to this order.",
        )
        raise BarcodeError(
            ExceptionType.WRONG_ORDER,
            f"Barcode {barcode} is not allocated to this order.",
        )
    if alloc.order_id != order.id:
        _record_exception(
            order.id, barcode, ExceptionType.UNIT_IN_OTHER_ACTIVE_ORDER,
            f"Barcode {barcode} is allocated to order {alloc.order.order_number}.",
        )
        raise BarcodeError(
            ExceptionType.UNIT_IN_OTHER_ACTIVE_ORDER,
            f"Barcode {barcode} belongs to another active order.",
        )

    existing_content = BoxContent.query.filter_by(
        inventory_unit_id=unit.id
    ).first()
    if existing_content is not None:
        if existing_content.box_id == box.id:
            _record_exception(
                order.id, barcode, ExceptionType.DUPLICATE_SCAN,
                f"Barcode {barcode} is already in this box.",
            )
            raise BarcodeError(
                ExceptionType.DUPLICATE_SCAN,
                f"Barcode {barcode} is already in this box.",
            )
        _record_exception(
            order.id, barcode, ExceptionType.UNIT_ALREADY_BOXED,
            f"Barcode {barcode} is already packed in box "
            f"{existing_content.box.box_number}.",
        )
        raise BarcodeError(
            ExceptionType.UNIT_ALREADY_BOXED,
            f"Barcode {barcode} is already packed in another box.",
        )

    content = BoxContent(box_id=box.id, inventory_unit_id=unit.id, barcode=barcode)
    db.session.add(content)

    prev_status = unit.status
    unit.status = UnitStatus.PACKED
    db.session.add(
        InventoryMovement(
            inventory_unit_id=unit.id,
            barcode=barcode,
            from_status=prev_status,
            to_status=UnitStatus.PACKED,
            reason=f"Packed into box {box.box_number}",
        )
    )
    db.session.add(
        Transaction(
            type="PACK",
            order_id=order.id,
            inventory_unit_id=unit.id,
            barcode=barcode,
            quantity=1,
            detail=f"Packed into box {box.box_number}",
        )
    )
    db.session.flush()
    return content


def close_box(box: Box, weight_kg: float) -> Box:
    if weight_kg is None or weight_kg <= 0:
        raise ValueError("A positive box weight (kg) is required to close a box.")
    if not box.contents:
        raise ValueError("Cannot close an empty box.")
    box.status = BoxStatus.CLOSED
    box.weight_kg = float(weight_kg)
    box.closed_at = datetime.utcnow()
    db.session.add(
        Transaction(
            type="CLOSE_BOX",
            order_id=box.order_id,
            detail=f"Box {box.box_number} closed at {weight_kg} kg "
            f"with {len(box.contents)} units",
        )
    )
    db.session.flush()
    return box


def packed_count(order: Order) -> int:
    return (
        BoxContent.query.join(Box, BoxContent.box_id == Box.id)
        .filter(Box.order_id == order.id)
        .count()
    )


def open_boxes(order: Order) -> list[Box]:
    return [b for b in order.boxes if b.status == BoxStatus.OPEN]


def reconcile(order: Order) -> dict:
    """Compare ordered vs allocated vs packed quantities."""
    ordered = order.ordered_quantity
    allocated = len(active_allocations(order))
    packed = packed_count(order)
    open_box_list = open_boxes(order)
    ok = (
        ordered > 0
        and allocated == ordered
        and packed == allocated
        and len(open_box_list) == 0
    )
    return {
        "ordered": ordered,
        "allocated": allocated,
        "packed": packed,
        "open_boxes": len(open_box_list),
        "ok": ok,
    }


def mark_processed(order: Order) -> Order:
    """Transition PROCESSING -> PROCESSED once every unit is packed."""
    rec = reconcile(order)
    if rec["open_boxes"] > 0:
        raise ValueError("All boxes must be closed before marking PROCESSED.")
    if rec["packed"] != rec["allocated"] or rec["allocated"] == 0:
        raise ValueError(
            "All allocated units must be packed before marking PROCESSED."
        )
    return transition(order, OrderStatus.PROCESSED, "All units packed.")


def mark_ready_to_close(order: Order) -> Order:
    rec = reconcile(order)
    if not rec["ok"]:
        _record_exception(
            order.id, None, ExceptionType.RECONCILIATION,
            f"Reconciliation failed: ordered={rec['ordered']} "
            f"allocated={rec['allocated']} packed={rec['packed']} "
            f"open_boxes={rec['open_boxes']}",
        )
        raise ValueError("Quantity reconciliation failed; cannot proceed to close.")
    return transition(order, OrderStatus.READY_TO_CLOSE, "Reconciliation passed.")


def close_order(order: Order) -> Order:
    """Close the order after quantity reconciliation (final step G)."""
    rec = reconcile(order)
    if not rec["ok"]:
        raise ValueError("Quantity reconciliation failed; cannot close order.")
    order_ = transition(order, OrderStatus.CLOSED, "Order closed.")
    for alloc in active_allocations(order):
        alloc.unit.status = UnitStatus.SHIPPED
    db.session.flush()
    return order_
