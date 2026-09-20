"""SQLAlchemy models for the WMS.

All operational data lives in PostgreSQL. Enum-like fields are stored as
plain strings validated against the constants in ``app.constants``.
"""

from __future__ import annotations

from datetime import datetime

from .constants import (
    AllocationStatus,
    BoxStatus,
    InvoiceStatus,
    OrderStatus,
    UnitStatus,
)
from .extensions import db


def _utcnow() -> datetime:
    return datetime.utcnow()


class Client(db.Model):
    __tablename__ = "clients"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(32), unique=True, nullable=False, index=True)
    name = db.Column(db.String(255), nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)

    warehouses = db.relationship("Warehouse", backref="client")
    order_types = db.relationship("OrderType", backref="client")


class Warehouse(db.Model):
    __tablename__ = "warehouses"
    __table_args__ = (
        db.UniqueConstraint("client_id", "code", name="uq_warehouse_client_code"),
    )

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False)
    code = db.Column(db.String(32), nullable=False, index=True)
    name = db.Column(db.String(255), nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)


class OrderType(db.Model):
    __tablename__ = "order_types"
    __table_args__ = (
        db.UniqueConstraint("client_id", "code", name="uq_order_type_client_code"),
    )

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False)
    code = db.Column(db.String(32), nullable=False, index=True)
    name = db.Column(db.String(255), nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)


class ImportBatch(db.Model):
    __tablename__ = "import_batches"

    id = db.Column(db.Integer, primary_key=True)
    type = db.Column(db.String(20), nullable=False)
    filename = db.Column(db.String(255), nullable=False)
    row_count = db.Column(db.Integer, default=0, nullable=False)
    rows_submitted = db.Column(db.Integer, default=0, nullable=False)
    rows_imported = db.Column(db.Integer, default=0, nullable=False)
    rows_updated = db.Column(db.Integer, default=0, nullable=False)
    rows_rejected = db.Column(db.Integer, default=0, nullable=False)
    warnings_count = db.Column(db.Integer, default=0, nullable=False)
    clients = db.Column(db.String(512))
    warehouses = db.Column(db.String(512))
    status = db.Column(db.String(20), default="COMPLETED", nullable=False)
    message = db.Column(db.Text)
    detail = db.Column(db.Text)
    created_by = db.Column(db.String(128))
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)


class InventoryUnit(db.Model):
    """A single physical unit, uniquely identified by its barcode.

    Physical inventory is partitioned ONLY by Client + Warehouse. Order Type is
    NOT an inventory dimension (``order_type_id`` is a deprecated, nullable
    compatibility column that is no longer read, written, or filtered; a later
    migration will drop it).
    """

    __tablename__ = "inventory_units"
    __table_args__ = (
        db.Index("ix_inventory_units_cwu", "client_id", "warehouse_id", "upc"),
        db.Index("ix_inventory_units_cws", "client_id", "warehouse_id", "sku"),
        db.Index("ix_inventory_units_cwl", "client_id", "warehouse_id", "location"),
    )

    id = db.Column(db.Integer, primary_key=True)
    barcode = db.Column(db.String(128), unique=True, nullable=False, index=True)
    upc = db.Column(db.String(64), nullable=False, index=True)
    sku = db.Column(db.String(64), nullable=False, index=True)
    description = db.Column(db.String(255))
    location = db.Column(db.String(64), nullable=False)
    status = db.Column(db.String(20), default=UnitStatus.AVAILABLE, nullable=False)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False, index=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=False, index=True)
    # DEPRECATED: inventory is not scoped by order type. Kept nullable for
    # backward compatibility only; scheduled for removal in a later migration.
    order_type_id = db.Column(db.Integer, db.ForeignKey("order_types.id"), nullable=True)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"), nullable=True)
    import_batch_id = db.Column(
        db.Integer, db.ForeignKey("import_batches.id"), nullable=True
    )
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)

    order = db.relationship("Order", backref="units")
    client = db.relationship("Client")
    warehouse = db.relationship("Warehouse")


class Order(db.Model):
    __tablename__ = "orders"
    __table_args__ = (
        db.UniqueConstraint(
            "client_id",
            "warehouse_id",
            "order_type_id",
            "order_number",
            name="uq_order_scope_number",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    order_number = db.Column(db.String(64), nullable=False, index=True)
    customer = db.Column(db.String(255))
    carrier = db.Column(db.String(128))
    shipping_service = db.Column(db.String(128))
    status = db.Column(db.String(20), default=OrderStatus.NEW, nullable=False, index=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False, index=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=False, index=True)
    order_type_id = db.Column(db.Integer, db.ForeignKey("order_types.id"), nullable=False, index=True)
    notes = db.Column(db.Text)
    import_batch_id = db.Column(
        db.Integer, db.ForeignKey("import_batches.id"), nullable=True
    )
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)

    lines = db.relationship(
        "OrderLine", backref="order", cascade="all, delete-orphan"
    )
    client = db.relationship("Client")
    warehouse = db.relationship("Warehouse")
    order_type = db.relationship("OrderType")
    invoice = db.relationship("Invoice", backref="order", uselist=False)

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
    barcode = db.Column(db.String(128), index=True)
    upc = db.Column(db.String(64))
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=True, index=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=True, index=True)
    movement_type = db.Column(db.String(48))
    from_status = db.Column(db.String(20))
    to_status = db.Column(db.String(20))
    from_location = db.Column(db.String(64))
    to_location = db.Column(db.String(64))
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"), nullable=True)
    actor = db.Column(db.String(128))
    reason = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)

    unit = db.relationship("InventoryUnit")
    client = db.relationship("Client")
    warehouse = db.relationship("Warehouse")
    order = db.relationship("Order")


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


class InventoryException(db.Model):
    """Inventory-specific exceptions, kept separate from order-processing
    exceptions. Scoped by Client + Warehouse (no order type)."""

    __tablename__ = "inventory_exceptions"

    id = db.Column(db.Integer, primary_key=True)
    type = db.Column(db.String(48), nullable=False)
    barcode = db.Column(db.String(128))
    upc = db.Column(db.String(64))
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=True)
    client_code = db.Column(db.String(32))
    warehouse_code = db.Column(db.String(32))
    details = db.Column(db.Text)
    status = db.Column(db.String(20), default="OPEN", nullable=False)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)

    client = db.relationship("Client")
    warehouse = db.relationship("Warehouse")


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


class Invoice(db.Model):
    __tablename__ = "invoices"
    __table_args__ = (
        db.UniqueConstraint("order_id", name="uq_invoice_order"),
    )

    id = db.Column(db.Integer, primary_key=True)
    invoice_number = db.Column(db.String(64), unique=True, nullable=False, index=True)
    order_id = db.Column(
        db.Integer, db.ForeignKey("orders.id"), unique=True, nullable=False
    )
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=False)
    order_type_id = db.Column(db.Integer, db.ForeignKey("order_types.id"), nullable=False)
    customer = db.Column(db.String(255))
    carrier = db.Column(db.String(128))
    shipping_service = db.Column(db.String(128))
    total_units = db.Column(db.Integer, default=0, nullable=False)
    total_boxes = db.Column(db.Integer, default=0, nullable=False)
    total_weight = db.Column(db.Float, default=0.0, nullable=False)
    status = db.Column(db.String(20), default=InvoiceStatus.ISSUED, nullable=False)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    created_by = db.Column(db.String(128))

    client = db.relationship("Client")
    warehouse = db.relationship("Warehouse")
    order_type = db.relationship("OrderType")
