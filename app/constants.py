"""Shared enums/constants for the WMS domain."""

from __future__ import annotations


class OrderStatus:
    NEW = "NEW"
    VALIDATED = "VALIDATED"
    ALLOCATING = "ALLOCATING"
    ALLOCATED = "ALLOCATED"
    READY_TO_PICK = "READY_TO_PICK"
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"
    READY_TO_CLOSE = "READY_TO_CLOSE"
    CLOSED = "CLOSED"

    ORDER = [
        NEW,
        VALIDATED,
        ALLOCATING,
        ALLOCATED,
        READY_TO_PICK,
        PROCESSING,
        PROCESSED,
        READY_TO_CLOSE,
        CLOSED,
    ]


# Allowed forward transitions for the order workflow state machine.
ORDER_TRANSITIONS = {
    OrderStatus.NEW: {OrderStatus.VALIDATED},
    OrderStatus.VALIDATED: {OrderStatus.ALLOCATING},
    OrderStatus.ALLOCATING: {OrderStatus.ALLOCATED},
    OrderStatus.ALLOCATED: {OrderStatus.READY_TO_PICK},
    OrderStatus.READY_TO_PICK: {OrderStatus.PROCESSING},
    OrderStatus.PROCESSING: {OrderStatus.PROCESSED},
    OrderStatus.PROCESSED: {OrderStatus.READY_TO_CLOSE},
    OrderStatus.READY_TO_CLOSE: {OrderStatus.CLOSED},
    OrderStatus.CLOSED: set(),
}


class UnitStatus:
    IN_STOCK = "IN_STOCK"
    ALLOCATED = "ALLOCATED"
    PACKED = "PACKED"
    SHIPPED = "SHIPPED"


class AllocationStatus:
    ACTIVE = "ACTIVE"
    RELEASED = "RELEASED"


class BoxStatus:
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class DocumentType:
    PICK_TICKET = "PICK_TICKET"
    ORDER_CLOSURE = "ORDER_CLOSURE"
    PACKING_REPORT = "PACKING_REPORT"
    BOX_DETAIL = "BOX_DETAIL"


class ImportType:
    INVENTORY = "INVENTORY"
    ORDERS = "ORDERS"


class ExceptionType:
    DUPLICATE_SCAN = "DUPLICATE_SCAN"
    WRONG_ORDER = "WRONG_ORDER"
    UNIT_IN_OTHER_ACTIVE_ORDER = "UNIT_IN_OTHER_ACTIVE_ORDER"
    UNIT_ALREADY_BOXED = "UNIT_ALREADY_BOXED"
    UNKNOWN_BARCODE = "UNKNOWN_BARCODE"
    NO_DEMAND = "NO_DEMAND"
    RECONCILIATION = "RECONCILIATION"
