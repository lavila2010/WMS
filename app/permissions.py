"""V2 permission catalog and navigation."""

from __future__ import annotations

from .constants import Role

PERMISSIONS = [
    ("DASHBOARD_VIEW", "View the dashboard", "Dashboard"),
    ("INVENTORY_VIEW", "View inventory", "Inventory"),
    ("INVENTORY_UPLOAD", "Upload inventory", "Inventory"),
    ("INVENTORY_EXPORT", "Export inventory", "Inventory"),
    ("ORDERS_VIEW", "View orders", "Orders"),
    ("ORDERS_UPLOAD", "Upload orders", "Orders"),
    ("PICK_TICKET_VIEW", "View pick tickets", "Orders"),
    ("PICK_TICKET_GENERATE", "Generate pick ticket PDFs", "Orders"),
    ("PICK_TICKET_PRINT", "Print / reprint pick tickets", "Orders"),
    ("ALLOCATION_VIEW", "View allocation", "Allocation"),
    ("ALLOCATION_EXECUTE", "Execute allocation", "Allocation"),
    ("ALLOCATION_RELEASE", "Release allocation", "Allocation"),
    ("PROCESSING_VIEW", "View order processing", "Processing"),
    ("PROCESSING_EXECUTE", "Execute processing / scanning", "Processing"),
    ("BOX_CLOSE", "Close boxes", "Processing"),
    ("ORDER_CLOSE", "Close orders", "Processing"),
    ("REPORTS_VIEW", "View reports", "Reports"),
    ("REPORTS_EXPORT", "Generate / export reports", "Reports"),
    ("DOCUMENT_REPRINT", "Reprint / download documents", "Reports"),
    ("USERS_VIEW", "View users", "Administration"),
    ("USERS_CREATE", "Create users", "Administration"),
    ("USERS_EDIT", "Edit users", "Administration"),
    ("USERS_DISABLE", "Enable / disable users", "Administration"),
    ("PERMISSIONS_ASSIGN", "Assign permissions", "Administration"),
    ("AUDIT_VIEW", "View audit log", "Administration"),
    ("CLIENTS_VIEW", "View clients", "Administration"),
    ("CLIENTS_CREATE", "Create clients", "Administration"),
    ("CLIENTS_EDIT", "Edit clients", "Administration"),
    ("WAREHOUSES_VIEW", "View warehouses", "Administration"),
    ("WAREHOUSES_CREATE", "Create warehouses", "Administration"),
    ("WAREHOUSES_EDIT", "Edit warehouses", "Administration"),
    ("DIVISIONS_VIEW", "View divisions", "Administration"),
    ("DIVISIONS_CREATE", "Create divisions", "Administration"),
    ("DIVISIONS_EDIT", "Edit divisions", "Administration"),
]

ALL_CODES = [p[0] for p in PERMISSIONS]

USER_DEFAULTS = [
    "DASHBOARD_VIEW",
    "INVENTORY_VIEW",
    "ORDERS_VIEW",
    "PICK_TICKET_VIEW",
    "ALLOCATION_VIEW",
    "PROCESSING_VIEW",
    "REPORTS_VIEW",
]

MODULE_ORDER = [
    "Dashboard",
    "Inventory",
    "Orders",
    "Allocation",
    "Processing",
    "Reports",
    "Administration",
]


def grouped_permissions():
    groups: dict[str, list[tuple[str, str]]] = {m: [] for m in MODULE_ORDER}
    for code, desc, module in PERMISSIONS:
        groups.setdefault(module, []).append((code, desc))
    return groups


NAV_MODULES = [
    ("DASHBOARD_VIEW", "Dashboard", "dashboard.index"),
    ("INVENTORY_VIEW", "Inventory", "inventory.overview"),
    ("ORDERS_VIEW", "Orders", "orders.index"),
    ("ALLOCATION_VIEW", "Allocation", "allocation.index"),
    ("PROCESSING_VIEW", "Order Processing", "processing.index"),
    ("REPORTS_VIEW", "Order Reports", "reports.index"),
    ("REPORTS_VIEW", "KPI Orders", "kpi.orders"),
    ("USERS_VIEW", "Administration", "admin.users"),
]

__all__ = [
    "Role",
    "PERMISSIONS",
    "ALL_CODES",
    "USER_DEFAULTS",
    "grouped_permissions",
    "NAV_MODULES",
]
