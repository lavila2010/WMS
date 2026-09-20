"""Barcode-level allocation service and barcode business rules.

Barcode rules enforced here:
- every physical unit has a unique barcode (schema-enforced);
- allocation is barcode-level (one Allocation row per unit);
- scan or manual entry are the same code path (a barcode string);
- duplicate scan is rejected (unit already actively allocated to this order);
- the barcode must belong to the selected order (SKU must match an order
  line with remaining demand);
- a barcode cannot belong to two active orders (a unit may have only one
  ACTIVE allocation).
"""

from __future__ import annotations

from ..constants import (
    AllocationStatus,
    ExceptionType,
    MovementType,
    OrderStatus,
    UnitStatus,
)
from ..extensions import db
from ..models import (
    Allocation,
    InventoryUnit,
    Order,
    OrderException,
    OrderLine,
    Transaction,
)
from ..workflow import transition
from .movements import record_movement


class BarcodeError(Exception):
    """Raised when a barcode violates an allocation/packing rule."""

    def __init__(self, exc_type: str, message: str):
        super().__init__(message)
        self.exc_type = exc_type
        self.message = message


def _record_exception(order_id, barcode, exc_type, message):
    db.session.add(
        OrderException(
            order_id=order_id, barcode=barcode, type=exc_type, message=message
        )
    )
    db.session.flush()


def normalize_barcode(raw: str) -> str:
    barcode = (raw or "").strip()
    if not barcode:
        raise BarcodeError(ExceptionType.UNKNOWN_BARCODE, "Empty barcode.")
    return barcode


def find_unit(barcode: str) -> InventoryUnit | None:
    return InventoryUnit.query.filter_by(barcode=barcode).first()


def _validate_scope(order, unit, barcode: str) -> None:
    """Enforce Client + Warehouse isolation for a barcode.

    Physical inventory is partitioned ONLY by Client + Warehouse. Order Type is
    an order attribute and does NOT participate in inventory matching, so an
    order type mismatch never rejects a unit.
    """
    if unit.client_id != order.client_id:
        _record_exception(
            order.id, barcode, ExceptionType.WRONG_CLIENT,
            f"Barcode {barcode} belongs to a different client.",
        )
        raise BarcodeError(
            ExceptionType.WRONG_CLIENT,
            f"Barcode {barcode} belongs to a different client.",
        )
    if unit.warehouse_id != order.warehouse_id:
        _record_exception(
            order.id, barcode, ExceptionType.WRONG_WAREHOUSE,
            f"Barcode {barcode} belongs to a different warehouse.",
        )
        raise BarcodeError(
            ExceptionType.WRONG_WAREHOUSE,
            f"Barcode {barcode} belongs to a different warehouse.",
        )


def active_allocation(unit: InventoryUnit) -> Allocation | None:
    return Allocation.query.filter_by(
        inventory_unit_id=unit.id, status=AllocationStatus.ACTIVE
    ).first()


def allocated_count_for_line(line: OrderLine) -> int:
    return Allocation.query.filter_by(
        order_line_id=line.id, status=AllocationStatus.ACTIVE
    ).count()


def _pick_line_with_demand(order: Order, sku: str) -> OrderLine | None:
    matching = [line for line in order.lines if line.sku == sku]
    if not matching:
        return None
    for line in matching:
        if allocated_count_for_line(line) < line.quantity:
            return line
    return None


def allocate_barcode(order: Order, raw_barcode: str) -> Allocation:
    """Allocate the unit identified by ``raw_barcode`` to ``order``.

    Raises ``BarcodeError`` (and records an OrderException) on any rule
    violation.
    """
    barcode = normalize_barcode(raw_barcode)

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

    _validate_scope(order, unit, barcode)

    existing = active_allocation(unit)
    if existing is not None:
        if existing.order_id == order.id:
            _record_exception(
                order.id, barcode, ExceptionType.DUPLICATE_SCAN,
                f"Barcode {barcode} is already allocated to this order.",
            )
            raise BarcodeError(
                ExceptionType.DUPLICATE_SCAN,
                f"Barcode {barcode} is already allocated to this order.",
            )
        _record_exception(
            order.id, barcode, ExceptionType.UNIT_IN_OTHER_ACTIVE_ORDER,
            f"Barcode {barcode} is actively allocated to order "
            f"{existing.order.order_number}.",
        )
        raise BarcodeError(
            ExceptionType.UNIT_IN_OTHER_ACTIVE_ORDER,
            f"Barcode {barcode} belongs to another active order.",
        )

    line = _pick_line_with_demand(order, unit.sku)
    if line is None:
        has_sku = any(line.sku == unit.sku for line in order.lines)
        if not has_sku:
            _record_exception(
                order.id, barcode, ExceptionType.WRONG_ORDER,
                f"SKU {unit.sku} is not on order {order.order_number}.",
            )
            raise BarcodeError(
                ExceptionType.WRONG_ORDER,
                f"Barcode {barcode} (SKU {unit.sku}) does not belong to this order.",
            )
        _record_exception(
            order.id, barcode, ExceptionType.NO_DEMAND,
            f"Order {order.order_number} has no remaining demand for SKU {unit.sku}.",
        )
        raise BarcodeError(
            ExceptionType.NO_DEMAND,
            f"No remaining demand for SKU {unit.sku} on this order.",
        )

    # Advance workflow to ALLOCATING on the first allocation.
    if order.status == OrderStatus.VALIDATED:
        transition(order, OrderStatus.ALLOCATING, "First unit allocated.")

    allocation = Allocation(
        order_id=order.id,
        order_line_id=line.id,
        inventory_unit_id=unit.id,
        barcode=barcode,
        status=AllocationStatus.ACTIVE,
    )
    db.session.add(allocation)

    prev_status = unit.status
    unit.status = UnitStatus.ALLOCATED
    unit.order_id = order.id
    record_movement(
        unit,
        from_status=prev_status,
        to_status=UnitStatus.ALLOCATED,
        movement_type=MovementType.ALLOCATE,
        order_id=order.id,
        reason=f"Allocated to order {order.order_number}",
    )
    db.session.add(
        Transaction(
            type="ALLOCATE",
            order_id=order.id,
            inventory_unit_id=unit.id,
            barcode=barcode,
            quantity=1,
            detail=f"Allocated to order {order.order_number}",
        )
    )
    db.session.flush()
    return allocation


def release_allocation(allocation: Allocation) -> None:
    unit = allocation.unit
    allocation.status = AllocationStatus.RELEASED
    prev_status = unit.status
    unit.status = UnitStatus.AVAILABLE
    unit.order_id = None
    record_movement(
        unit,
        from_status=prev_status,
        to_status=UnitStatus.AVAILABLE,
        movement_type=MovementType.RELEASE,
        order_id=allocation.order_id,
        reason="Allocation released",
    )
    db.session.add(
        Transaction(
            type="RELEASE_ALLOCATION",
            order_id=allocation.order_id,
            inventory_unit_id=unit.id,
            barcode=unit.barcode,
            quantity=1,
        )
    )
    db.session.flush()


def active_allocations(order: Order) -> list[Allocation]:
    return Allocation.query.filter_by(
        order_id=order.id, status=AllocationStatus.ACTIVE
    ).all()


def allocation_progress(order: Order) -> dict:
    """Return per-SKU {ordered, allocated} plus totals for ``order``."""
    per_sku: dict[str, dict[str, int]] = {}
    for line in order.lines:
        entry = per_sku.setdefault(line.sku, {"ordered": 0, "allocated": 0})
        entry["ordered"] += line.quantity
        entry["allocated"] += allocated_count_for_line(line)
    total_ordered = sum(v["ordered"] for v in per_sku.values())
    total_allocated = sum(v["allocated"] for v in per_sku.values())
    return {
        "per_sku": per_sku,
        "total_ordered": total_ordered,
        "total_allocated": total_allocated,
        "fully_allocated": total_ordered > 0 and total_allocated >= total_ordered,
    }


def is_fully_allocated(order: Order) -> bool:
    return allocation_progress(order)["fully_allocated"]


def mark_allocated(order: Order) -> Order:
    """Transition ALLOCATING -> ALLOCATED when demand is fully covered."""
    if not is_fully_allocated(order):
        raise BarcodeError(
            ExceptionType.NO_DEMAND,
            "Order is not fully allocated; cannot mark as ALLOCATED.",
        )
    return transition(order, OrderStatus.ALLOCATED, "All demand allocated.")
