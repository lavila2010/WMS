"""Order fulfillment quantities: ordered = shipped + allocated + remaining."""

from __future__ import annotations

from sqlalchemy import text

from ..constants import AllocationStatus, OrderStatus, PickTicketStatus, UnitStatus
from ..extensions import db
from ..models import Allocation, InventoryUnit, Order, OrderLine, PickTicket


ACTIVE_ALLOC_STATUSES = {UnitStatus.RESERVED, UnitStatus.PACKED}


def line_live_allocated(line: OrderLine) -> int:
    return (
        db.session.query(InventoryUnit)
        .join(Allocation, Allocation.inventory_unit_id == InventoryUnit.id)
        .filter(
            Allocation.order_line_id == line.id,
            Allocation.status == AllocationStatus.ACTIVE,
            InventoryUnit.status.in_(ACTIVE_ALLOC_STATUSES),
        )
        .count()
    )


def line_short(line: OrderLine) -> int:
    return max(int(line.qty_short or 0), 0)


def line_remaining(line: OrderLine) -> int:
    return max(
        int(line.qty_ordered) - int(line.qty_shipped or 0) - line_short(line) - line_live_allocated(line),
        0,
    )


def order_quantities(order: Order) -> dict:
    lines = OrderLine.query.filter_by(order_id=order.id).order_by(OrderLine.id).all()
    ordered = sum(int(line.qty_ordered) for line in lines)
    shipped = sum(int(line.qty_shipped or 0) for line in lines)
    short = sum(line_short(line) for line in lines)
    currently = 0
    remaining = 0
    upc_rows = []
    for line in lines:
        live = line_live_allocated(line)
        rem = max(int(line.qty_ordered) - int(line.qty_shipped or 0) - line_short(line) - live, 0)
        currently += live
        remaining += rem
        upc_rows.append(
            {
                "upc": line.upc,
                "sku": line.sku,
                "description": line.description,
                "style": None,
                "color": None,
                "size": None,
                "ordered_qty": int(line.qty_ordered),
                "shipped_qty": int(line.qty_shipped or 0),
                "short_qty": line_short(line),
                "allocated_qty": live,
                "remaining_qty": rem,
                "line": line,
            }
        )
    return {
        "ordered": ordered,
        "shipped": shipped,
        "short": short,
        "currently_allocated": currently,
        "remaining": remaining,
        "lines": lines,
        "upc_rows": upc_rows,
    }


def allocation_need(line: OrderLine) -> int:
    return line_remaining(line)


def is_allocation_eligible(order: Order, qty: dict | None = None) -> bool:
    if order.status in {OrderStatus.CLOSED, OrderStatus.CANCELLED}:
        return False
    qty = qty or order_quantities(order)
    return qty["remaining"] > 0


def is_fully_allocated(order: Order, qty: dict | None = None) -> bool:
    qty = qty or order_quantities(order)
    return qty["remaining"] == 0 and qty["currently_allocated"] > 0


def is_partially_allocated_wave(order: Order, qty: dict | None = None) -> bool:
    qty = qty or order_quantities(order)
    return qty["currently_allocated"] > 0 and qty["remaining"] > 0


def current_wave_number(order: Order) -> int:
    return int(order.current_wave_number or 0)


def current_wave_approved(order: Order) -> bool:
    wave = current_wave_number(order)
    approved = int(order.partial_approved_wave or 0)
    return wave > 0 and approved == wave


def pick_ticket_eligible(order: Order, qty: dict | None = None) -> bool:
    qty = qty or order_quantities(order)
    if order.status in {OrderStatus.CLOSED, OrderStatus.CANCELLED}:
        return False
    if qty["currently_allocated"] <= 0:
        return False
    unticketed = unticketed_allocation_count(order, wave=current_wave_number(order) or None)
    if unticketed <= 0:
        return False
    if qty["remaining"] == 0:
        return True
    return current_wave_approved(order)


def _unticketed_query(order: Order, *, wave: int | None = None):
    query = Allocation.query.filter_by(
        order_id=order.id, status=AllocationStatus.ACTIVE, pick_ticket_id=None
    )
    if wave:
        query = query.filter(Allocation.wave_number == int(wave))
    return query


def unticketed_allocations(order: Order, *, wave: int | None = None) -> list[Allocation]:
    return _unticketed_query(order, wave=wave).order_by(Allocation.id).all()


def current_wave_unticketed_allocations(order: Order) -> list[Allocation]:
    return unticketed_allocations(order, wave=current_wave_number(order) or None)


def unticketed_allocation_count(order: Order, *, wave: int | None = None) -> int:
    return _unticketed_query(order, wave=wave).count()


def current_wave_allocation_count(order: Order) -> int:
    wave = current_wave_number(order)
    query = Allocation.query.filter_by(order_id=order.id, status=AllocationStatus.ACTIVE)
    if wave:
        query = query.filter(Allocation.wave_number == wave)
    return query.count()


def active_unticketed_wave_numbers(order: Order) -> list[int]:
    rows = (
        db.session.query(Allocation.wave_number)
        .filter_by(order_id=order.id, status=AllocationStatus.ACTIVE, pick_ticket_id=None)
        .distinct()
        .all()
    )
    return sorted({int(row[0] or 0) for row in rows})


def derive_active_wave(order: Order, pending: list[Allocation] | None = None) -> int:
    """Resolve the operational wave from the order, falling back to unticketed allocations."""
    pending = pending if pending is not None else unticketed_allocations(order)
    waves = [int(row.wave_number or 0) for row in pending]
    current = current_wave_number(order)
    if current > 0:
        on_current = [wave for wave in waves if wave == current]
        if on_current:
            return current
    positive = [wave for wave in waves if wave > 0]
    if positive:
        return max(positive)
    if waves:
        return max(waves)
    return current


def open_pick_ticket_count(order: Order) -> int:
    return PickTicket.query.filter(
        PickTicket.order_id == order.id,
        PickTicket.status.in_(PickTicketStatus.LEGACY_OPEN),
    ).count()


def allocation_page_visible(order: Order, qty: dict | None = None) -> bool:
    """Allocation action-queue visibility. Daily Exceptions does not use this rule."""
    if order.status in {OrderStatus.CLOSED, OrderStatus.CANCELLED}:
        return False
    if order.status == OrderStatus.PROCESSING:
        return False
    if open_pick_ticket_count(order) > 0:
        return False
    qty = qty or order_quantities(order)
    if qty["remaining"] > 0:
        return True
    return qty["currently_allocated"] > 0 and pick_ticket_eligible(order, qty)


def repair_zero_current_wave_numbers() -> dict:
    """Idempotent repair: current_wave_number=0 with a single active unticketed wave.

    Does not change Allocation.wave_number, Pick Ticket numbers, or orders already
    on a positive current wave. Conflicting multi-wave rows are skipped.
    """
    allocs = db.session.execute(text("SELECT to_regclass('public.allocations')")).scalar()
    orders = db.session.execute(text("SELECT to_regclass('public.orders')")).scalar()
    if not allocs or not orders:
        return {"repaired": 0, "skipped_conflicts": []}
    skipped_rows = db.session.execute(
        text(
            """
            SELECT o.wms_order_id AS wms_order_id,
                   array_agg(DISTINCT a.wave_number) AS waves
            FROM orders o
            JOIN allocations a ON a.order_id = o.id
            WHERE COALESCE(o.current_wave_number, 0) <= 0
              AND a.status = 'ACTIVE'
              AND a.pick_ticket_id IS NULL
            GROUP BY o.id, o.wms_order_id
            HAVING COUNT(DISTINCT a.wave_number) > 1
            """
        )
    ).mappings().all()
    skipped = [
        {"wms_order_id": row["wms_order_id"], "waves": sorted(int(w) for w in (row["waves"] or []))}
        for row in skipped_rows
    ]
    result = db.session.execute(
        text(
            """
            UPDATE orders o
            SET current_wave_number = sub.max_wave
            FROM (
                SELECT a.order_id AS order_id, MAX(a.wave_number) AS max_wave
                FROM allocations a
                WHERE a.status = 'ACTIVE' AND a.pick_ticket_id IS NULL
                GROUP BY a.order_id
                HAVING COUNT(DISTINCT a.wave_number) = 1
            ) sub
            WHERE o.id = sub.order_id
              AND COALESCE(o.current_wave_number, 0) <= 0
            RETURNING o.wms_order_id
            """
        )
    )
    repaired_ids = [row[0] for row in result]
    if repaired_ids or skipped:
        db.session.commit()
    return {"repaired": len(repaired_ids), "skipped_conflicts": skipped, "repaired_ids": repaired_ids}


def pick_ticket_handoff_diagnostic(order: Order) -> dict:
    """Admin-safe snapshot for partial-approval → Pick Ticket debugging. No customer PII."""
    qty = order_quantities(order)
    wave = current_wave_number(order)
    current_unticketed = unticketed_allocation_count(order, wave=wave or None)
    return {
        "wms_order_id": order.wms_order_id,
        "status": order.status,
        "ordered": qty["ordered"],
        "shipped": qty["shipped"],
        "currently_allocated": qty["currently_allocated"],
        "remaining": qty["remaining"],
        "current_wave_number": wave,
        "partial_approved_wave": order.partial_approved_wave,
        "active_unticketed_wave_numbers": active_unticketed_wave_numbers(order),
        "unticketed_allocation_count": current_unticketed,
        "current_wave_unticketed_count": current_unticketed,
        "open_pick_ticket_count": open_pick_ticket_count(order),
        "pick_ticket_eligible": pick_ticket_eligible(order, qty),
        "allocation_page_visible": allocation_page_visible(order, qty),
    }


def open_ticket_for_order(order: Order):
    return (
        PickTicket.query.filter(
            PickTicket.order_id == order.id,
            PickTicket.status.in_(PickTicketStatus.LEGACY_OPEN),
        )
        .order_by(PickTicket.ticket_sequence.desc())
        .first()
    )


def refresh_order_status(order: Order) -> str:
    if order.status in {OrderStatus.CLOSED, OrderStatus.CANCELLED}:
        return order.status
    qty = order_quantities(order)
    shipped = qty["shipped"]
    ordered = qty["ordered"]
    if ordered and shipped >= ordered:
        order.status = OrderStatus.CLOSED
        return order.status
    has_open_ticket = open_ticket_for_order(order) is not None
    if order.status == OrderStatus.PROCESSING and has_open_ticket:
        return order.status
    if shipped > 0 and shipped < ordered:
        if has_open_ticket:
            order.status = OrderStatus.PICK_TICKET_READY
        elif qty["remaining"] == 0 and qty["currently_allocated"] > 0:
            order.status = OrderStatus.ALLOCATED
        elif qty["currently_allocated"] > 0:
            order.status = OrderStatus.PARTIALLY_ALLOCATED
        else:
            order.status = OrderStatus.PARTIALLY_FULFILLED
        return order.status
    if has_open_ticket:
        order.status = OrderStatus.PICK_TICKET_READY
        return order.status
    if qty["remaining"] == 0 and qty["currently_allocated"] > 0:
        order.status = OrderStatus.ALLOCATED
    elif qty["currently_allocated"] > 0:
        order.status = OrderStatus.PARTIALLY_ALLOCATED
    else:
        order.status = OrderStatus.UNALLOCATED
    return order.status


def enrich_upc_rows_from_units(order: Order, upc_rows: list[dict]) -> list[dict]:
    units = InventoryUnit.query.filter_by(allocated_order_id=order.id).all()
    by_upc: dict[str, InventoryUnit] = {}
    for unit in units:
        by_upc.setdefault(unit.upc, unit)
    for row in upc_rows:
        unit = by_upc.get(row["upc"])
        if unit:
            row["style"] = unit.style
            row["color"] = unit.color
            row["size"] = unit.size
            row["sku"] = row["sku"] or unit.sku
            row["description"] = row["description"] or unit.description
    return upc_rows
