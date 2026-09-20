"""The only permitted inventory-unit mutation path.

Every status change writes one immutable ``inventory_transactions`` row in the
same PostgreSQL transaction. Callers must never assign ``InventoryUnit.status``
directly.
"""

from __future__ import annotations

from ..constants import LedgerType, UnitStatus
from ..extensions import db
from ..models import InventoryTransaction, InventoryUnit, Warehouse


class LedgerError(ValueError):
    pass


ALLOWED = {
    (None, UnitStatus.AVAILABLE): {LedgerType.IMPORT, LedgerType.TRANSFER_IN, LedgerType.RETURN},
    (UnitStatus.AVAILABLE, UnitStatus.RESERVED): {LedgerType.RESERVE},
    (UnitStatus.RESERVED, UnitStatus.AVAILABLE): {LedgerType.UNRESERVE},
    (UnitStatus.RESERVED, UnitStatus.PACKED): {LedgerType.PACK},
    (UnitStatus.PACKED, UnitStatus.RESERVED): {LedgerType.UNPACK},
    (UnitStatus.PACKED, UnitStatus.SHIPPED): {LedgerType.SHIP},
    (UnitStatus.SHIPPED, UnitStatus.AVAILABLE): {LedgerType.RETURN},
    (UnitStatus.AVAILABLE, UnitStatus.AVAILABLE): {LedgerType.ADJUSTMENT, LedgerType.TRANSFER_OUT},
}


def _require_warehouse(client_id: int, warehouse_id: int) -> Warehouse:
    warehouse = db.session.get(Warehouse, warehouse_id)
    if warehouse is None or not warehouse.active:
        raise LedgerError("Warehouse is unknown or inactive.")
    if warehouse.client_id != client_id:
        raise LedgerError("Warehouse does not belong to the selected client.")
    return warehouse


def record_transaction(
    unit: InventoryUnit,
    *,
    transaction_type: str,
    from_status: str | None,
    to_status: str,
    user_id=None,
    reference=None,
    order_id=None,
    order_line_id=None,
    allocation_id=None,
    pick_ticket_id=None,
    carton_id=None,
) -> InventoryTransaction:
    txn = InventoryTransaction(
        client_id=unit.client_id,
        warehouse_id=unit.warehouse_id,
        inventory_unit_id=unit.id,
        import_batch_id=unit.import_batch_id,
        upc=unit.upc,
        location=unit.location,
        transaction_type=transaction_type,
        from_status=from_status,
        to_status=to_status,
        order_id=order_id,
        order_line_id=order_line_id,
        allocation_id=allocation_id,
        pick_ticket_id=pick_ticket_id,
        carton_id=carton_id,
        user_id=user_id,
        reference=reference,
    )
    db.session.add(txn)
    db.session.flush()
    return txn


def create_available_unit(
    *,
    client_id: int,
    warehouse_id: int,
    upc: str,
    location: str,
    sku=None,
    description=None,
    style=None,
    color=None,
    size=None,
    import_batch_id=None,
    user_id=None,
    reference=None,
    transaction_type: str = LedgerType.IMPORT,
) -> InventoryUnit:
    upc = (upc or "").strip()
    location = (location or "").strip()
    if not upc:
        raise LedgerError("UPC is required.")
    if not location:
        raise LedgerError("Location is required.")
    _require_warehouse(client_id, warehouse_id)
    allowed = ALLOWED.get((None, UnitStatus.AVAILABLE), set())
    if transaction_type not in allowed:
        raise LedgerError(f"Invalid create ledger type {transaction_type}.")
    unit = InventoryUnit(
        client_id=client_id,
        warehouse_id=warehouse_id,
        upc=upc,
        sku=(sku or "").strip() or None,
        description=(description or "").strip() or None,
        style=(style or "").strip() or None,
        color=(color or "").strip() or None,
        size=(size or "").strip() or None,
        location=location,
        status=UnitStatus.AVAILABLE,
        import_batch_id=import_batch_id,
    )
    db.session.add(unit)
    db.session.flush()
    record_transaction(
        unit,
        transaction_type=transaction_type,
        from_status=None,
        to_status=UnitStatus.AVAILABLE,
        user_id=user_id,
        reference=reference,
    )
    return unit


def transition_unit(
    unit: InventoryUnit,
    *,
    to_status: str,
    transaction_type: str,
    user_id=None,
    reference=None,
    order_id=None,
    order_line_id=None,
    allocation_id=None,
    pick_ticket_id=None,
    carton_id=None,
    allocated_order_id=None,
    clear_allocation=False,
    clear_carton=False,
) -> InventoryTransaction:
    if unit is None:
        raise LedgerError("Inventory unit is required.")
    from_status = unit.status
    allowed = ALLOWED.get((from_status, to_status))
    if not allowed or transaction_type not in allowed:
        raise LedgerError(
            f"Illegal transition {from_status} → {to_status} as {transaction_type}."
        )
    unit.status = to_status
    if allocated_order_id is not None:
        unit.allocated_order_id = allocated_order_id
    if allocation_id is not None:
        unit.allocation_id = allocation_id
    if carton_id is not None:
        unit.carton_id = carton_id
    if clear_allocation:
        unit.allocated_order_id = None
        unit.allocation_id = None
    if clear_carton:
        unit.carton_id = None
    db.session.flush()
    return record_transaction(
        unit,
        transaction_type=transaction_type,
        from_status=from_status,
        to_status=to_status,
        user_id=user_id,
        reference=reference,
        order_id=order_id,
        order_line_id=order_line_id,
        allocation_id=allocation_id,
        pick_ticket_id=pick_ticket_id,
        carton_id=carton_id if carton_id is not None else unit.carton_id,
    )
