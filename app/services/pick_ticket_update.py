"""OPEN pick-ticket unit substitution with ISSUE_HOLD."""

from __future__ import annotations

from sqlalchemy import select

from ..auth import current_actor, record_audit
from ..constants import (
    CLOSED_PICK_TICKET_UPDATE_MESSAGE,
    REPLACEMENT_UNAVAILABLE_MESSAGE,
    AllocationStatus,
    InventoryIssueType,
    LedgerType,
    PickTicketStatus,
    UnitStatus,
)
from ..extensions import db
from ..models import Allocation, ImportBatch, InventoryIssue, InventoryUnit, Order, OrderLine, PickTicket
from .inventory_ledger import transition_unit
from .inventory_visibility import operational_batch_clause


class PickTicketUpdateError(ValueError):
    pass


def require_open_ticket(ticket: PickTicket) -> PickTicket:
    if ticket is None:
        raise PickTicketUpdateError("Pick ticket not found.")
    stored = PickTicketStatus.OPEN if ticket.status == "ACTIVE" else ticket.status
    if stored == PickTicketStatus.CLOSED:
        raise PickTicketUpdateError(CLOSED_PICK_TICKET_UPDATE_MESSAGE)
    if stored == PickTicketStatus.CANCELLED:
        raise PickTicketUpdateError("Pick ticket is cancelled.")
    if stored != PickTicketStatus.OPEN:
        raise PickTicketUpdateError(CLOSED_PICK_TICKET_UPDATE_MESSAGE)
    return ticket


def ticket_units(ticket: PickTicket) -> list[dict]:
    rows = (
        db.session.query(Allocation, InventoryUnit)
        .join(InventoryUnit, InventoryUnit.id == Allocation.inventory_unit_id)
        .filter(Allocation.pick_ticket_id == ticket.id, Allocation.status == AllocationStatus.ACTIVE)
        .order_by(InventoryUnit.location.asc(), InventoryUnit.upc.asc(), InventoryUnit.id.asc())
        .all()
    )
    return [
        {
            "allocation": allocation,
            "unit": unit,
            "location": unit.location,
            "upc": unit.upc,
            "sku": unit.sku,
            "description": unit.description,
            "style": unit.style,
            "color": unit.color,
            "size": unit.size,
            "qty": 1,
            "status": unit.status,
        }
        for allocation, unit in rows
    ]


def units_for_upc(ticket: PickTicket, upc: str) -> list[InventoryUnit]:
    upc = (upc or "").strip()
    return [
        row["unit"]
        for row in ticket_units(ticket)
        if row["upc"] == upc and row["status"] == UnitStatus.RESERVED
    ]


def replacement_candidates(order: Order, upc: str, *, exclude_id: int | None = None) -> list[InventoryUnit]:
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
    )
    units = list(db.session.execute(stmt).scalars().all())
    if exclude_id:
        units = [u for u in units if u.id != exclude_id]
    return units


def substitute_unit(ticket: PickTicket, original: InventoryUnit, replacement: InventoryUnit, *, user=None) -> dict:
    require_open_ticket(ticket)
    order = db.session.get(Order, ticket.order_id)
    if original.upc != replacement.upc:
        raise PickTicketUpdateError("Replacement must be the same UPC.")
    if original.client_id != replacement.client_id or original.client_id != order.client_id:
        raise PickTicketUpdateError("Replacement must be the same Client.")
    if original.warehouse_id != replacement.warehouse_id or original.warehouse_id != order.warehouse_id:
        raise PickTicketUpdateError("Replacement must be the same Warehouse.")
    locked_ticket = db.session.execute(
        select(PickTicket).where(PickTicket.id == ticket.id).with_for_update()
    ).scalar_one()
    require_open_ticket(locked_ticket)
    orig = db.session.execute(
        select(InventoryUnit).where(InventoryUnit.id == original.id).with_for_update()
    ).scalar_one_or_none()
    repl = db.session.execute(
        select(InventoryUnit)
        .where(InventoryUnit.id == replacement.id)
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()
    if orig is None:
        raise PickTicketUpdateError("Original unit is no longer reserved on this ticket.")
    if repl is None:
        raise PickTicketUpdateError(REPLACEMENT_UNAVAILABLE_MESSAGE)
    if orig.status != UnitStatus.RESERVED:
        raise PickTicketUpdateError("Original unit is no longer reserved on this ticket.")
    if repl.status != UnitStatus.AVAILABLE:
        raise PickTicketUpdateError(REPLACEMENT_UNAVAILABLE_MESSAGE)
    allocation = Allocation.query.filter_by(
        inventory_unit_id=orig.id, pick_ticket_id=locked_ticket.id, status=AllocationStatus.ACTIVE
    ).one_or_none()
    if allocation is None:
        raise PickTicketUpdateError("UPC is not on this pick ticket.")
    uid, uname = current_actor()
    old_location = orig.location
    line = db.session.get(OrderLine, allocation.order_line_id)
    transition_unit(
        orig,
        to_status=UnitStatus.ISSUE_HOLD,
        transaction_type=LedgerType.ISSUE_HOLD,
        user_id=uid,
        order_id=order.id,
        order_line_id=line.id if line else None,
        allocation_id=allocation.id,
        pick_ticket_id=locked_ticket.id,
        clear_allocation=True,
        reference=f"issue:{locked_ticket.pick_ticket_number}",
    )
    allocation.status = AllocationStatus.RELEASED
    new_alloc = Allocation(
        client_id=order.client_id,
        order_id=order.id,
        order_line_id=allocation.order_line_id,
        inventory_unit_id=repl.id,
        warehouse_id=order.warehouse_id,
        upc=repl.upc,
        location=repl.location,
        status=AllocationStatus.ACTIVE,
        wave_number=allocation.wave_number,
        pick_ticket_id=locked_ticket.id,
        created_by=uid,
    )
    db.session.add(new_alloc)
    db.session.flush()
    transition_unit(
        repl,
        to_status=UnitStatus.RESERVED,
        transaction_type=LedgerType.RESERVE,
        user_id=uid,
        order_id=order.id,
        order_line_id=line.id if line else None,
        allocation_id=new_alloc.id,
        pick_ticket_id=locked_ticket.id,
        allocated_order_id=order.id,
        reference=f"replace:{locked_ticket.pick_ticket_number}",
    )
    repl.allocation_id = new_alloc.id
    issue = InventoryIssue(
        client_id=order.client_id,
        warehouse_id=order.warehouse_id,
        inventory_unit_id=orig.id,
        upc=orig.upc,
        original_location=old_location,
        current_location=orig.location,
        source_order_id=order.id,
        source_order_line_id=allocation.order_line_id,
        source_pick_ticket_id=locked_ticket.id,
        replacement_inventory_unit_id=repl.id,
        issue_type=InventoryIssueType.PICK_UNIT_NOT_FOUND,
        status="OPEN",
        reported_by_user_id=uid,
    )
    db.session.add(issue)
    locked_ticket.revision_number = int(locked_ticket.revision_number or 1) + 1
    record_audit(
        "PICK_TICKET_UNIT_EXCEPTION",
        module="Orders",
        entity_type="pick_ticket",
        entity_id=locked_ticket.id,
        client_id=order.client_id,
        detail=f"{locked_ticket.pick_ticket_number} upc={orig.upc} unit={orig.id}",
    )
    record_audit(
        "PICK_TICKET_UNIT_REPLACED",
        module="Orders",
        entity_type="pick_ticket",
        entity_id=locked_ticket.id,
        client_id=order.client_id,
        detail=f"{orig.id}@{old_location} -> {repl.id}@{repl.location}",
    )
    record_audit(
        "PICK_TICKET_REVISION_CREATED",
        module="Orders",
        entity_type="pick_ticket",
        entity_id=locked_ticket.id,
        client_id=order.client_id,
        detail=f"{locked_ticket.pick_ticket_number} revision={locked_ticket.revision_number}",
    )
    record_audit(
        "INVENTORY_ISSUE_CREATED",
        module="Inventory",
        entity_type="inventory_issue",
        entity_id=None,
        client_id=order.client_id,
        detail=f"unit={orig.id} ticket={locked_ticket.pick_ticket_number}",
    )
    db.session.commit()
    db.session.refresh(issue)
    return {
        "ticket": locked_ticket,
        "issue": issue,
        "original": orig,
        "replacement": repl,
        "revision": locked_ticket.revision_number,
    }
