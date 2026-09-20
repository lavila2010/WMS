"""UPC allocation: Client + Warehouse + UPC + AVAILABLE units only."""

from __future__ import annotations

from sqlalchemy import select

from ..auth import current_actor, record_audit
from ..constants import AllocationStatus, LedgerType, OrderStatus, UnitStatus
from ..extensions import db
from ..models import Allocation, ImportBatch, InventoryUnit, Order, OrderLine
from .inventory_ledger import LedgerError, transition_unit
from .inventory_visibility import operational_batch_clause


class AllocationError(ValueError):
    pass


ELIGIBLE = {OrderStatus.UNALLOCATED, OrderStatus.PARTIALLY_ALLOCATED}


def _lock_order(order_id: int) -> Order:
    order = db.session.execute(
        select(Order).where(Order.id == order_id).with_for_update()
    ).scalar_one_or_none()
    if order is None:
        raise AllocationError("Order not found.")
    return order


def _candidate_units(order: Order, upc: str, need: int) -> list[InventoryUnit]:
    stmt = (
        select(InventoryUnit)
        .outerjoin(ImportBatch, InventoryUnit.import_batch_id == ImportBatch.id)
        .where(
            InventoryUnit.client_id == order.client_id,
            InventoryUnit.warehouse_id == order.warehouse_id,
            InventoryUnit.upc == upc,
            InventoryUnit.status == UnitStatus.AVAILABLE,
            operational_batch_clause(),
        )
        .order_by(InventoryUnit.location.asc(), InventoryUnit.id.asc())
        .with_for_update(skip_locked=True, of=InventoryUnit)
        .limit(need)
    )
    return list(db.session.execute(stmt).scalars().all())


def _refresh_order_status(order: Order) -> str:
    lines = OrderLine.query.filter_by(order_id=order.id).all()
    if not lines:
        order.status = OrderStatus.UNALLOCATED
        return order.status
    full = all(line.qty_allocated >= line.qty_ordered for line in lines)
    any_allocated = any(line.qty_allocated > 0 for line in lines)
    if full:
        order.status = OrderStatus.ALLOCATED
    elif any_allocated:
        order.status = OrderStatus.PARTIALLY_ALLOCATED
    else:
        order.status = OrderStatus.UNALLOCATED
    return order.status


def allocate_order(order: Order, *, user=None, _fail_after: int | None = None) -> dict:
    if order.status not in ELIGIBLE:
        raise AllocationError(f"Order {order.wms_order_id} is not eligible for allocation.")
    uid, _ = current_actor()
    record_audit(
        "ALLOCATION_STARTED",
        module="Allocation",
        entity_type="order",
        entity_id=order.id,
        client_id=order.client_id,
        detail=order.wms_order_id,
    )
    locked = _lock_order(order.id)
    reserved = 0
    shortages = []
    try:
        for line in OrderLine.query.filter_by(order_id=locked.id).order_by(OrderLine.id).all():
            need = line.qty_ordered - line.qty_allocated
            if need <= 0:
                continue
            units = _candidate_units(locked, line.upc, need)
            for unit in units:
                if unit.client_id != locked.client_id or unit.warehouse_id != locked.warehouse_id:
                    raise AllocationError("Refusing cross-tenant unit.")
                if unit.upc != line.upc:
                    raise AllocationError("Refusing SKU/UPC mismatch.")
                allocation = Allocation(
                    client_id=locked.client_id,
                    order_id=locked.id,
                    order_line_id=line.id,
                    inventory_unit_id=unit.id,
                    warehouse_id=locked.warehouse_id,
                    upc=unit.upc,
                    location=unit.location,
                    status=AllocationStatus.ACTIVE,
                    created_by=uid,
                )
                db.session.add(allocation)
                db.session.flush()
                transition_unit(
                    unit,
                    to_status=UnitStatus.RESERVED,
                    transaction_type=LedgerType.RESERVE,
                    user_id=uid,
                    order_id=locked.id,
                    order_line_id=line.id,
                    allocation_id=allocation.id,
                    allocated_order_id=locked.id,
                    reference=f"alloc:{locked.wms_order_id}",
                )
                unit.allocation_id = allocation.id
                line.qty_allocated += 1
                reserved += 1
                if _fail_after is not None and reserved >= _fail_after:
                    raise LedgerError("injected failure")
            if line.qty_allocated < line.qty_ordered:
                shortages.append(
                    {
                        "upc": line.upc,
                        "needed": line.qty_ordered - line.qty_allocated,
                        "ordered": line.qty_ordered,
                        "allocated": line.qty_allocated,
                    }
                )
        status = _refresh_order_status(locked)
        event = (
            "ALLOCATION_COMPLETED"
            if status == OrderStatus.ALLOCATED
            else "ALLOCATION_PARTIAL"
            if status == OrderStatus.PARTIALLY_ALLOCATED
            else "ALLOCATION_COMPLETED"
        )
        record_audit(
            event,
            module="Allocation",
            entity_type="order",
            entity_id=locked.id,
            client_id=locked.client_id,
            detail=f"{status} reserved={reserved}",
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return {
        "order_id": locked.id,
        "status": status,
        "reserved": reserved,
        "shortages": shortages,
    }


def release_allocation(allocation: Allocation) -> None:
    if allocation.status != AllocationStatus.ACTIVE:
        raise AllocationError("Allocation is not active.")
    unit = db.session.get(InventoryUnit, allocation.inventory_unit_id)
    if unit is None:
        raise AllocationError("Inventory unit missing.")
    uid, _ = current_actor()
    transition_unit(
        unit,
        to_status=UnitStatus.AVAILABLE,
        transaction_type=LedgerType.UNRESERVE,
        user_id=uid,
        order_id=allocation.order_id,
        order_line_id=allocation.order_line_id,
        allocation_id=allocation.id,
        clear_allocation=True,
        reference=f"release:{allocation.id}",
    )
    allocation.status = AllocationStatus.RELEASED
    line = db.session.get(OrderLine, allocation.order_line_id)
    if line and line.qty_allocated > 0:
        line.qty_allocated -= 1
    order = db.session.get(Order, allocation.order_id)
    if order:
        _refresh_order_status(order)
    db.session.flush()
