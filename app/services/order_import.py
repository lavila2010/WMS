"""Two-step order import: VALIDATE -> PREVIEW -> CONFIRM.

Preview performs **no** database writes. Only an explicit confirm inserts
orders atomically. Existing orders are skipped with a warning.

Order identity is Client + Warehouse + Order Type + Order Number.
"""

from __future__ import annotations

from ..constants import ImportType, OrderStatus
from ..extensions import db
from ..models import Client, ImportBatch, Order, OrderLine, OrderType, Warehouse
from ..services.imports import ImportError_, _norm, _val
from ..services.scope import resolve_scope

REQUIRED_COLUMNS = [
    "client",
    "warehouse",
    "ordertype",
    "ordernumber",
    "sku",
    "qtyordered",
]


class OrderPreview:
    def __init__(self, filename: str):
        self.filename = filename
        self.total_rows = 0
        self.clients: set[str] = set()
        self.warehouses: set[str] = set()
        self.order_types: set[str] = set()
        self.order_keys: set[tuple] = set()
        self.total_units = 0
        self.valid_orders = 0
        self.duplicate_orders = 0
        self.warnings: list[dict] = []
        self.blocking: list[dict] = []
        self.client_breakdown: dict[str, int] = {}
        self.warehouse_breakdown: dict[str, int] = {}
        self.groups: list[dict] = []

    @property
    def has_blocking(self) -> bool:
        return len(self.blocking) > 0

    def as_dict(self) -> dict:
        return {
            "filename": self.filename,
            "total_rows": self.total_rows,
            "orders": len(self.order_keys),
            "total_units": self.total_units,
            "clients": sorted(self.clients),
            "warehouses": sorted(self.warehouses),
            "order_types": sorted(self.order_types),
            "valid_orders": self.valid_orders,
            "warnings": self.warnings,
            "blocking": self.blocking,
            "duplicate_orders": self.duplicate_orders,
            "client_breakdown": self.client_breakdown,
            "warehouse_breakdown": self.warehouse_breakdown,
            "has_blocking": self.has_blocking,
        }


def _read(source):
    import pandas as pd

    df = pd.read_excel(source, engine="openpyxl", dtype=str)
    df.columns = [_norm(c) for c in df.columns]
    return df.fillna("")


def analyze(source, filename: str) -> OrderPreview:
    """Parse and validate an orders workbook without writing to the DB."""
    preview = OrderPreview(filename)
    try:
        df = _read(source)
    except Exception as exc:
        preview.blocking.append({"row": None, "message": f"Invalid workbook: {exc}"})
        return preview

    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        preview.blocking.append({
            "row": None,
            "message": f"Orders workbook is missing required column(s): {', '.join(missing)}",
        })
        return preview

    groups: dict[tuple, dict] = {}
    preview.total_rows = len(df.index)

    for idx, row in df.iterrows():
        excel_row = int(idx) + 2
        client_code = _val(row, "client")
        warehouse_code = _val(row, "warehouse")
        order_type_code = _val(row, "ordertype")
        order_number = _val(row, "ordernumber")
        sku = _val(row, "sku")
        qty_raw = _val(row, "qtyordered")

        blanks = []
        if not client_code:
            blanks.append("Client")
        if not warehouse_code:
            blanks.append("Warehouse")
        if not order_type_code:
            blanks.append("OrderType")
        if not order_number:
            blanks.append("OrderNumber")
        if not sku:
            blanks.append("SKU")
        if not qty_raw:
            blanks.append("QtyOrdered")
        if blanks:
            preview.blocking.append({
                "row": excel_row,
                "message": f"Row {excel_row}: blank {', '.join(blanks)}.",
            })
            continue

        try:
            quantity = int(float(qty_raw))
        except ValueError:
            preview.blocking.append({
                "row": excel_row,
                "message": f"Row {excel_row}: QtyOrdered '{qty_raw}' is not numeric.",
            })
            continue
        if quantity <= 0:
            preview.blocking.append({
                "row": excel_row,
                "message": f"Row {excel_row}: QtyOrdered must be a positive number.",
            })
            continue

        key = (client_code, warehouse_code, order_type_code, order_number)
        preview.order_keys.add(key)
        preview.clients.add(client_code)
        preview.warehouses.add(warehouse_code)
        preview.order_types.add(order_type_code)
        preview.total_units += quantity
        preview.client_breakdown[client_code] = preview.client_breakdown.get(client_code, 0) + quantity
        wh_key = f"{client_code} / {warehouse_code}"
        preview.warehouse_breakdown[wh_key] = preview.warehouse_breakdown.get(wh_key, 0) + quantity

        group = groups.setdefault(
            key,
            {
                "client": client_code,
                "warehouse": warehouse_code,
                "order_type": order_type_code,
                "order_number": order_number,
                "customer": "",
                "carrier": "",
                "shipping_service": "",
                "lines": [],
                "skip": False,
            },
        )
        customer = _val(row, "customer")
        carrier = _val(row, "carrier")
        shipping = _val(row, "shippingservice")
        for field, label, new in (
            ("customer", "Customer", customer),
            ("carrier", "Carrier", carrier),
            ("shipping_service", "Shipping Service", shipping),
        ):
            if not new:
                continue
            cur = group.get(field) or ""
            if cur and cur != new:
                preview.blocking.append({
                    "row": excel_row,
                    "message": (
                        f"Conflicting {label} for order {order_number}: "
                        f"'{cur}' vs '{new}'. All lines of an order must agree."
                    ),
                })
            else:
                group[field] = new
        group["lines"].append({
            "sku": sku,
            "description": _val(row, "description") or None,
            "quantity": quantity,
        })

    if preview.has_blocking:
        preview.groups = list(groups.values())
        return preview

    for group in groups.values():
        existing = _existing_order(
            group["client"], group["warehouse"], group["order_type"], group["order_number"]
        )
        if existing is not None:
            group["skip"] = True
            preview.duplicate_orders += 1
            preview.warnings.append({
                "message": (
                    f"Order {group['order_number']} already exists for "
                    f"{group['client']}/{group['warehouse']}/{group['order_type']}; it will be skipped."
                )
            })
        else:
            preview.valid_orders += 1

    preview.groups = list(groups.values())
    return preview


def _existing_order(client_code, warehouse_code, order_type_code, order_number):
    """Look up an existing order by codes without creating master rows."""
    client = Client.query.filter_by(code=client_code).first()
    if client is None:
        return None
    warehouse = Warehouse.query.filter_by(client_id=client.id, code=warehouse_code).first()
    order_type = OrderType.query.filter_by(client_id=client.id, code=order_type_code).first()
    if warehouse is None or order_type is None:
        return None
    return Order.query.filter_by(
        client_id=client.id,
        warehouse_id=warehouse.id,
        order_type_id=order_type.id,
        order_number=order_number,
    ).first()


def confirm_import(preview: OrderPreview) -> ImportBatch:
    """Persist a previously analyzed preview atomically."""
    if preview.has_blocking:
        msg = preview.blocking[0]["message"] if preview.blocking else "Orders workbook has blocking errors."
        raise ImportError_(msg)

    batch = ImportBatch(
        type=ImportType.ORDERS,
        filename=preview.filename,
        row_count=0,
        rows_submitted=preview.total_rows,
        clients=", ".join(sorted(preview.clients)),
        warehouses=", ".join(sorted(preview.warehouses)),
        warnings_count=len(preview.warnings),
        status="COMPLETED",
    )
    db.session.add(batch)
    db.session.flush()

    lines_created = 0
    orders_created = 0
    orders_skipped = 0
    for group in preview.groups:
        if group.get("skip"):
            orders_skipped += 1
            continue
        client, warehouse, order_type = resolve_scope(
            group["client"], group["warehouse"], group["order_type"]
        )
        from .scope import ensure_default_division

        division = ensure_default_division(client)
        order = Order(
            order_number=group["order_number"],
            customer=group.get("customer") or None,
            carrier=group.get("carrier") or None,
            shipping_service=group.get("shipping_service") or None,
            status=OrderStatus.NEW,
            client_id=client.id,
            warehouse_id=warehouse.id,
            order_type_id=order_type.id,
            division_id=division.id,
            import_batch_id=batch.id,
        )
        db.session.add(order)
        db.session.flush()
        orders_created += 1
        for line in group["lines"]:
            db.session.add(
                OrderLine(
                    order_id=order.id,
                    sku=line["sku"],
                    description=line["description"],
                    quantity=line["quantity"],
                )
            )
            lines_created += 1

    batch.row_count = lines_created
    batch.rows_imported = orders_created
    batch.rows_updated = 0
    batch.rows_rejected = orders_skipped
    batch.message = (
        f"Imported {orders_created} order(s), {lines_created} line(s); "
        f"skipped {orders_skipped} existing order(s)."
    )
    db.session.commit()
    return batch


def import_orders(source, filename: str) -> ImportBatch:
    """Programmatic one-shot import used by tests and legacy callers."""
    preview = analyze(source, filename)
    return confirm_import(preview)
