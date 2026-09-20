"""Centralized inventory-movement ledger writes.

Denormalizes client/warehouse/upc onto each movement so the Transactions tab
can filter without expensive joins.
"""

from __future__ import annotations

from ..extensions import db
from ..models import InventoryMovement, InventoryUnit


def record_movement(
    unit: InventoryUnit,
    *,
    to_status: str,
    movement_type: str,
    from_status: str | None = None,
    from_location: str | None = None,
    to_location: str | None = None,
    order_id: int | None = None,
    actor: str = "system",
    reason: str | None = None,
) -> InventoryMovement:
    movement = InventoryMovement(
        inventory_unit_id=unit.id,
        barcode=unit.barcode,
        upc=unit.upc,
        client_id=unit.client_id,
        warehouse_id=unit.warehouse_id,
        movement_type=movement_type,
        from_status=from_status if from_status is not None else unit.status,
        to_status=to_status,
        from_location=from_location if from_location is not None else unit.location,
        to_location=to_location if to_location is not None else unit.location,
        order_id=order_id,
        actor=actor,
        reason=reason,
    )
    db.session.add(movement)
    return movement
