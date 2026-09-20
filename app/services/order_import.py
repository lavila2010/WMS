"""V2 order import: Client + Division context, UPC lines, atomic commit."""

from __future__ import annotations

import io
import json
import re
import uuid
from collections import defaultdict
from pathlib import Path

import pandas as pd
from flask import current_app

from ..auth import current_actor, record_audit
from ..constants import ImportType, OrderStatus
from ..extensions import db
from ..models import Client, Division, DivisionWarehouse, ImportBatch, Order, OrderLine, Warehouse
from ..services.inventory_query import unique_client_upc_description
from ..services.tenant import user_can_access_client

REQUIRED = (
    "warehouse",
    "ordernumber",
    "customer",
    "customeraddress",
    "customerphone",
    "upc",
    "qty",
    "carrier",
    "shippingservice",
)

ALIASES = {
    "warehouse": {"warehouse", "warehousesymbol", "warehousecode", "wh", "symbol"},
    "ordernumber": {"ordernumber", "order", "clientordernumber"},
    "customer": {"customer", "customername"},
    "customeraddress": {"customeraddress", "address"},
    "customerphone": {"customerphone", "phone"},
    "upc": {"upc"},
    "qty": {"qty", "quantity", "qtyordered"},
    "carrier": {"carrier"},
    "shippingservice": {"shippingservice", "service"},
    "sku": {"sku"},
    "description": {"description", "desc"},
}

HEADER_FIELDS = (
    "warehouse_id",
    "customer",
    "customer_address",
    "customer_phone",
    "carrier",
    "shipping_service",
)


class OrderImportError(ValueError):
    pass


def _norm(col: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(col).lower())


def _cell(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def _map_columns(columns) -> dict[str, str]:
    mapped = {}
    for raw in columns:
        key = _norm(raw)
        for field, aliases in ALIASES.items():
            if key in aliases:
                mapped[field] = raw
                break
    return mapped


def _preview_dir() -> Path:
    path = Path(current_app.config["DOCUMENTS_DIR"]) / "_order_previews"
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_preview(payload: dict) -> str:
    token = uuid.uuid4().hex
    (_preview_dir() / f"{token}.json").write_text(json.dumps(payload), encoding="utf-8")
    return token


def load_preview(token: str) -> dict | None:
    if not token or not re.fullmatch(r"[0-9a-f]{32}", token):
        return None
    path = _preview_dir() / f"{token}.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def clear_preview(token: str) -> None:
    if not token or not re.fullmatch(r"[0-9a-f]{32}", token):
        return
    path = _preview_dir() / f"{token}.json"
    if path.is_file():
        path.unlink()


def resolve_context(user, client_id, division_id) -> tuple[Client, Division]:
    if not client_id or not division_id:
        raise OrderImportError("Select Client and Division before uploading.")
    if not user_can_access_client(user, int(client_id)):
        raise OrderImportError("Client is not authorized.")
    client = db.session.get(Client, int(client_id))
    division = db.session.get(Division, int(division_id))
    if client is None or not client.active:
        raise OrderImportError("Client is unknown or inactive.")
    if division is None or not division.active:
        raise OrderImportError("Division is unknown or inactive.")
    if division.client_id != client.id:
        raise OrderImportError("Division does not belong to the selected client.")
    return client, division


def _resolve_warehouse(client: Client, division: Division, raw: str) -> Warehouse | None:
    value = (raw or "").strip()
    if not value:
        return None
    needle = _norm(value)
    candidates = Warehouse.query.filter_by(client_id=client.id, active=True).all()
    match = None
    for warehouse in candidates:
        if needle in {
            _norm(warehouse.warehouse_symbol),
            _norm(warehouse.warehouse_code),
            _norm(warehouse.name),
            _norm(str(warehouse.id)),
        }:
            match = warehouse
            break
    if match is None:
        return None
    mapped = DivisionWarehouse.query.filter_by(
        division_id=division.id, warehouse_id=match.id, active=True
    ).first()
    if mapped is None:
        raise OrderImportError(
            f"Warehouse {match.warehouse_code} is not mapped to division {division.code}."
        )
    return match


def analyze(source, filename: str, client: Client, division: Division) -> dict:
    raw = source.read() if hasattr(source, "read") else source
    try:
        frame = pd.read_excel(io.BytesIO(raw))
    except Exception as exc:  # noqa: BLE001
        raise OrderImportError(f"Unable to read Excel file: {exc}") from exc

    mapped = _map_columns(frame.columns)
    blocking: list[dict] = []
    missing = [col for col in REQUIRED if col not in mapped]
    if missing:
        blocking.append(
            {
                "type": "COLUMNS",
                "message": "Missing required columns: " + ", ".join(c.upper() for c in missing),
            }
        )
        return _payload(filename, client, division, frame, blocking, {}, 0)

    groups: dict[str, dict] = {}
    for index, series in frame.iterrows():
        excel_row = int(index) + 2
        order_number = _cell(series[mapped["ordernumber"]])
        upc = _cell(series[mapped["upc"]])
        qty_raw = _cell(series[mapped["qty"]])
        warehouse_raw = _cell(series[mapped["warehouse"]])
        customer = _cell(series[mapped["customer"]])
        address = _cell(series[mapped["customeraddress"]])
        phone = _cell(series[mapped["customerphone"]])
        carrier = _cell(series[mapped["carrier"]])
        service = _cell(series[mapped["shippingservice"]])
        sku = _cell(series[mapped["sku"]]) if "sku" in mapped else ""
        description = _cell(series[mapped["description"]]) if "description" in mapped else ""

        row_errors = []
        if not order_number:
            row_errors.append("OrderNumber is required.")
        if not upc:
            row_errors.append("UPC is required.")
        try:
            qty = int(float(qty_raw)) if qty_raw else 0
        except ValueError:
            qty = 0
            row_errors.append("Qty must be an integer greater than 0.")
        else:
            if qty <= 0:
                row_errors.append("Qty must be an integer greater than 0.")
        warehouse = None
        try:
            warehouse = _resolve_warehouse(client, division, warehouse_raw)
        except OrderImportError as exc:
            row_errors.append(str(exc))
        if warehouse is None and not any("mapped" in e for e in row_errors):
            row_errors.append(f"Warehouse '{warehouse_raw}' is not an active warehouse of {client.client_code}.")

        if row_errors:
            blocking.append({"type": "ROW", "message": f"Row {excel_row}: " + " ".join(row_errors)})
            continue

        header = {
            "warehouse_id": warehouse.id,
            "warehouse_code": warehouse.warehouse_code,
            "customer": customer,
            "customer_address": address,
            "customer_phone": phone,
            "carrier": carrier,
            "shipping_service": service,
        }
        if order_number not in groups:
            groups[order_number] = {"header": header, "lines": defaultdict(lambda: {"qty": 0, "sku": "", "description": ""})}
        else:
            existing = groups[order_number]["header"]
            for field in HEADER_FIELDS:
                if existing[field] != header[field]:
                    blocking.append(
                        {
                            "type": "HEADER",
                            "message": (
                                f"Order {order_number} has inconsistent {field.replace('_', ' ')} "
                                f"across rows."
                            ),
                        }
                    )
                    break
        groups[order_number]["lines"][upc]["qty"] += qty
        if sku:
            groups[order_number]["lines"][upc]["sku"] = sku
        if description:
            groups[order_number]["lines"][upc]["description"] = description

    orders = []
    total_units = 0
    for order_number, group in groups.items():
        existing = Order.query.filter_by(
            client_id=client.id, client_order_number=order_number
        ).first()
        if existing:
            blocking.append(
                {
                    "type": "DUPLICATE",
                    "message": f"Order {order_number} already exists for {client.client_code}.",
                }
            )
            continue
        lines = []
        for upc, meta in group["lines"].items():
            description = meta["description"] or unique_client_upc_description(client.id, upc)
            lines.append(
                {
                    "upc": upc,
                    "sku": meta["sku"] or None,
                    "description": description or None,
                    "qty_ordered": meta["qty"],
                }
            )
        units = sum(line["qty_ordered"] for line in lines)
        total_units += units
        wms_order_id = f"{client.client_code}-{order_number}"
        orders.append(
            {
                "client_order_number": order_number,
                "wms_order_id": wms_order_id,
                **group["header"],
                "lines": lines,
                "units": units,
            }
        )

    return _payload(filename, client, division, frame, blocking, orders, total_units)


def _payload(filename, client, division, frame, blocking, orders, total_units) -> dict:
    if isinstance(orders, dict):
        orders = list(orders)
    return {
        "filename": filename,
        "client_id": client.id,
        "division_id": division.id,
        "client_code": client.client_code,
        "division_code": division.code,
        "total_rows": int(len(frame)),
        "orders": orders,
        "order_count": len(orders),
        "total_units": total_units,
        "blocking": blocking,
        "has_blocking": len(blocking) > 0,
    }


def commit_import(preview: dict, *, user, _fail_after: int | None = None) -> ImportBatch:
    if preview.get("has_blocking") or not preview.get("orders"):
        raise OrderImportError("Import is blocked. Fix validation errors and retry.")
    client, division = resolve_context(user, preview["client_id"], preview["division_id"])
    uid, uname = current_actor()
    batch = ImportBatch(
        client_id=client.id,
        division_id=division.id,
        type=ImportType.ORDERS,
        filename=preview["filename"],
        status="COMPLETED",
        rows_submitted=preview["total_rows"],
        rows_imported=0,
        rows_rejected=0,
        created_by_user_id=uid,
        created_by_username=uname,
    )
    db.session.add(batch)
    db.session.flush()
    created_orders = 0
    try:
        for group in preview["orders"]:
            if Order.query.filter_by(
                client_id=client.id, client_order_number=group["client_order_number"]
            ).first():
                raise OrderImportError(
                    f"Order {group['client_order_number']} already exists for {client.client_code}."
                )
            if Order.query.filter_by(wms_order_id=group["wms_order_id"]).first():
                raise OrderImportError(f"WMS order id {group['wms_order_id']} already exists.")
            order = Order(
                client_id=client.id,
                division_id=division.id,
                warehouse_id=group["warehouse_id"],
                client_order_number=group["client_order_number"],
                wms_order_id=group["wms_order_id"],
                customer=group["customer"] or None,
                customer_address=group["customer_address"] or None,
                customer_phone=group["customer_phone"] or None,
                carrier=group["carrier"] or None,
                shipping_service=group["shipping_service"] or None,
                status=OrderStatus.UNALLOCATED,
                import_batch_id=batch.id,
                created_by=uid,
            )
            db.session.add(order)
            db.session.flush()
            for line in group["lines"]:
                description = line.get("description") or unique_client_upc_description(
                    client.id, line["upc"]
                )
                db.session.add(
                    OrderLine(
                        order_id=order.id,
                        client_id=client.id,
                        upc=line["upc"],
                        sku=line.get("sku"),
                        description=description,
                        qty_ordered=line["qty_ordered"],
                        qty_allocated=0,
                        qty_packed=0,
                        qty_shipped=0,
                    )
                )
            created_orders += 1
            if _fail_after is not None and created_orders >= _fail_after:
                raise OrderImportError("injected failure")
        batch.rows_imported = created_orders
        record_audit(
            "ORDER_IMPORT",
            module="Orders",
            entity_type="import_batch",
            entity_id=batch.id,
            client_id=client.id,
            detail=f"{created_orders} orders from {preview['filename']}",
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return batch


def template_bytes() -> bytes:
    frame = pd.DataFrame(
        columns=[
            "Warehouse",
            "OrderNumber",
            "Customer",
            "CustomerAddress",
            "CustomerPhone",
            "UPC",
            "Qty",
            "Carrier",
            "ShippingService",
        ]
    )
    buffer = io.BytesIO()
    frame.to_excel(buffer, index=False)
    return buffer.getvalue()
