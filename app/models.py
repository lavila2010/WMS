"""WMS V2 SQLAlchemy models."""

from __future__ import annotations

from datetime import datetime

from .constants import AllocationStatus, CartonStatus, OrderStatus, Role, UnitStatus
from .extensions import db


def _utcnow() -> datetime:
    return datetime.utcnow()


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    full_name = db.Column(db.String(255))
    email = db.Column(db.String(255))
    role = db.Column(db.String(16), nullable=False, default=Role.USER)
    active = db.Column(db.Boolean, nullable=False, default=True)
    must_change_password = db.Column(db.Boolean, nullable=False, default=True)
    failed_login_count = db.Column(db.Integer, nullable=False, default=0)
    locked_until = db.Column(db.DateTime)
    last_login_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)
    updated_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))

    client_links = db.relationship(
        "UserClient",
        backref="user",
        cascade="all, delete-orphan",
        foreign_keys="UserClient.user_id",
    )

    @property
    def is_active(self) -> bool:
        return bool(self.active)

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def is_anonymous(self) -> bool:
        return False

    def get_id(self) -> str:
        return str(self.id)

    def is_admin(self) -> bool:
        return self.role == Role.ADMIN

    def has_permission(self, code: str) -> bool:
        if self.is_admin():
            return True
        return (
            UserPermission.query.filter_by(
                user_id=self.id, permission_id=_permission_id(code), granted=True
            ).first()
            is not None
        )

    def permission_count(self) -> int:
        if self.is_admin():
            from .permissions import ALL_CODES

            return len(ALL_CODES)
        return UserPermission.query.filter_by(user_id=self.id, granted=True).count()

    def client_ids(self) -> list[int]:
        if self.is_admin():
            return [c.id for c in Client.query.order_by(Client.id).all()]
        return [link.client_id for link in self.client_links]


def _permission_id(code: str):
    perm = Permission.query.filter_by(code=code).first()
    return perm.id if perm else -1


class Permission(db.Model):
    __tablename__ = "permissions"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(64), unique=True, nullable=False, index=True)
    description = db.Column(db.String(255))
    module = db.Column(db.String(64))


class UserPermission(db.Model):
    __tablename__ = "user_permissions"
    __table_args__ = (db.UniqueConstraint("user_id", "permission_id", name="uq_user_permission"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    permission_id = db.Column(db.Integer, db.ForeignKey("permissions.id"), nullable=False)
    granted = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    permission = db.relationship("Permission")


class UserClient(db.Model):
    __tablename__ = "user_clients"
    __table_args__ = (db.UniqueConstraint("user_id", "client_id", name="uq_user_client"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    client = db.relationship("Client")


class AuditEvent(db.Model):
    __tablename__ = "audit_events"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    username = db.Column(db.String(64))
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), index=True)
    event_type = db.Column(db.String(48), nullable=False, index=True)
    module = db.Column(db.String(48))
    entity_type = db.Column(db.String(48))
    entity_id = db.Column(db.String(64))
    ip_address = db.Column(db.String(64))
    detail = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False, index=True)


class Client(db.Model):
    __tablename__ = "clients"

    id = db.Column(db.Integer, primary_key=True)
    sequence_number = db.Column(db.Integer, unique=True, nullable=False)
    client_code = db.Column(db.String(32), unique=True, nullable=False, index=True)
    name = db.Column(db.String(255), nullable=False)
    initials = db.Column(db.String(8), nullable=False)
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)
    updated_by = db.Column(db.Integer, db.ForeignKey("users.id"))

    divisions = db.relationship("Division", backref="client")
    warehouses = db.relationship("Warehouse", backref="client")


class Division(db.Model):
    __tablename__ = "divisions"
    __table_args__ = (
        db.UniqueConstraint("code", name="uq_division_code"),
        db.UniqueConstraint("client_id", "operation_type", name="uq_division_client_op"),
    )

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False, index=True)
    code = db.Column(db.String(64), nullable=False, index=True)
    name = db.Column(db.String(255), nullable=False)
    operation_type = db.Column(db.String(8), nullable=False)
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)
    updated_by = db.Column(db.Integer, db.ForeignKey("users.id"))


class Warehouse(db.Model):
    __tablename__ = "warehouses"
    __table_args__ = (
        db.UniqueConstraint("client_id", "warehouse_symbol", name="uq_warehouse_client_symbol"),
        db.UniqueConstraint("warehouse_code", name="uq_warehouse_code"),
    )

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False, index=True)
    warehouse_symbol = db.Column(db.String(16), nullable=False)
    warehouse_code = db.Column(db.String(64), nullable=False)
    name = db.Column(db.String(255), nullable=False)
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)
    updated_by = db.Column(db.Integer, db.ForeignKey("users.id"))


class DivisionWarehouse(db.Model):
    __tablename__ = "division_warehouses"
    __table_args__ = (
        db.UniqueConstraint("division_id", "warehouse_id", name="uq_division_warehouse"),
    )

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False, index=True)
    division_id = db.Column(db.Integer, db.ForeignKey("divisions.id"), nullable=False)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=False)
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)

    division = db.relationship("Division")
    warehouse = db.relationship("Warehouse")


class ImportBatch(db.Model):
    __tablename__ = "import_batches"

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False, index=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"))
    division_id = db.Column(db.Integer, db.ForeignKey("divisions.id"))
    type = db.Column(db.String(20), nullable=False)
    filename = db.Column(db.String(255), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="COMPLETED")
    rows_submitted = db.Column(db.Integer, nullable=False, default=0)
    rows_imported = db.Column(db.Integer, nullable=False, default=0)
    rows_rejected = db.Column(db.Integer, nullable=False, default=0)
    message = db.Column(db.Text)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_by_username = db.Column(db.String(64))
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)


class InventoryUnit(db.Model):
    __tablename__ = "inventory_units"
    __table_args__ = (
        db.Index("ix_units_cwu", "client_id", "warehouse_id", "upc"),
        db.Index("ix_units_cwuls", "client_id", "warehouse_id", "upc", "location", "status"),
    )

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False, index=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=False, index=True)
    upc = db.Column(db.String(64), nullable=False, index=True)
    sku = db.Column(db.String(64))
    style = db.Column(db.String(64))
    color = db.Column(db.String(64))
    size = db.Column(db.String(32))
    location = db.Column(db.String(64), nullable=False)
    status = db.Column(db.String(20), nullable=False, default=UnitStatus.AVAILABLE)
    allocated_order_id = db.Column(
        db.Integer, db.ForeignKey("orders.id", use_alter=True, name="fk_unit_order")
    )
    allocation_id = db.Column(
        db.Integer, db.ForeignKey("allocations.id", use_alter=True, name="fk_unit_allocation")
    )
    carton_id = db.Column(
        db.Integer, db.ForeignKey("cartons.id", use_alter=True, name="fk_unit_carton")
    )
    import_batch_id = db.Column(db.Integer, db.ForeignKey("import_batches.id"))
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)


class Order(db.Model):
    __tablename__ = "orders"
    __table_args__ = (
        db.UniqueConstraint("client_id", "client_order_number", name="uq_order_client_number"),
    )

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False, index=True)
    division_id = db.Column(db.Integer, db.ForeignKey("divisions.id"), nullable=False, index=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=False, index=True)
    client_order_number = db.Column(db.String(64), nullable=False, index=True)
    wms_order_id = db.Column(db.String(96), unique=True, nullable=False)
    customer = db.Column(db.String(255))
    customer_address = db.Column(db.String(512))
    customer_phone = db.Column(db.String(64))
    carrier = db.Column(db.String(128))
    shipping_service = db.Column(db.String(128))
    status = db.Column(db.String(24), nullable=False, default=OrderStatus.UNALLOCATED, index=True)
    processing_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    processing_username = db.Column(db.String(64))
    processing_started_at = db.Column(db.DateTime)
    processing_lock_id = db.Column(db.String(64), index=True)
    import_batch_id = db.Column(db.Integer, db.ForeignKey("import_batches.id"))
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False, index=True)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)
    closed_at = db.Column(db.DateTime)
    closed_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    closed_by_username = db.Column(db.String(64))

    lines = db.relationship("OrderLine", backref="order", cascade="all, delete-orphan")
    client = db.relationship("Client")
    warehouse = db.relationship("Warehouse")
    division = db.relationship("Division")


class OrderLine(db.Model):
    __tablename__ = "order_lines"

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"), nullable=False, index=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False, index=True)
    upc = db.Column(db.String(64), nullable=False)
    sku = db.Column(db.String(64))
    description = db.Column(db.String(255))
    qty_ordered = db.Column(db.Integer, nullable=False)
    qty_allocated = db.Column(db.Integer, nullable=False, default=0)
    qty_packed = db.Column(db.Integer, nullable=False, default=0)
    qty_shipped = db.Column(db.Integer, nullable=False, default=0)


class Allocation(db.Model):
    __tablename__ = "allocations"
    __table_args__ = (db.Index("ix_alloc_unit_status", "inventory_unit_id", "status"),)

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False, index=True)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"), nullable=False, index=True)
    order_line_id = db.Column(db.Integer, db.ForeignKey("order_lines.id"), nullable=False)
    inventory_unit_id = db.Column(db.Integer, db.ForeignKey("inventory_units.id"), nullable=False)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=False)
    upc = db.Column(db.String(64), nullable=False)
    location = db.Column(db.String(64), nullable=False)
    status = db.Column(db.String(16), nullable=False, default=AllocationStatus.ACTIVE)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))


class InventoryTransaction(db.Model):
    __tablename__ = "inventory_transactions"

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False, index=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=False)
    inventory_unit_id = db.Column(db.Integer, db.ForeignKey("inventory_units.id"), nullable=False, index=True)
    upc = db.Column(db.String(64), nullable=False)
    location = db.Column(db.String(64), nullable=False)
    transaction_type = db.Column(db.String(24), nullable=False)
    from_status = db.Column(db.String(20))
    to_status = db.Column(db.String(20), nullable=False)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"), index=True)
    order_line_id = db.Column(db.Integer, db.ForeignKey("order_lines.id"))
    allocation_id = db.Column(db.Integer, db.ForeignKey("allocations.id"))
    pick_ticket_id = db.Column(db.Integer, db.ForeignKey("pick_tickets.id"))
    carton_id = db.Column(db.Integer, db.ForeignKey("cartons.id"))
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    reference = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False, index=True)


class PickTicket(db.Model):
    __tablename__ = "pick_tickets"

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=False)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"), unique=True, nullable=False)
    pick_ticket_number = db.Column(db.String(96), unique=True, nullable=False, index=True)
    ticket_sequence = db.Column(db.Integer, nullable=False, default=1)
    status = db.Column(db.String(16), nullable=False, default="ACTIVE")
    print_count = db.Column(db.Integer, nullable=False, default=0)
    last_printed_at = db.Column(db.DateTime)
    last_printed_by = db.Column(db.String(64))
    document_id = db.Column(db.Integer, db.ForeignKey("documents.id"))
    assigned_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    assigned_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)


class PickTicketPrintEvent(db.Model):
    __tablename__ = "pick_ticket_print_events"

    id = db.Column(db.Integer, primary_key=True)
    pick_ticket_id = db.Column(db.Integer, db.ForeignKey("pick_tickets.id"), nullable=False, index=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    username = db.Column(db.String(64))
    printed_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    source = db.Column(db.String(20), nullable=False)
    ip_address = db.Column(db.String(64))


class Carton(db.Model):
    __tablename__ = "cartons"
    __table_args__ = (db.UniqueConstraint("order_id", "carton_number", name="uq_carton_order_number"),)

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"), nullable=False)
    carton_number = db.Column(db.String(64), nullable=False)
    length = db.Column(db.Float)
    width = db.Column(db.Float)
    height = db.Column(db.Float)
    dimension_unit = db.Column(db.String(8), nullable=False, default="in")
    weight = db.Column(db.Float)
    weight_unit = db.Column(db.String(8), nullable=False, default="lb")
    previous_weight = db.Column(db.Float)
    reweigh_required = db.Column(db.Boolean, nullable=False, default=False)
    status = db.Column(db.String(20), nullable=False, default=CartonStatus.OPEN)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_by_username = db.Column(db.String(64))
    closed_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    closed_by_username = db.Column(db.String(64))
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    closed_at = db.Column(db.DateTime)
    contents = db.relationship("CartonContent", backref="carton", cascade="all, delete-orphan")


class CartonContent(db.Model):
    __tablename__ = "carton_contents"
    __table_args__ = (db.UniqueConstraint("inventory_unit_id", name="uq_carton_unit"),)

    id = db.Column(db.Integer, primary_key=True)
    carton_id = db.Column(db.Integer, db.ForeignKey("cartons.id"), nullable=False)
    inventory_unit_id = db.Column(db.Integer, db.ForeignKey("inventory_units.id"), nullable=False)
    upc = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)


class Document(db.Model):
    __tablename__ = "documents"

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"))
    carton_id = db.Column(db.Integer, db.ForeignKey("cartons.id"))
    type = db.Column(db.String(48), nullable=False)
    filename = db.Column(db.String(255), nullable=False)
    storage_key = db.Column(db.String(512), nullable=False)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_by_username = db.Column(db.String(64))
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)


class Invoice(db.Model):
    __tablename__ = "invoices"
    __table_args__ = (db.UniqueConstraint("order_id", name="uq_invoice_order"),)

    id = db.Column(db.Integer, primary_key=True)
    invoice_number = db.Column(db.String(64), unique=True, nullable=False)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"), nullable=False)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=False)
    division_id = db.Column(db.Integer, db.ForeignKey("divisions.id"), nullable=False)
    customer = db.Column(db.String(255))
    carrier = db.Column(db.String(128))
    shipping_service = db.Column(db.String(128))
    total_units = db.Column(db.Integer, nullable=False, default=0)
    total_cartons = db.Column(db.Integer, nullable=False, default=0)
    total_weight = db.Column(db.Float, nullable=False, default=0.0)
    status = db.Column(db.String(20), nullable=False, default="ISSUED")
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_by_username = db.Column(db.String(64))
