"""SQLAlchemy models for the WMS.

All operational data lives in PostgreSQL. Enum-like fields are stored as
plain strings validated against the constants in ``app.constants``.
"""

from __future__ import annotations

from datetime import datetime

from .constants import (
    AllocationStatus,
    BoxStatus,
    OrderStatus,
    UnitStatus,
)
from .extensions import db


def _utcnow() -> datetime:
    return datetime.utcnow()


class ImportBatch(db.Model):
    __tablename__ = "import_batches"

    id = db.Column(db.Integer, primary_key=True)
    type = db.Column(db.String(20), nullable=False)
    filename = db.Column(db.String(255), nullable=False)
    row_count = db.Column(db.Integer, default=0, nullable=False)
    status = db.Column(db.String(20), default="COMPLETED", nullable=False)
    message = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)


class InventoryUnit(db.Model):
    """A single physical unit, uniquely identified by its barcode."""

    __tablename__ = "inventory_units"

    id = db.Column(db.Integer, primary_key=True)
    barcode = db.Column(db.String(128), unique=True, nullable=False, index=True)
    sku = db.Column(db.String(64), nullable=False, index=True)
    description = db.Column(db.String(255))
    location = db.Column(db.String(64))
    status = db.Column(db.String(20), default=UnitStatus.IN_STOCK, nullable=False)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"), nullable=True)
    import_batch_id = db.Column(
        db.Integer, db.ForeignKey("import_batches.id"), nullable=True
    )
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)

    order = db.relationship("Order", backref="units")


class Order(db.Model):
    __tablename__ = "orders"

    id = db.Column(db.Integer, primary_key=True)
    order_number = db.Column(db.String(64), unique=True, nullable=False, index=True)
    customer = db.Column(db.String(255))
    status = db.Column(db.String(20), default=OrderStatus.NEW, nullable=False, index=True)
    notes = db.Column(db.Text)
    import_batch_id = db.Column(
        db.Integer, db.ForeignKey("import_batches.id"), nullable=True
    )
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)

    lines = db.relationship(
        "OrderLine", backref="order", cascade="all, delete-orphan"
    )

    @property
    def ordered_quantity(self) -> int:
        return sum(line.quantity for line in self.lines)


class OrderLine(db.Model):
    __tablename__ = "order_lines"

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"), nullable=False)
    sku = db.Column(db.String(64), nullable=False)
    description = db.Column(db.String(255))
    quantity = db.Column(db.Integer, default=0, nullable=False)


class Allocation(db.Model):
    """Barcode-level allocation of an inventory unit to an order."""

    __tablename__ = "allocations"
    __table_args__ = (
        # A unit can only have one ACTIVE allocation at a time. Enforced
        # in code; a partial unique index would require raw DDL, so this is
        # validated by the allocation service (see app/services/allocation.py).
        db.Index("ix_allocations_unit_status", "inventory_unit_id", "status"),
    )

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"), nullable=False)
    order_line_id = db.Column(
        db.Integer, db.ForeignKey("order_lines.id"), nullable=True
    )
    inventory_unit_id = db.Column(
        db.Integer, db.ForeignKey("inventory_units.id"), nullable=False
    )
    barcode = db.Column(db.String(128), nullable=False, index=True)
    status = db.Column(
        db.String(20), default=AllocationStatus.ACTIVE, nullable=False
    )
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)

    order = db.relationship("Order", backref="allocations")
    order_line = db.relationship("OrderLine", backref="allocations")
    unit = db.relationship("InventoryUnit", backref="allocations")


class Box(db.Model):
    __tablename__ = "boxes"

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"), nullable=False)
    box_number = db.Column(db.String(64), nullable=False)
    length_cm = db.Column(db.Float)
    width_cm = db.Column(db.Float)
    height_cm = db.Column(db.Float)
    weight_kg = db.Column(db.Float)
    status = db.Column(db.String(20), default=BoxStatus.OPEN, nullable=False)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    closed_at = db.Column(db.DateTime)

    order = db.relationship("Order", backref="boxes")
    contents = db.relationship(
        "BoxContent", backref="box", cascade="all, delete-orphan"
    )


class BoxContent(db.Model):
    __tablename__ = "box_contents"
    __table_args__ = (
        # A unit cannot be in two boxes.
        db.UniqueConstraint("inventory_unit_id", name="uq_box_contents_unit"),
    )

    id = db.Column(db.Integer, primary_key=True)
    box_id = db.Column(db.Integer, db.ForeignKey("boxes.id"), nullable=False)
    inventory_unit_id = db.Column(
        db.Integer, db.ForeignKey("inventory_units.id"), nullable=False
    )
    barcode = db.Column(db.String(128), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)

    unit = db.relationship("InventoryUnit", backref="box_contents")


class Transaction(db.Model):
    """Append-only ledger of operational events."""

    __tablename__ = "transactions"

    id = db.Column(db.Integer, primary_key=True)
    type = db.Column(db.String(48), nullable=False)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"), nullable=True)
    inventory_unit_id = db.Column(
        db.Integer, db.ForeignKey("inventory_units.id"), nullable=True
    )
    barcode = db.Column(db.String(128))
    quantity = db.Column(db.Integer)
    detail = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)


class InventoryMovement(db.Model):
    __tablename__ = "inventory_movements"

    id = db.Column(db.Integer, primary_key=True)
    inventory_unit_id = db.Column(
        db.Integer, db.ForeignKey("inventory_units.id"), nullable=False
    )
    barcode = db.Column(db.String(128))
    from_status = db.Column(db.String(20))
    to_status = db.Column(db.String(20))
    from_location = db.Column(db.String(64))
    to_location = db.Column(db.String(64))
    reason = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)


class OrderException(db.Model):
    __tablename__ = "order_exceptions"

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"), nullable=True)
    barcode = db.Column(db.String(128))
    type = db.Column(db.String(48), nullable=False)
    message = db.Column(db.Text)
    resolved = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)

    order = db.relationship("Order", backref="exceptions")


class Document(db.Model):
    __tablename__ = "documents"

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"), nullable=True)
    box_id = db.Column(db.Integer, db.ForeignKey("boxes.id"), nullable=True)
    type = db.Column(db.String(48), nullable=False)
    filename = db.Column(db.String(255), nullable=False)
    path = db.Column(db.String(512), nullable=False)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)

    order = db.relationship("Order", backref="documents")
    box = db.relationship("Box", backref="documents")
