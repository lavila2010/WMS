"""V2 enums and constants."""

from __future__ import annotations


class Role:
    ADMIN = "ADMIN"
    USER = "USER"
    ALL = [ADMIN, USER]


class OperationType:
    ECOM = "ECOM"
    RTL = "RTL"
    WHLS = "WHLS"
    ALL = [ECOM, RTL, WHLS]
    CHANNEL = {
        ECOM: "ECOMMERCE",
        RTL: "RETAIL",
        WHLS: "WHOLESALE",
    }


class UnitStatus:
    AVAILABLE = "AVAILABLE"
    RESERVED = "RESERVED"
    PACKED = "PACKED"
    SHIPPED = "SHIPPED"
    ALL = [AVAILABLE, RESERVED, PACKED, SHIPPED]
    ON_HAND = [AVAILABLE, RESERVED, PACKED]


class OrderStatus:
    UNALLOCATED = "UNALLOCATED"
    PARTIALLY_ALLOCATED = "PARTIALLY_ALLOCATED"
    ALLOCATED = "ALLOCATED"
    PICK_TICKET_READY = "PICK_TICKET_READY"
    PROCESSING = "PROCESSING"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"
    ALL = [
        UNALLOCATED,
        PARTIALLY_ALLOCATED,
        ALLOCATED,
        PICK_TICKET_READY,
        PROCESSING,
        CLOSED,
        CANCELLED,
    ]


class AllocationStatus:
    ACTIVE = "ACTIVE"
    RELEASED = "RELEASED"


class CartonStatus:
    OPEN = "OPEN"
    AWAITING_WEIGHT = "AWAITING_WEIGHT"
    REWEIGH_REQUIRED = "REWEIGH_REQUIRED"
    CLOSED = "CLOSED"


class LedgerType:
    IMPORT = "IMPORT"
    ADJUSTMENT = "ADJUSTMENT"
    RESERVE = "RESERVE"
    UNRESERVE = "UNRESERVE"
    PACK = "PACK"
    UNPACK = "UNPACK"
    SHIP = "SHIP"
    RETURN = "RETURN"
    TRANSFER_IN = "TRANSFER_IN"
    TRANSFER_OUT = "TRANSFER_OUT"


class ImportType:
    INVENTORY = "INVENTORY"
    ORDERS = "ORDERS"


APP_VERSION = "2.0.0"
