"""V2 production order import: destination grouping, staging, bulk activate.

Raw client Order Number is preserved and is NOT unique per destination.
Physical fulfillment orders are grouped by:

    Client + Division + Warehouse + Client Order Number + Customer + Address

WMS Order ID:

    <ClientCode>-<ClientOrderNumber>-<DestinationSequence:02d>

Destination sequence is assigned by sorting groups for a raw order number by
warehouse_code ASC, then customer and address after casefold + whitespace
collapse. Re-upload of the same destinations produces the same IDs and is
rejected as a duplicate.

CustomerPhone, Carrier, and ShippingService are optional attributes and may
be blank. Two nonblank conflicting values inside the same fulfillment group
are a HEADER error. UPC is the merchandise key and is stored as text.
"""

from __future__ import annotations

import io
import json
import math
import re
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pandas as pd
from flask import current_app
from sqlalchemy import insert, text
from sqlalchemy.exc import SQLAlchemyError

from ..auth import current_actor, record_audit
from ..constants import ImportBatchStatus, ImportType, OrderStatus
from ..extensions import db
from ..models import (
    Client,
    Division,
    DivisionWarehouse,
    ImportBatch,
    Order,
    OrderImportRow,
    OrderLine,
    Warehouse,
)
from .inventory_import import normalize_upc
from .inventory_query import bulk_client_upc_descriptions
from .tenant import user_can_access_client

REQUIRED_HEADERS = ("warehouse", "ordernumber", "customer", "customeraddress", "upc", "qty")
OPTIONAL_HEADERS = ("customerphone", "carrier", "shippingservice", "sku", "description")

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

STAGING_CHUNK_SIZE = 2000
ORDER_CHUNK_SIZE = 500
LINE_CHUNK_SIZE = 2000
PREVIEW_ORDER_LIMIT = 100
ERROR_LIMIT = 50
ADVISORY_LOCK_CLASS = 87421002
_SCI = re.compile(r"^[+-]?\d+(\.\d+)?[eE][+-]?\d+$")
_FLOAT_INT = re.compile(r"^[+-]?\d+\.0+$")


class OrderImportError(ValueError):
    pass


def _norm(col: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(col).lower())


def _is_blank(value) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and pd.isna(value):
        return True
    return False


def _plain_text(value) -> str:
    if _is_blank(value):
        return ""
    if isinstance(value, bool):
        return ""
    if isinstance(value, float) and math.isfinite(value) and value == math.trunc(value):
        return str(int(value))
    return str(value).strip()


def normalize_order_number(value) -> str:
    """Keep the raw client order number as text (no forced uniqueness)."""
    return normalize_order_upc(value)


def normalize_order_upc(value) -> str:
    """Preserve UPC as text, including literal tokens such as ``NONE``."""
    if _is_blank(value):
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if text.lower() in {"none", "nan", "nat"}:
        return text
    return normalize_upc(value)


def _parse_qty(raw) -> tuple[int | None, str | None]:
    text_value = _plain_text(raw)
    if not text_value:
        return None, "Qty must be an integer greater than 0."
    try:
        if _SCI.fullmatch(text_value) or _FLOAT_INT.fullmatch(text_value):
            number = float(text_value)
        else:
            number = float(text_value) if "." in text_value else int(text_value)
    except ValueError:
        return None, "Qty must be an integer greater than 0."
    if isinstance(number, float):
        if not math.isfinite(number) or abs(number - round(number)) > 1e-9:
            return None, "Qty must be an integer greater than 0."
        number = int(round(number))
    if int(number) <= 0:
        return None, "Qty must be an integer greater than 0."
    return int(number), None


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


def _source_dir(batch_id: int) -> Path:
    path = Path(current_app.config["DOCUMENTS_DIR"]) / "order_imports" / str(batch_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_preview(payload: dict) -> str:
    compact = dict(payload)
    compact["orders"] = list(payload.get("orders") or [])[:PREVIEW_ORDER_LIMIT]
    compact["blocking"] = list(payload.get("blocking") or [])[:ERROR_LIMIT]
    token = uuid.uuid4().hex
    (_preview_dir() / f"{token}.json").write_text(json.dumps(compact), encoding="utf-8")
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


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (name or "orders.xlsx"))
    return cleaned[:180] or "orders.xlsx"


class _WarehouseCache:
    """Authorized warehouses for one Client/Division. No per-row queries."""

    def __init__(self, client: Client, division: Division):
        self.client = client
        self.division = division
        self.warehouses = Warehouse.query.filter_by(client_id=client.id, active=True).all()
        mapped = {
            row.warehouse_id
            for row in DivisionWarehouse.query.filter_by(division_id=division.id, active=True).all()
        }
        self.mapped_ids = mapped
        self.by_id = {warehouse.id: warehouse for warehouse in self.warehouses}
        self._index: dict[str, Warehouse] = {}
        for warehouse in self.warehouses:
            for key in (
                _norm(warehouse.warehouse_symbol),
                _norm(warehouse.warehouse_code),
                _norm(warehouse.name),
                _norm(str(warehouse.id)),
            ):
                self._index[key] = warehouse

    def resolve(self, raw: str) -> tuple[Warehouse | None, str | None]:
        value = (raw or "").strip()
        if not value:
            return None, "Warehouse is required."
        match = self._index.get(_norm(value))
        if match is None:
            return None, (
                f"Warehouse '{value}' is not an active warehouse of {self.client.client_code}."
            )
        if match.id not in self.mapped_ids:
            return None, (
                f"Warehouse {match.warehouse_code} is not mapped to division {self.division.code}."
            )
        return match, None


def _normalize_dest_text(value: str) -> str:
    """Casefold, strip, and collapse whitespace for destination identity/sort."""
    return " ".join((value or "").casefold().split())


def _existing_order_keys(client_id: int) -> tuple[set[str], set[tuple]]:
    rows = (
        db.session.query(
            Order.wms_order_id,
            Order.client_order_number,
            Order.warehouse_id,
            Order.customer,
            Order.customer_address,
        )
        .filter(Order.client_id == client_id)
        .all()
    )
    ids = {row[0] for row in rows if row[0]}
    dests = {
        (row[1], row[2], _normalize_dest_text(row[3] or ""), _normalize_dest_text(row[4] or ""))
        for row in rows
    }
    return ids, dests


def _group_fulfillment_orders(rows: list[dict], client_code: str, warehouse_by_id: dict) -> list[dict]:
    """Group source rows into WMS fulfillment orders and assign sequences.

    Destination sequence is deterministic: for each Client + raw Order Number,
    sort groups by warehouse_code ASC, normalized customer ASC, normalized
    address ASC, then number 01, 02, 03….
    """
    groups: dict[tuple, dict] = {}
    for row in rows:
        customer = row["customer"] or ""
        address = row["customer_address"] or ""
        key = (
            int(row["warehouse_id"]),
            row["raw_order_number"],
            _normalize_dest_text(customer),
            _normalize_dest_text(address),
        )
        if key not in groups:
            warehouse = warehouse_by_id[key[0]]
            groups[key] = {
                "warehouse_id": key[0],
                "warehouse_code": warehouse.warehouse_code,
                "client_order_number": row["raw_order_number"],
                "customer": customer,
                "customer_address": address,
                "customer_phone": row.get("customer_phone") or "",
                "carrier": row.get("carrier") or "",
                "shipping_service": row.get("shipping_service") or "",
                "attribute_conflicts": [],
                "lines": {},
            }
        else:
            header = groups[key]
            for field in ("customer_phone", "carrier", "shipping_service"):
                incoming = row.get(field) or ""
                current = header[field]
                if current and incoming and _normalize_dest_text(current) != _normalize_dest_text(incoming):
                    label = field.replace("_", " ")
                    message = (
                        f"Order {header['client_order_number']} destination "
                        f"{header['customer']} has conflicting {label}."
                    )
                    if message not in header["attribute_conflicts"]:
                        header["attribute_conflicts"].append(message)
                elif not current and incoming:
                    header[field] = incoming
        upc = row["upc"]
        line = groups[key]["lines"].setdefault(upc, {"qty": 0, "sku": "", "description": ""})
        line["qty"] += int(row["qty"] or 0)
        if row.get("sku"):
            line["sku"] = row["sku"]
        if row.get("description"):
            line["description"] = row["description"]

    by_raw: dict[str, list[dict]] = defaultdict(list)
    for group in groups.values():
        by_raw[group["client_order_number"]].append(group)

    assigned: list[dict] = []
    for raw_number in sorted(by_raw.keys(), key=lambda value: (str(value),)):
        dests = sorted(
            by_raw[raw_number],
            key=lambda group: (
                group["warehouse_code"],
                _normalize_dest_text(group["customer"]),
                _normalize_dest_text(group["customer_address"]),
            ),
        )
        for sequence, group in enumerate(dests, start=1):
            group["destination_sequence"] = sequence
            group["wms_order_id"] = f"{client_code}-{group['client_order_number']}-{sequence:02d}"
            assigned.append(group)
    return assigned


def _group_level_blocking(groups: list[dict], existing_ids: set, existing_dests: set, client_code: str) -> list[dict]:
    blocking: list[dict] = []
    for group in groups:
        for message in group.get("attribute_conflicts") or []:
            if len(blocking) < ERROR_LIMIT:
                blocking.append({"type": "HEADER", "message": message})
        dest = (
            group["client_order_number"],
            group["warehouse_id"],
            _normalize_dest_text(group["customer"]),
            _normalize_dest_text(group["customer_address"]),
        )
        if group["wms_order_id"] in existing_ids or dest in existing_dests:
            if len(blocking) < ERROR_LIMIT:
                blocking.append(
                    {
                        "type": "DUPLICATE",
                        "message": (
                            f"WMS order {group['wms_order_id']} already exists for "
                            f"{client_code} destination {group['customer']}."
                        ),
                    }
                )
    return blocking


def _compact_order(group: dict) -> dict:
    lines = group["lines"]
    units = sum(int(meta["qty"]) for meta in lines.values())
    return {
        "client_order_number": group["client_order_number"],
        "destination_sequence": group["destination_sequence"],
        "wms_order_id": group["wms_order_id"],
        "warehouse_id": group["warehouse_id"],
        "warehouse_code": group["warehouse_code"],
        "customer": group["customer"],
        "customer_address": group["customer_address"],
        "customer_phone": group.get("customer_phone") or "",
        "carrier": group.get("carrier") or "",
        "shipping_service": group.get("shipping_service") or "",
        "line_count": len(lines),
        "units": units,
    }


def analyze(source, filename: str, client: Client, division: Division, user=None) -> dict:
    """Parse Excel once, persist staged rows, return a compact preview."""
    timings: dict[str, int] = {}
    started = datetime.utcnow()
    raw = source.read() if hasattr(source, "read") else source
    parse_started = datetime.utcnow()
    try:
        frame = pd.read_excel(io.BytesIO(raw), dtype=str, keep_default_na=False)
    except Exception as exc:  # noqa: BLE001
        raise OrderImportError(f"Unable to read Excel file: {exc}") from exc
    timings["excel_parse_ms"] = int((datetime.utcnow() - parse_started).total_seconds() * 1000)

    uid, uname = (None, None)
    if user is not None:
        uid, uname = current_actor()
    batch = ImportBatch(
        client_id=client.id,
        division_id=division.id,
        type=ImportType.ORDERS,
        filename=filename,
        status=ImportBatchStatus.UPLOADED,
        rows_submitted=int(len(frame)),
        created_by_user_id=uid,
        created_by_username=uname,
    )
    db.session.add(batch)
    db.session.flush()
    stored = _source_dir(batch.id) / _safe_filename(filename)
    stored.write_bytes(raw)
    batch.source_storage_key = str(stored)
    batch.status = ImportBatchStatus.VALIDATING
    db.session.flush()

    mapped = _map_columns(frame.columns)
    missing = [col for col in REQUIRED_HEADERS if col not in mapped]
    blocking: list[dict] = []
    warnings: list[dict] = []
    staged: list[dict] = []
    warehouses = _WarehouseCache(client, division)
    existing_ids, existing_dests = _existing_order_keys(client.id)

    validate_started = datetime.utcnow()
    if missing:
        blocking.append(
            {
                "type": "COLUMNS",
                "message": "Missing required columns: " + ", ".join(c.upper() for c in missing),
            }
        )
    else:
        for index, series in frame.iterrows():
            excel_row = int(index) + 2
            warehouse_raw = _plain_text(series[mapped["warehouse"]])
            order_number = normalize_order_number(series[mapped["ordernumber"]])
            customer = _plain_text(series[mapped["customer"]])
            address = _plain_text(series[mapped["customeraddress"]])
            phone = _plain_text(series[mapped["customerphone"]]) if "customerphone" in mapped else ""
            upc = normalize_order_upc(series[mapped["upc"]])
            qty, qty_error = _parse_qty(series[mapped["qty"]])
            carrier = _plain_text(series[mapped["carrier"]]) if "carrier" in mapped else ""
            service = (
                _plain_text(series[mapped["shippingservice"]]) if "shippingservice" in mapped else ""
            )
            sku = _plain_text(series[mapped["sku"]]) if "sku" in mapped else ""
            description = (
                _plain_text(series[mapped["description"]]) if "description" in mapped else ""
            )

            row_errors: list[str] = []
            if not order_number:
                row_errors.append("OrderNumber is required.")
            if not customer:
                row_errors.append("Customer is required.")
            if not address:
                row_errors.append("CustomerAddress is required.")
            if not upc:
                row_errors.append("UPC is required.")
            if qty_error:
                row_errors.append(qty_error)
                qty = 0
            warehouse, warehouse_error = warehouses.resolve(warehouse_raw)
            if warehouse_error:
                row_errors.append(warehouse_error)

            valid = not row_errors
            staged.append(
                {
                    "import_batch_id": batch.id,
                    "source_row_number": excel_row,
                    "warehouse": warehouse_raw or None,
                    "warehouse_id": warehouse.id if warehouse is not None else None,
                    "raw_order_number": order_number or None,
                    "customer": customer or None,
                    "customer_address": address or None,
                    "customer_phone": phone or None,
                    "upc": upc or None,
                    "qty": int(qty or 0),
                    "carrier": carrier or None,
                    "shipping_service": service or None,
                    "sku": sku or None,
                    "description": description or None,
                    "validation_status": "VALID" if valid else "INVALID",
                    "validation_error": " ".join(row_errors) if row_errors else None,
                }
            )
            if row_errors and len(blocking) < ERROR_LIMIT:
                blocking.append(
                    {
                        "type": "ROW",
                        "message": f"Row {excel_row}: " + " ".join(row_errors),
                    }
                )

    timings["validation_ms"] = int((datetime.utcnow() - validate_started).total_seconds() * 1000)

    valid_rows = [row for row in staged if row["validation_status"] == "VALID"]
    unique_upcs = {row["upc"] for row in valid_rows if row["upc"]}
    desc_started = datetime.utcnow()
    descriptions = bulk_client_upc_descriptions(client.id, unique_upcs)
    for row in valid_rows:
        if not row["description"] and row["upc"] in descriptions:
            row["description"] = descriptions[row["upc"]]
    timings["description_lookup_ms"] = int((datetime.utcnow() - desc_started).total_seconds() * 1000)

    stage_started = datetime.utcnow()
    for offset in range(0, len(staged), STAGING_CHUNK_SIZE):
        db.session.execute(insert(OrderImportRow), staged[offset : offset + STAGING_CHUNK_SIZE])
    timings["staging_ms"] = int((datetime.utcnow() - stage_started).total_seconds() * 1000)

    group_started = datetime.utcnow()
    groups = _group_fulfillment_orders(valid_rows, client.client_code, warehouses.by_id) if valid_rows else []
    timings["grouping_ms"] = int((datetime.utcnow() - group_started).total_seconds() * 1000)

    blocking.extend(
        _group_level_blocking(groups, existing_ids, existing_dests, client.client_code)
    )

    invalid_rows = len(staged) - len(valid_rows)
    extra_errors = max(0, invalid_rows - len([error for error in blocking if error["type"] == "ROW"]))
    if extra_errors:
        warnings.append(
            {
                "type": "TRUNCATED_ERRORS",
                "message": f"{extra_errors} additional row errors are stored on the batch.",
            }
        )

    compact_orders = [_compact_order(group) for group in groups]
    units_expected = sum(item["units"] for item in compact_orders)
    order_lines_expected = sum(item["line_count"] for item in compact_orders)
    raw_order_numbers = {group["client_order_number"] for group in groups}
    warehouse_codes = sorted({group["warehouse_code"] for group in groups})
    has_blocking = bool(blocking or missing or invalid_rows)

    batch.rows_submitted = int(len(frame))
    batch.rows_validated = len(valid_rows)
    batch.rows_rejected = invalid_rows if not missing else int(len(frame))
    batch.units_expected = units_expected
    batch.orders_expected = 0 if has_blocking else len(groups)
    batch.order_lines_expected = 0 if has_blocking else order_lines_expected
    batch.unique_upc_count = len(unique_upcs)
    batch.unique_location_count = len(warehouse_codes)
    batch.status = ImportBatchStatus.VALIDATED
    db.session.commit()

    timings["analyze_total_ms"] = int((datetime.utcnow() - started).total_seconds() * 1000)
    return {
        "batch_id": batch.id,
        "filename": filename,
        "client_id": client.id,
        "division_id": division.id,
        "client_code": client.client_code,
        "division_code": division.code,
        "total_rows": int(len(frame)),
        "valid_rows": len(valid_rows),
        "raw_order_count": len(raw_order_numbers),
        "order_count": 0 if has_blocking else len(groups),
        "wms_order_count": 0 if has_blocking else len(groups),
        "order_lines_expected": 0 if has_blocking else order_lines_expected,
        "total_units": units_expected,
        "unique_upcs": len(unique_upcs),
        "warehouses": warehouse_codes,
        "orders": compact_orders[:PREVIEW_ORDER_LIMIT],
        "preview_order_limit": PREVIEW_ORDER_LIMIT,
        "blocking": blocking,
        "warnings": warnings,
        "has_blocking": has_blocking,
        "timings": timings,
        "status": batch.status,
    }


def preview_from_batch(batch: ImportBatch) -> dict:
    warehouses = _WarehouseCache(batch.client, batch.division) if batch.client and batch.division else None
    valid_rows = (
        OrderImportRow.query.filter_by(import_batch_id=batch.id, validation_status="VALID")
        .order_by(OrderImportRow.source_row_number)
        .all()
    )
    groups = []
    if warehouses is not None and valid_rows:
        payload = [_row_as_dict(row) for row in valid_rows]
        groups = _group_fulfillment_orders(payload, batch.client.client_code, warehouses.by_id)
    errors = (
        OrderImportRow.query.filter_by(import_batch_id=batch.id, validation_status="INVALID")
        .order_by(OrderImportRow.source_row_number)
        .limit(ERROR_LIMIT)
        .all()
    )
    blocking = [
        {"type": "ROW", "message": f"Row {row.source_row_number}: {row.validation_error}"}
        for row in errors
        if row.validation_error
    ]
    existing_ids, existing_dests = _existing_order_keys(batch.client_id)
    client_code = batch.client.client_code if batch.client else ""
    blocking.extend(_group_level_blocking(groups, existing_ids, existing_dests, client_code))
    compact = [_compact_order(group) for group in groups]
    has_blocking = bool(blocking) or batch.rows_rejected > 0 or batch.rows_validated == 0
    return {
        "batch_id": batch.id,
        "filename": batch.filename,
        "client_id": batch.client_id,
        "division_id": batch.division_id,
        "client_code": client_code,
        "division_code": batch.division.code if batch.division else "",
        "total_rows": batch.rows_submitted,
        "valid_rows": batch.rows_validated,
        "raw_order_count": len({group["client_order_number"] for group in groups}),
        "order_count": 0 if has_blocking else (batch.orders_expected or len(groups)),
        "wms_order_count": 0 if has_blocking else (batch.orders_expected or len(groups)),
        "order_lines_expected": 0 if has_blocking else batch.order_lines_expected,
        "total_units": batch.units_expected,
        "unique_upcs": batch.unique_upc_count,
        "warehouses": sorted({group["warehouse_code"] for group in groups}),
        "orders": compact[:PREVIEW_ORDER_LIMIT],
        "preview_order_limit": PREVIEW_ORDER_LIMIT,
        "blocking": blocking,
        "warnings": [],
        "has_blocking": has_blocking,
        "status": batch.status,
    }


def batch_progress(batch: ImportBatch) -> dict:
    duration_seconds = None
    start = batch.started_at
    end = batch.completed_at or batch.failed_at
    if start and end:
        duration_seconds = max(0, int((end - start).total_seconds()))
    elif start and batch.status == ImportBatchStatus.PROCESSING:
        duration_seconds = max(0, int((datetime.utcnow() - start).total_seconds()))
    reason = batch.error_message or batch.message
    payload = {
        "batch_id": batch.id,
        "status": batch.status,
        "filename": batch.filename,
        "client_id": batch.client_id,
        "division_id": batch.division_id,
        "client_code": batch.client.client_code if batch.client else "",
        "division_code": batch.division.code if batch.division else "",
        "rows_submitted": batch.rows_submitted,
        "rows_validated": batch.rows_validated,
        "rows_rejected": batch.rows_rejected,
        "rows_imported": batch.rows_imported,
        "orders_expected": batch.orders_expected,
        "orders_created": batch.orders_created,
        "order_lines_expected": batch.order_lines_expected,
        "order_lines_created": batch.order_lines_created,
        "units_expected": batch.units_expected,
        "unique_upcs": batch.unique_upc_count,
        "current_source_row": batch.current_source_row,
        "progress_percent": batch.progress_percent,
        "error_message": reason if batch.status == ImportBatchStatus.FAILED else None,
        "retry_safe": batch.status == ImportBatchStatus.FAILED,
        "cleanup_safe": batch.status == ImportBatchStatus.FAILED,
        "duration_seconds": duration_seconds,
        "started_at": batch.started_at.isoformat() + "Z" if batch.started_at else None,
        "completed_at": batch.completed_at.isoformat() + "Z" if batch.completed_at else None,
    }
    from .import_execution import attach_execution_status

    return attach_execution_status(payload, batch)


def _row_as_dict(row: OrderImportRow) -> dict:
    return {
        "warehouse_id": row.warehouse_id,
        "raw_order_number": row.raw_order_number,
        "customer": row.customer or "",
        "customer_address": row.customer_address or "",
        "customer_phone": row.customer_phone or "",
        "upc": row.upc,
        "qty": int(row.qty or 0),
        "carrier": row.carrier or "",
        "shipping_service": row.shipping_service or "",
        "sku": row.sku or "",
        "description": row.description or "",
    }


def _cas_status(batch_id: int, from_status: str, to_status: str, **fields) -> bool:
    assignments = ["status = :to_status"]
    params = {"batch_id": batch_id, "from_status": from_status, "to_status": to_status}
    for key, value in fields.items():
        assignments.append(f"{key} = :{key}")
        params[key] = value
    result = db.session.execute(
        text(
            f"UPDATE import_batches SET {', '.join(assignments)} "
            "WHERE id = :batch_id AND status = :from_status"
        ),
        params,
    )
    db.session.commit()
    return result.rowcount == 1


def _mark_failed(batch: ImportBatch, message: str) -> None:
    safe = (message or "Import failed.")[:500]
    batch.status = ImportBatchStatus.FAILED
    batch.failed_at = datetime.utcnow()
    batch.error_message = safe
    batch.message = safe
    db.session.commit()


def _try_lock(batch_id: int) -> bool:
    return bool(
        db.session.execute(
            text("SELECT pg_try_advisory_lock(:cls, :bid)"),
            {"cls": ADVISORY_LOCK_CLASS, "bid": batch_id},
        ).scalar()
    )


def _unlock(batch_id: int) -> None:
    db.session.execute(
        text("SELECT pg_advisory_unlock(:cls, :bid)"),
        {"cls": ADVISORY_LOCK_CLASS, "bid": batch_id},
    )
    db.session.commit()


def _header_payload(batch: ImportBatch, group: dict, now: datetime) -> dict:
    return {
        "client_id": batch.client_id,
        "division_id": batch.division_id,
        "warehouse_id": group["warehouse_id"],
        "client_order_number": group["client_order_number"],
        "destination_sequence": group["destination_sequence"],
        "wms_order_id": group["wms_order_id"],
        "customer": group["customer"] or None,
        "customer_address": group["customer_address"] or None,
        "customer_phone": group.get("customer_phone") or None,
        "carrier": group.get("carrier") or None,
        "shipping_service": group.get("shipping_service") or None,
        "status": OrderStatus.UNALLOCATED,
        "import_batch_id": batch.id,
        "created_by": batch.created_by_user_id,
        "created_at": now,
        "updated_at": now,
    }


def _line_payloads(order_id: int, client_id: int, group: dict) -> list[dict]:
    payloads = []
    for upc in sorted(group["lines"].keys()):
        meta = group["lines"][upc]
        payloads.append(
            {
                "order_id": order_id,
                "client_id": client_id,
                "upc": upc,
                "sku": meta.get("sku") or None,
                "description": meta.get("description") or None,
                "qty_ordered": int(meta["qty"]),
                "qty_allocated": 0,
                "qty_packed": 0,
                "qty_shipped": 0,
            }
        )
    return payloads


def process_import_batch(
    batch_id: int,
    *,
    chunk_size: int | None = None,
    max_chunks: int | None = None,
    _fail_after: int | None = None,
) -> ImportBatch:
    """Resumable bulk insert of order headers + lines."""
    header_chunk = int(chunk_size or ORDER_CHUNK_SIZE)
    if header_chunk < 1:
        raise OrderImportError("Chunk size must be at least 1.")
    batch = db.session.get(ImportBatch, batch_id)
    if batch is None:
        raise OrderImportError("Import batch was not found.")
    if batch.type != ImportType.ORDERS:
        raise OrderImportError("Batch is not an order import.")
    if batch.status == ImportBatchStatus.COMPLETED:
        return batch
    if batch.status not in {ImportBatchStatus.PROCESSING, ImportBatchStatus.FAILED}:
        raise OrderImportError("Batch is not ready to process.")
    if not _try_lock(batch_id):
        return batch

    metrics = {"header_insert_ms": 0, "line_insert_ms": 0, "sql_statements": 0, "chunks": 0}
    try:
        if batch.status == ImportBatchStatus.FAILED:
            batch.status = ImportBatchStatus.PROCESSING
            batch.error_message = None
            batch.failed_at = None
            if batch.started_at is None:
                batch.started_at = datetime.utcnow()
            db.session.commit()

        client = db.session.get(Client, batch.client_id)
        division = db.session.get(Division, batch.division_id)
        if client is None or division is None:
            raise OrderImportError("Import context is missing.")
        warehouses = _WarehouseCache(client, division)
        valid_rows = (
            OrderImportRow.query.filter_by(import_batch_id=batch.id, validation_status="VALID")
            .order_by(OrderImportRow.id.asc())
            .all()
        )
        groups = _group_fulfillment_orders(
            [_row_as_dict(row) for row in valid_rows],
            client.client_code,
            warehouses.by_id,
        )
        if any(group.get("attribute_conflicts") for group in groups):
            raise OrderImportError("Import is blocked. Fix validation errors and retry.")
        already_ids = {
            row[0]
            for row in db.session.query(Order.wms_order_id)
            .filter(Order.import_batch_id == batch.id)
            .all()
        }
        pending = [group for group in groups if group["wms_order_id"] not in already_ids]
        already = len(already_ids)
        chunks_done = 0

        for offset in range(0, len(pending), header_chunk):
            chunk = pending[offset : offset + header_chunk]
            if _fail_after is not None and already + len(chunk) > _fail_after:
                raise OrderImportError("injected failure")
            now = datetime.utcnow()
            headers = [_header_payload(batch, group, now) for group in chunk]
            started = datetime.utcnow()
            inserted = db.session.execute(
                insert(Order).values(headers).returning(Order.id, Order.wms_order_id)
            ).all()
            metrics["header_insert_ms"] += int((datetime.utcnow() - started).total_seconds() * 1000)
            metrics["sql_statements"] += 1
            if len(inserted) != len(headers):
                raise OrderImportError("Order insert count did not match the chunk.")
            id_by_wms = {row.wms_order_id: row.id for row in inserted}
            line_payloads: list[dict] = []
            for group in chunk:
                order_id = id_by_wms[group["wms_order_id"]]
                line_payloads.extend(_line_payloads(order_id, batch.client_id, group))
            started = datetime.utcnow()
            for line_offset in range(0, len(line_payloads), LINE_CHUNK_SIZE):
                db.session.execute(
                    insert(OrderLine), line_payloads[line_offset : line_offset + LINE_CHUNK_SIZE]
                )
                metrics["sql_statements"] += 1
            metrics["line_insert_ms"] += int((datetime.utcnow() - started).total_seconds() * 1000)
            already += len(inserted)
            created_lines = (
                db.session.query(OrderLine.id)
                .join(Order, Order.id == OrderLine.order_id)
                .filter(Order.import_batch_id == batch.id)
                .count()
            )
            batch.orders_created = already
            batch.order_lines_created = created_lines
            batch.rows_imported = already
            if batch.orders_expected:
                batch.progress_percent = min(99, int(100 * already / batch.orders_expected))
            from .import_execution import notify_progress

            notify_progress(batch)
            db.session.commit()
            metrics["chunks"] += 1
            chunks_done += 1
            if max_chunks is not None and chunks_done >= max_chunks:
                return db.session.get(ImportBatch, batch_id)

        batch = db.session.get(ImportBatch, batch_id)
        created = (
            db.session.query(Order.id).filter(Order.import_batch_id == batch.id).count()
        )
        created_lines = (
            db.session.query(OrderLine.id)
            .join(Order, Order.id == OrderLine.order_id)
            .filter(Order.import_batch_id == batch.id)
            .count()
        )
        if created != batch.orders_expected or created_lines != batch.order_lines_expected:
            raise OrderImportError(
                f"Import invariant failed: orders={created} lines={created_lines} "
                f"expected_orders={batch.orders_expected} expected_lines={batch.order_lines_expected}."
            )
        batch.orders_created = created
        batch.order_lines_created = created_lines
        batch.rows_imported = created
        batch.progress_percent = 100
        batch.status = ImportBatchStatus.COMPLETED
        batch.completed_at = datetime.utcnow()
        batch.error_message = None
        process_ms = int(metrics["header_insert_ms"] + metrics["line_insert_ms"])
        batch.message = (
            f"{created} orders / {created_lines} lines from {batch.filename} "
            f"(header_ms={metrics['header_insert_ms']} line_ms={metrics['line_insert_ms']} "
            f"process_ms={process_ms} chunks={metrics['chunks']} sql={metrics['sql_statements']})"
        )
        record_audit(
            "ORDER_IMPORT",
            module="Orders",
            entity_type="import_batch",
            entity_id=batch.id,
            client_id=batch.client_id,
            detail=f"{created} orders from {batch.filename}",
        )
        db.session.commit()
        return batch
    except Exception as exc:
        db.session.rollback()
        batch = db.session.get(ImportBatch, batch_id)
        if batch is not None:
            _mark_failed(batch, "Import failed. Orders from this batch are not operational.")
        if isinstance(exc, (OrderImportError, SQLAlchemyError)):
            raise
        raise OrderImportError("Import failed.") from exc
    finally:
        try:
            _unlock(batch_id)
        except Exception:
            db.session.rollback()


def processing_order_batch_ids() -> list[int]:
    rows = (
        ImportBatch.query.filter_by(type=ImportType.ORDERS, status=ImportBatchStatus.PROCESSING)
        .order_by(ImportBatch.started_at.asc().nullsfirst(), ImportBatch.id.asc())
        .with_entities(ImportBatch.id)
        .all()
    )
    return [row[0] for row in rows]


def begin_processing(batch_id: int, *, user, run: str = "async") -> ImportBatch:
    """Mark a validated batch PROCESSING. The worker executes it."""
    batch = db.session.get(ImportBatch, batch_id)
    if batch is None:
        raise OrderImportError("Import batch was not found.")
    resolve_context(user, batch.client_id, batch.division_id)
    if batch.rows_rejected or batch.rows_validated <= 0 or batch.orders_expected <= 0:
        raise OrderImportError("Import is blocked. Fix validation errors and retry.")
    if batch.status == ImportBatchStatus.COMPLETED:
        return batch
    if batch.status == ImportBatchStatus.PROCESSING:
        if run == "sync":
            return process_import_batch(batch.id)
        return batch
    if batch.status == ImportBatchStatus.FAILED:
        raise OrderImportError("Import failed. Use Retry Import.")
    if batch.status != ImportBatchStatus.VALIDATED:
        raise OrderImportError("Import is not ready to confirm.")
    now = datetime.utcnow()
    claimed = _cas_status(
        batch.id,
        ImportBatchStatus.VALIDATED,
        ImportBatchStatus.PROCESSING,
        started_at=now,
        last_progress_at=now,
        progress_percent=0,
        error_message=None,
    )
    batch = db.session.get(ImportBatch, batch_id)
    if not claimed and batch.status not in {
        ImportBatchStatus.PROCESSING,
        ImportBatchStatus.COMPLETED,
    }:
        raise OrderImportError("Import could not be started.")
    if batch.status == ImportBatchStatus.COMPLETED:
        return batch
    if run == "sync":
        return process_import_batch(batch.id)
    return batch


def retry_import(batch_id: int, *, user, run: str = "async") -> ImportBatch:
    batch = db.session.get(ImportBatch, batch_id)
    if batch is None:
        raise OrderImportError("Import batch was not found.")
    resolve_context(user, batch.client_id, batch.division_id)
    if batch.status == ImportBatchStatus.COMPLETED:
        return batch
    if batch.status != ImportBatchStatus.FAILED:
        raise OrderImportError("Retry is only available after a failed import.")
    claimed = _cas_status(
        batch.id,
        ImportBatchStatus.FAILED,
        ImportBatchStatus.PROCESSING,
        error_message=None,
        failed_at=None,
    )
    batch = db.session.get(ImportBatch, batch_id)
    if not claimed and batch.status not in {
        ImportBatchStatus.PROCESSING,
        ImportBatchStatus.COMPLETED,
    }:
        raise OrderImportError("Retry could not be started.")
    if run == "sync":
        return process_import_batch(batch.id)
    return batch


def cleanup_failed_import(batch_id: int, *, user) -> ImportBatch:
    batch = db.session.get(ImportBatch, batch_id)
    if batch is None:
        raise OrderImportError("Import batch was not found.")
    resolve_context(user, batch.client_id, batch.division_id)
    if batch.status != ImportBatchStatus.FAILED:
        raise OrderImportError("Cleanup is only available for a failed import.")
    if not _try_lock(batch.id):
        raise OrderImportError("Import is still busy. Try again in a moment.")
    try:
        db.session.execute(
            text(
                "DELETE FROM order_lines WHERE order_id IN "
                "(SELECT id FROM orders WHERE import_batch_id = :bid)"
            ),
            {"bid": batch.id},
        )
        db.session.execute(
            text("DELETE FROM orders WHERE import_batch_id = :bid"),
            {"bid": batch.id},
        )
        batch.orders_created = 0
        batch.order_lines_created = 0
        batch.rows_imported = 0
        batch.progress_percent = 0
        batch.message = "Failed import cleaned up. Orders from this batch were removed."
        db.session.commit()
        return batch
    finally:
        _unlock(batch.id)


def cancel_validated_batch(batch_id: int, *, user) -> None:
    batch = db.session.get(ImportBatch, batch_id)
    if batch is None:
        return
    resolve_context(user, batch.client_id, batch.division_id)
    if batch.status not in {
        ImportBatchStatus.UPLOADED,
        ImportBatchStatus.VALIDATING,
        ImportBatchStatus.VALIDATED,
    }:
        raise OrderImportError("Only an unconfirmed preview can be cancelled.")
    db.session.execute(
        text("DELETE FROM order_import_rows WHERE import_batch_id = :bid"),
        {"bid": batch.id},
    )
    db.session.delete(batch)
    db.session.commit()


def commit_import(
    preview: dict,
    *,
    user,
    _fail_after: int | None = None,
    chunk_size: int | None = None,
) -> ImportBatch:
    """Synchronous confirm+process used by tests and service callers."""
    if preview.get("has_blocking") or not preview.get("batch_id"):
        raise OrderImportError("Import is blocked. Fix validation errors and retry.")
    if preview.get("order_count", 0) <= 0 and not preview.get("orders"):
        raise OrderImportError("Import is blocked. Fix validation errors and retry.")
    resolve_context(user, preview["client_id"], preview["division_id"])
    batch = db.session.get(ImportBatch, int(preview["batch_id"]))
    if batch is None:
        raise OrderImportError("Import batch was not found.")
    if batch.status == ImportBatchStatus.COMPLETED:
        return batch
    if batch.status == ImportBatchStatus.VALIDATED:
        now = datetime.utcnow()
        _cas_status(
            batch.id,
            ImportBatchStatus.VALIDATED,
            ImportBatchStatus.PROCESSING,
            started_at=now,
            progress_percent=0,
            error_message=None,
        )
    return process_import_batch(batch.id, chunk_size=chunk_size, _fail_after=_fail_after)


def import_orders(source, filename: str) -> ImportBatch:
    raise OrderImportError("Client and Division context is required. Use analyze().")


def template_bytes() -> bytes:
    frame = pd.DataFrame(
        columns=[
            "Warehouse",
            "Order Number",
            "Customer",
            "CustomerPhone",
            "CustomerAddress",
            "UPC",
            "QTY",
            "carrier",
            "Shipping Service",
        ]
    )
    buffer = io.BytesIO()
    frame.to_excel(buffer, index=False)
    return buffer.getvalue()
