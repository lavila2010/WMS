"""Inventory issue resolution for ISSUE_HOLD units."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import or_

from ..auth import current_actor, record_audit
from ..constants import InventoryIssueStatus, LedgerType, UnitStatus
from ..extensions import db
from ..models import Client, InventoryIssue, InventoryUnit, Order, PickTicket, Warehouse
from .inventory_ledger import transition_unit


class InventoryIssueError(ValueError):
    pass


def list_issues(
    *,
    client_id=None,
    warehouse_id=None,
    status=InventoryIssueStatus.OPEN,
    upc="",
    location="",
    date_str="",
    q="",
    accessible_client_ids=None,
    admin=False,
):
    query = InventoryIssue.query
    if client_id:
        query = query.filter(InventoryIssue.client_id == client_id)
    elif not admin:
        query = query.filter(InventoryIssue.client_id.in_(accessible_client_ids or [-1]))
    if warehouse_id:
        query = query.filter(InventoryIssue.warehouse_id == warehouse_id)
    if status:
        query = query.filter(InventoryIssue.status == status)
    if upc:
        query = query.filter(InventoryIssue.upc.ilike(f"%{upc.strip()}%"))
    if location:
        query = query.filter(InventoryIssue.original_location.ilike(f"%{location.strip()}%"))
    if date_str:
        from datetime import timedelta

        day = datetime.strptime(date_str, "%Y-%m-%d")
        query = query.filter(InventoryIssue.reported_at >= day, InventoryIssue.reported_at < day + timedelta(days=1))
    if q:
        like = f"%{q.strip()}%"
        query = query.outerjoin(Order, Order.id == InventoryIssue.source_order_id)
        query = query.outerjoin(PickTicket, PickTicket.id == InventoryIssue.source_pick_ticket_id)
        query = query.filter(
            or_(
                InventoryIssue.upc.ilike(like),
                Order.wms_order_id.ilike(like),
                PickTicket.pick_ticket_number.ilike(like),
            )
        )
    return query.order_by(InventoryIssue.reported_at.desc(), InventoryIssue.id.desc()).all()


def resolve_found(issue: InventoryIssue, *, location: str, note: str = "", user=None) -> InventoryIssue:
    location = (location or "").strip()
    if not location:
        raise InventoryIssueError("Resolved location is required.")
    unit = _lock_unit(issue)
    if unit.status != UnitStatus.ISSUE_HOLD:
        raise InventoryIssueError("Issue unit is not on hold.")
    uid, _ = current_actor()
    unit.location = location
    transition_unit(
        unit,
        to_status=UnitStatus.AVAILABLE,
        transaction_type=LedgerType.ISSUE_RESOLVED,
        user_id=uid,
        order_id=issue.source_order_id,
        pick_ticket_id=issue.source_pick_ticket_id,
        reference=f"issue-resolved:{issue.id}",
    )
    _close_issue(issue, InventoryIssueStatus.RESOLVED, uid, note, location)
    record_audit(
        "INVENTORY_ISSUE_RESOLVED",
        module="Inventory",
        entity_type="inventory_issue",
        entity_id=issue.id,
        client_id=issue.client_id,
        detail=f"unit={unit.id} location={location}",
    )
    db.session.commit()
    return issue


def confirm_missing(issue: InventoryIssue, *, note: str = "", user=None) -> InventoryIssue:
    unit = _lock_unit(issue)
    if unit.status != UnitStatus.ISSUE_HOLD:
        raise InventoryIssueError("Issue unit is not on hold.")
    uid, _ = current_actor()
    transition_unit(
        unit,
        to_status=UnitStatus.MISSING,
        transaction_type=LedgerType.MISSING_CONFIRMED,
        user_id=uid,
        order_id=issue.source_order_id,
        pick_ticket_id=issue.source_pick_ticket_id,
        reference=f"missing:{issue.id}",
    )
    _close_issue(issue, InventoryIssueStatus.MISSING, uid, note, unit.location)
    record_audit(
        "INVENTORY_UNIT_MISSING",
        module="Inventory",
        entity_type="inventory_unit",
        entity_id=unit.id,
        client_id=issue.client_id,
        detail=f"issue={issue.id}",
    )
    db.session.commit()
    return issue


def decommission_unit(issue: InventoryIssue, *, note: str = "", user=None) -> InventoryIssue:
    unit = _lock_unit(issue)
    if unit.status not in {UnitStatus.ISSUE_HOLD, UnitStatus.MISSING}:
        raise InventoryIssueError("Only hold or missing units can be decommissioned.")
    uid, _ = current_actor()
    transition_unit(
        unit,
        to_status=UnitStatus.DECOMMISSIONED,
        transaction_type=LedgerType.UNIT_DECOMMISSIONED,
        user_id=uid,
        order_id=issue.source_order_id,
        pick_ticket_id=issue.source_pick_ticket_id,
        reference=f"decommission:{issue.id}",
    )
    _close_issue(issue, InventoryIssueStatus.TO_DELETE, uid, note, unit.location)
    record_audit(
        "INVENTORY_UNIT_DECOMMISSIONED",
        module="Inventory",
        entity_type="inventory_unit",
        entity_id=unit.id,
        client_id=issue.client_id,
        detail=f"issue={issue.id} unit={unit.id}",
    )
    db.session.commit()
    return issue


def _lock_unit(issue: InventoryIssue) -> InventoryUnit:
    from sqlalchemy import select

    unit = db.session.execute(
        select(InventoryUnit).where(InventoryUnit.id == issue.inventory_unit_id).with_for_update()
    ).scalar_one_or_none()
    if unit is None:
        raise InventoryIssueError("Inventory unit is missing.")
    return unit


def _close_issue(issue: InventoryIssue, status: str, user_id, note: str, location: str | None):
    issue.status = status
    issue.resolved_by_user_id = user_id
    issue.resolved_at = datetime.utcnow()
    issue.resolution_note = (note or "").strip() or None
    issue.current_location = location
    issue.updated_at = datetime.utcnow()
