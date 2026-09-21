"""UPC allocation: Client + Warehouse + UPC + AVAILABLE units only."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import insert, select, text
from sqlalchemy.orm.attributes import set_committed_value

from ..auth import current_actor, record_audit
from ..constants import AllocationStatus, LedgerType, OrderStatus, UnitStatus
from ..extensions import db
from ..models import Allocation, ImportBatch, InventoryTransaction, InventoryUnit, Order, OrderLine
from .fulfillment import (
    derive_active_wave,
    is_allocation_eligible,
    live_allocated_by_line,
    order_quantities,
    refresh_order_status,
    unticketed_allocation_count,
    unticketed_allocations,
)
from .inventory_ledger import ALLOWED, LedgerError, transition_unit
from .inventory_visibility import operational_batch_clause
from .order_visibility import order_is_operational

ALLOC_CHUNK_SIZE = 1000


class AllocationError(ValueError):
    pass


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
        .execution_options(populate_existing=True)
        .limit(need)
    )
    return list(db.session.execute(stmt).scalars().all())


def _refresh_order_status(order: Order, qty: dict | None = None) -> str:
    return refresh_order_status(order, qty)


def _line_need(line: OrderLine, live_map: dict[int, int]) -> int:
    short = max(int(line.qty_short or 0), 0)
    return max(
        int(line.qty_ordered) - int(line.qty_shipped or 0) - short - int(live_map.get(line.id, 0)),
        0,
    )


def _persist_reservation_chunk(
    locked: Order,
    line: OrderLine,
    units: list[InventoryUnit],
    *,
    wave: int,
    user_id,
) -> int:
    """One Allocation + RESERVE ledger row per unit, flushed as a bounded chunk."""
    if not units:
        return 0
    now = datetime.utcnow()
    reference = f"alloc:{locked.wms_order_id}"
    alloc_rows = []
    for unit in units:
        if unit.client_id != locked.client_id or unit.warehouse_id != locked.warehouse_id:
            raise AllocationError("Refusing cross-tenant unit.")
        if unit.upc != line.upc:
            raise AllocationError("Refusing SKU/UPC mismatch.")
        allowed = ALLOWED.get((unit.status, UnitStatus.RESERVED))
        if not allowed or LedgerType.RESERVE not in allowed:
            raise LedgerError(
                f"Illegal transition {unit.status} → {UnitStatus.RESERVED} as {LedgerType.RESERVE}."
            )
        alloc_rows.append(
            {
                "client_id": locked.client_id,
                "order_id": locked.id,
                "order_line_id": line.id,
                "inventory_unit_id": unit.id,
                "warehouse_id": locked.warehouse_id,
                "upc": unit.upc,
                "location": unit.location,
                "status": AllocationStatus.ACTIVE,
                "wave_number": wave,
                "created_by": user_id,
                "created_at": now,
            }
        )
    inserted = list(
        db.session.execute(
            insert(Allocation).values(alloc_rows).returning(Allocation.id, Allocation.inventory_unit_id)
        )
    )
    by_unit = {int(unit_id): int(alloc_id) for alloc_id, unit_id in inserted}
    value_sql = ",".join(f"({int(unit.id)}::int, {int(by_unit[unit.id])}::int)" for unit in units)
    db.session.execute(
        text(
            f"""
            UPDATE inventory_units AS u
            SET status = :st,
                allocated_order_id = :oid,
                allocation_id = v.aid,
                updated_at = :now
            FROM (VALUES {value_sql}) AS v(uid, aid)
            WHERE u.id = v.uid
            """
        ),
        {"st": UnitStatus.RESERVED, "oid": locked.id, "now": now},
    )
    txn_rows = []
    for unit in units:
        alloc_id = by_unit[unit.id]
        txn_rows.append(
            {
                "client_id": unit.client_id,
                "warehouse_id": unit.warehouse_id,
                "inventory_unit_id": unit.id,
                "import_batch_id": unit.import_batch_id,
                "upc": unit.upc,
                "location": unit.location,
                "transaction_type": LedgerType.RESERVE,
                "from_status": UnitStatus.AVAILABLE,
                "to_status": UnitStatus.RESERVED,
                "order_id": locked.id,
                "order_line_id": line.id,
                "allocation_id": alloc_id,
                "user_id": user_id,
                "reference": reference,
                "created_at": now,
            }
        )
        set_committed_value(unit, "status", UnitStatus.RESERVED)
        set_committed_value(unit, "allocation_id", alloc_id)
        set_committed_value(unit, "allocated_order_id", locked.id)
    db.session.execute(insert(InventoryTransaction).values(txn_rows))
    line.qty_allocated = int(line.qty_allocated or 0) + len(units)
    return len(units)


def allocate_order(order: Order, *, user=None, _fail_after: int | None = None) -> dict:
    if not order_is_operational(order):
        raise AllocationError(
            f"Order {order.wms_order_id} is not operational until its import batch is COMPLETED."
        )
    if order.status in {OrderStatus.CLOSED, OrderStatus.CANCELLED}:
        raise AllocationError(f"Order {order.wms_order_id} is not eligible for allocation.")
    qty = order_quantities(order)
    if not is_allocation_eligible(order, qty):
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
    locked.last_allocation_attempt_at = datetime.utcnow()
    if unticketed_allocation_count(locked) == 0:
        locked.current_wave_number = int(locked.current_wave_number or 0) + 1
        locked.partial_approved_wave = None
        locked.partial_allocation_approved_at = None
        locked.partial_allocation_approved_by_user_id = None
    elif not locked.current_wave_number:
        locked.current_wave_number = 1
    wave = int(locked.current_wave_number)
    reserved = 0
    shortages = []
    try:
        live_map = live_allocated_by_line(locked.id)
        for line in OrderLine.query.filter_by(order_id=locked.id).order_by(OrderLine.id).all():
            need = _line_need(line, live_map)
            if need <= 0:
                continue
            leftover = need
            while leftover > 0:
                take = min(leftover, ALLOC_CHUNK_SIZE)
                if _fail_after is not None:
                    budget = _fail_after - reserved
                    if budget <= 0:
                        raise LedgerError("injected failure")
                    take = min(take, budget)
                units = _candidate_units(locked, line.upc, take)
                if not units:
                    break
                reserved += _persist_reservation_chunk(
                    locked, line, units, wave=wave, user_id=uid
                )
                leftover -= len(units)
                if _fail_after is not None and reserved >= _fail_after:
                    raise LedgerError("injected failure")
                if len(units) < take:
                    break
            if leftover > 0:
                shortages.append(
                    {
                        "upc": line.upc,
                        "needed": leftover,
                        "ordered": line.qty_ordered,
                        "allocated": line.qty_allocated,
                    }
                )
        qty_after = order_quantities(locked)
        status = _refresh_order_status(locked, qty_after)
        if qty_after["remaining"] > 0 and qty_after["currently_allocated"] > 0:
            record_audit(
                "PARTIAL_ALLOCATION_CREATED",
                module="Allocation",
                entity_type="order",
                entity_id=locked.id,
                client_id=locked.client_id,
                detail=f"{status} reserved={reserved} remaining={qty_after['remaining']}",
            )
            event = "ALLOCATION_PARTIAL"
        else:
            event = "ALLOCATION_COMPLETED"
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
        "remaining": qty_after["remaining"],
        "currently_allocated": qty_after["currently_allocated"],
        "ordered": qty_after["ordered"],
        "shipped": qty_after["shipped"],
    }


def allocate_orders(order_ids: list[int], *, user=None) -> dict:
    selected = list(dict.fromkeys(int(oid) for oid in order_ids if oid))
    record_audit(
        "BULK_ALLOCATION_STARTED",
        module="Allocation",
        entity_type="bulk_allocation",
        detail=f"selected={len(selected)}",
    )
    summary = {
        "selected": len(selected),
        "fully_allocated": 0,
        "partially_allocated": 0,
        "no_inventory": 0,
        "failed": 0,
        "results": [],
        "errors": [],
    }
    for oid in selected:
        order = db.session.get(Order, oid)
        if order is None:
            summary["failed"] += 1
            summary["errors"].append({"order_id": oid, "error": "Order not found."})
            continue
        try:
            result = allocate_order(order, user=user)
        except Exception as exc:  # noqa: BLE001
            summary["failed"] += 1
            summary["errors"].append({"order_id": oid, "error": str(exc)})
            continue
        summary["results"].append(result)
        if result["reserved"] == 0:
            summary["no_inventory"] += 1
        elif result["shortages"]:
            summary["partially_allocated"] += 1
        else:
            summary["fully_allocated"] += 1
    record_audit(
        "BULK_ALLOCATION_COMPLETED",
        module="Allocation",
        entity_type="bulk_allocation",
        detail=(
            f"selected={summary['selected']} full={summary['fully_allocated']} "
            f"partial={summary['partially_allocated']} none={summary['no_inventory']} "
            f"failed={summary['failed']}"
        ),
    )
    db.session.commit()
    return summary


def approve_partial_allocation(order: Order, *, user=None) -> Order:
    if not order_is_operational(order):
        raise AllocationError("Order is not operational.")
    uid, uname = current_actor()
    locked = _lock_order(order.id)
    qty = order_quantities(locked)
    if not (qty["currently_allocated"] > 0 and qty["remaining"] > 0):
        raise AllocationError("Order does not have a partial allocation to approve.")
    pending = unticketed_allocations(locked)
    if not pending:
        raise AllocationError("No current allocation wave is waiting for approval.")
    active_wave = derive_active_wave(locked, pending)
    if active_wave <= 0:
        raise AllocationError("No current allocation wave is waiting for approval.")
    locked.current_wave_number = active_wave
    locked.partial_approved_wave = active_wave
    locked.partial_allocation_approved_at = datetime.utcnow()
    locked.partial_allocation_approved_by_user_id = uid
    record_audit(
        "PARTIAL_ALLOCATION_APPROVED",
        module="Allocation",
        entity_type="order",
        entity_id=locked.id,
        client_id=locked.client_id,
        detail=(
            f"{locked.wms_order_id} allocated={qty['currently_allocated']} "
            f"remaining={qty['remaining']} wave={active_wave} approved_by={uname}"
        ),
    )
    db.session.commit()
    return locked


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
