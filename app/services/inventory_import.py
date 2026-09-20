"""V2 production inventory import: stage → compact preview → bulk activate.

Unit-level inventory remains the system of record. Quantity N still creates N
``inventory_units`` rows and N IMPORT ledger rows. The engine stages Excel
once, never stores the full workbook in preview JSON, and inserts units plus
ledger records in bounded PostgreSQL chunks.
"""

from __future__ import annotations

import io
import json
import math
import re
import threading
import uuid
from datetime import date, datetime
from pathlib import Path

import pandas as pd
from flask import current_app
from sqlalchemy import func, insert, text
from sqlalchemy.exc import SQLAlchemyError

from ..auth import current_actor, record_audit
from ..constants import ImportBatchStatus, ImportType, LedgerType, UnitStatus
from ..extensions import db
from ..models import (
    Client,
    ImportBatch,
    InventoryImportRow,
    InventoryTransaction,
    InventoryUnit,
    Warehouse,
)
from .inventory_ledger import LedgerError
from .tenant import user_can_access_client

REQUIRED_HEADERS = ("upc", "sku", "description", "style", "color", "size", "quantity", "location")
REQUIRED_VALUES = ("upc", "description", "style", "color", "size", "quantity", "location")
OPTIONAL_CONTEXT = ("client", "warehouse")
DESCRIPTION_MAX = 255
FIELD_MAX = {
    "upc": 64,
    "sku": 64,
    "description": DESCRIPTION_MAX,
    "style": 64,
    "color": 64,
    "size": 32,
    "location": 64,
}

ALIASES = {
    "upc": {"upc"},
    "sku": {"sku"},
    "description": {"description", "desc", "productdescription"},
    "style": {"style"},
    "color": {"color", "colour"},
    "size": {"size"},
    "quantity": {"quantity", "qty", "count"},
    "location": {"location", "loc", "bin"},
    "client": {"client", "clientcode", "clientname", "client_code"},
    "warehouse": {"warehouse", "warehousesymbol", "warehousecode", "wh", "symbol"},
}

UNIT_CHUNK_SIZE = 2000
LEDGER_CHUNK_SIZE = 2000
STAGING_CHUNK_SIZE = 2000
PREVIEW_ROW_LIMIT = 100
ERROR_LIMIT = 50
ADVANCE_MAX_CHUNKS = 3
ADVISORY_LOCK_CLASS = 87421001

_SCI = re.compile(r"^[+-]?\d+(\.\d+)?[eE][+-]?\d+$")
_FLOAT_INT = re.compile(r"^[+-]?\d+\.0+$")
_JOBS: dict[int, threading.Thread] = {}
_JOBS_GUARD = threading.Lock()


class ImportErrorClosed(ValueError):
    pass


def _norm(col: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(col).lower())


def _is_blank(value) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and pd.isna(value):
        return True
    return False


def normalize_upc(value) -> str:
    """Persist UPC as text. Never keep float tails such as ``123.0``."""
    if _is_blank(value):
        return ""
    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isfinite(value) and value == math.trunc(value):
            return str(int(value))
        as_int = int(round(value))
        if math.isfinite(value) and abs(value - as_int) < 1e-6:
            return str(as_int)
        return ""
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "nat"}:
        return ""
    if _SCI.fullmatch(text):
        try:
            num = float(text)
        except ValueError:
            return text
        if math.isfinite(num) and abs(num - round(num)) < 1e-6:
            return str(int(round(num)))
        return text
    if _FLOAT_INT.fullmatch(text):
        return text.split(".")[0].lstrip("+")
    return text


def normalize_location(value) -> str | None:
    """Keep location as a textual identifier. Dates are rejected (None)."""
    if _is_blank(value):
        return ""
    if isinstance(value, datetime) or (isinstance(value, date) and not isinstance(value, datetime)):
        return None
    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isfinite(value) and value == math.trunc(value):
            return str(int(value))
        return str(value).strip()
    text = str(value).strip()
    if text.lower() in {"nan", "none", "nat"}:
        return ""
    return text


def _plain_text(value) -> str:
    if _is_blank(value):
        return ""
    if isinstance(value, float) and value == math.trunc(value):
        return str(int(value))
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
    path = Path(current_app.config["DOCUMENTS_DIR"]) / "_inventory_previews"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _source_dir(batch_id: int) -> Path:
    path = Path(current_app.config["DOCUMENTS_DIR"]) / "inventory_imports" / str(batch_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_preview(payload: dict) -> str:
    compact = dict(payload)
    compact.pop("rows", None)
    compact["rows"] = list(payload.get("rows") or [])[:PREVIEW_ROW_LIMIT]
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


def resolve_context(user, client_id, warehouse_id) -> tuple[Client, Warehouse]:
    if not client_id or not warehouse_id:
        raise ImportErrorClosed("Select Client and Warehouse before uploading.")
    if not user_can_access_client(user, int(client_id)):
        raise ImportErrorClosed("Client is not authorized.")
    client = db.session.get(Client, int(client_id))
    warehouse = db.session.get(Warehouse, int(warehouse_id))
    if client is None or not client.active:
        raise ImportErrorClosed("Client is unknown or inactive.")
    if warehouse is None or not warehouse.active:
        raise ImportErrorClosed("Warehouse is unknown or inactive.")
    if warehouse.client_id != client.id:
        raise ImportErrorClosed("Warehouse does not belong to the selected client.")
    return client, warehouse


def _context_matches(value: str, *candidates: str) -> bool:
    needle = _norm(value)
    if not needle:
        return True
    return needle in {_norm(c) for c in candidates if c}


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (name or "inventory.xlsx"))
    return cleaned[:180] or "inventory.xlsx"


def _parse_quantity(raw) -> tuple[int | None, str | None]:
    text = _plain_text(raw)
    if not text:
        return None, "Quantity must be an integer greater than 0."
    try:
        if _SCI.fullmatch(text) or _FLOAT_INT.fullmatch(text):
            number = float(text)
        else:
            number = float(text) if "." in text else int(text)
    except ValueError:
        return None, "Quantity must be an integer greater than 0."
    if isinstance(number, float):
        if not math.isfinite(number) or abs(number - round(number)) > 1e-9:
            return None, "Quantity must be an integer greater than 0."
        number = int(round(number))
    if int(number) <= 0:
        return None, "Quantity must be an integer greater than 0."
    return int(number), None


def _length_error(field: str, value: str) -> str | None:
    limit = FIELD_MAX[field]
    if len(value) > limit:
        return f"{field.capitalize()} must be {limit} characters or fewer."
    return None


def analyze(source, filename: str, client: Client, warehouse: Warehouse, user=None) -> dict:
    """Parse Excel once, persist staged rows, return a compact preview."""
    timings = {}
    started = datetime.utcnow()
    raw = source.read() if hasattr(source, "read") else source
    parse_started = datetime.utcnow()
    try:
        frame = pd.read_excel(io.BytesIO(raw), dtype=str, keep_default_na=False)
    except Exception as exc:  # noqa: BLE001
        raise ImportErrorClosed(f"Unable to read Excel file: {exc}") from exc
    timings["excel_parse_ms"] = int((datetime.utcnow() - parse_started).total_seconds() * 1000)

    uid, uname = (None, None)
    if user is not None:
        uid, uname = current_actor()
    batch = ImportBatch(
        client_id=client.id,
        warehouse_id=warehouse.id,
        type=ImportType.INVENTORY,
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
    preview_rows: list[dict] = []

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
            upc = normalize_upc(series[mapped["upc"]])
            sku = _plain_text(series[mapped["sku"]]) if "sku" in mapped else ""
            description = _plain_text(series[mapped["description"]])
            style = _plain_text(series[mapped["style"]])
            color = _plain_text(series[mapped["color"]])
            size = _plain_text(series[mapped["size"]])
            quantity, quantity_error = _parse_quantity(series[mapped["quantity"]])
            location = normalize_location(series[mapped["location"]])
            file_client = _plain_text(series[mapped["client"]]) if "client" in mapped else ""
            file_warehouse = _plain_text(series[mapped["warehouse"]]) if "warehouse" in mapped else ""

            row_errors: list[str] = []
            if not upc:
                row_errors.append("UPC is required.")
            else:
                err = _length_error("upc", upc)
                if err:
                    row_errors.append(err)
            if quantity_error:
                row_errors.append(quantity_error)
                quantity = 0
            if not description:
                row_errors.append("Description is required.")
            else:
                err = _length_error("description", description)
                if err:
                    row_errors.append(err)
            if not style:
                row_errors.append("Style is required.")
            else:
                err = _length_error("style", style)
                if err:
                    row_errors.append(err)
            if not color:
                row_errors.append("Color is required.")
            else:
                err = _length_error("color", color)
                if err:
                    row_errors.append(err)
            if not size:
                row_errors.append("Size is required.")
            else:
                err = _length_error("size", size)
                if err:
                    row_errors.append(err)
            if location is None:
                row_errors.append("Location must be a text identifier, not a date.")
                location = ""
            elif not location:
                row_errors.append("Location is required.")
            else:
                err = _length_error("location", location)
                if err:
                    row_errors.append(err)
            if sku:
                err = _length_error("sku", sku)
                if err:
                    row_errors.append(err)
            if file_client and not _context_matches(
                file_client, client.client_code, client.name, str(client.id), client.initials
            ):
                row_errors.append(
                    f"File Client '{file_client}' does not match selected {client.client_code}."
                )
            if file_warehouse and not _context_matches(
                file_warehouse,
                warehouse.warehouse_symbol,
                warehouse.warehouse_code,
                warehouse.name,
                str(warehouse.id),
            ):
                row_errors.append(
                    f"File Warehouse '{file_warehouse}' does not match selected {warehouse.warehouse_code}."
                )

            valid = not row_errors
            staged.append(
                {
                    "import_batch_id": batch.id,
                    "source_row_number": excel_row,
                    "upc": upc or None,
                    "sku": sku or None,
                    "description": description or None,
                    "style": style or None,
                    "color": color or None,
                    "size": size or None,
                    "quantity": int(quantity or 0),
                    "location": location or None,
                    "validation_status": "VALID" if valid else "INVALID",
                    "validation_error": " ".join(row_errors) if row_errors else None,
                }
            )
            if row_errors:
                if len(blocking) < ERROR_LIMIT:
                    blocking.append(
                        {
                            "type": "ROW",
                            "message": f"Row {excel_row}: " + " ".join(row_errors),
                        }
                    )
            elif len(preview_rows) < PREVIEW_ROW_LIMIT:
                preview_rows.append(
                    {
                        "excel_row": excel_row,
                        "upc": upc,
                        "sku": sku,
                        "description": description,
                        "style": style,
                        "color": color,
                        "size": size,
                        "quantity": int(quantity or 0),
                        "location": location,
                    }
                )

    timings["validation_ms"] = int((datetime.utcnow() - validate_started).total_seconds() * 1000)

    stage_started = datetime.utcnow()
    for offset in range(0, len(staged), STAGING_CHUNK_SIZE):
        db.session.execute(insert(InventoryImportRow), staged[offset : offset + STAGING_CHUNK_SIZE])
    timings["staging_ms"] = int((datetime.utcnow() - stage_started).total_seconds() * 1000)

    valid_rows = [r for r in staged if r["validation_status"] == "VALID"]
    invalid_rows = len(staged) - len(valid_rows)
    units_expected = sum(r["quantity"] for r in valid_rows)
    unique_upcs = len({r["upc"] for r in valid_rows if r["upc"]})
    unique_skus = len({r["sku"] for r in valid_rows if r["sku"]})
    unique_styles = len({r["style"] for r in valid_rows if r["style"]})
    unique_locations = len({r["location"] for r in valid_rows if r["location"]})

    batch.rows_submitted = int(len(frame))
    batch.rows_validated = len(valid_rows)
    batch.rows_rejected = invalid_rows if not missing else int(len(frame))
    batch.units_expected = units_expected
    batch.unique_upc_count = unique_upcs
    batch.unique_style_count = unique_styles
    batch.unique_location_count = unique_locations
    batch.status = ImportBatchStatus.VALIDATED
    db.session.commit()

    timings["analyze_total_ms"] = int((datetime.utcnow() - started).total_seconds() * 1000)
    extra_errors = max(0, invalid_rows - len([e for e in blocking if e["type"] == "ROW"]))
    if extra_errors:
        warnings.append(
            {
                "type": "TRUNCATED_ERRORS",
                "message": f"{extra_errors} additional row errors are stored on the batch.",
            }
        )
    return {
        "batch_id": batch.id,
        "filename": filename,
        "client_id": client.id,
        "warehouse_id": warehouse.id,
        "client_code": client.client_code,
        "warehouse_code": warehouse.warehouse_code,
        "total_rows": int(len(frame)),
        "valid_rows": len(valid_rows),
        "units": units_expected,
        "unique_upcs": unique_upcs,
        "unique_skus": unique_skus,
        "unique_styles": unique_styles,
        "unique_locations": unique_locations,
        "blocking": blocking,
        "warnings": warnings,
        "has_blocking": bool(blocking or missing or invalid_rows),
        "rows": preview_rows,
        "preview_row_limit": PREVIEW_ROW_LIMIT,
        "timings": timings,
        "status": batch.status,
    }


def preview_from_batch(batch: ImportBatch) -> dict:
    preview_rows = (
        InventoryImportRow.query.filter_by(import_batch_id=batch.id, validation_status="VALID")
        .order_by(InventoryImportRow.source_row_number)
        .limit(PREVIEW_ROW_LIMIT)
        .all()
    )
    errors = (
        InventoryImportRow.query.filter_by(import_batch_id=batch.id, validation_status="INVALID")
        .order_by(InventoryImportRow.source_row_number)
        .limit(ERROR_LIMIT)
        .all()
    )
    blocking = [
        {"type": "ROW", "message": f"Row {row.source_row_number}: {row.validation_error}"}
        for row in errors
        if row.validation_error
    ]
    return {
        "batch_id": batch.id,
        "filename": batch.filename,
        "client_id": batch.client_id,
        "warehouse_id": batch.warehouse_id,
        "client_code": batch.client.client_code if batch.client else "",
        "warehouse_code": batch.warehouse.warehouse_code if batch.warehouse else "",
        "total_rows": batch.rows_submitted,
        "valid_rows": batch.rows_validated,
        "units": batch.units_expected,
        "unique_upcs": batch.unique_upc_count,
        "unique_skus": 0,
        "unique_styles": batch.unique_style_count,
        "unique_locations": batch.unique_location_count,
        "blocking": blocking,
        "warnings": [],
        "has_blocking": batch.rows_rejected > 0 or batch.rows_validated == 0,
        "rows": [
            {
                "excel_row": row.source_row_number,
                "upc": row.upc,
                "sku": row.sku or "",
                "description": row.description,
                "style": row.style,
                "color": row.color,
                "size": row.size,
                "quantity": row.quantity,
                "location": row.location,
            }
            for row in preview_rows
        ],
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
    return {
        "batch_id": batch.id,
        "status": batch.status,
        "filename": batch.filename,
        "client_id": batch.client_id,
        "warehouse_id": batch.warehouse_id,
        "client_code": batch.client.client_code if batch.client else "",
        "warehouse_code": batch.warehouse.warehouse_code if batch.warehouse else "",
        "rows_submitted": batch.rows_submitted,
        "rows_validated": batch.rows_validated,
        "rows_rejected": batch.rows_rejected,
        "rows_imported": batch.rows_imported,
        "units_expected": batch.units_expected,
        "units_created": batch.units_created,
        "transactions_created": batch.transactions_created,
        "unique_upcs": batch.unique_upc_count,
        "unique_styles": batch.unique_style_count,
        "unique_locations": batch.unique_location_count,
        "current_source_row": batch.current_source_row,
        "progress_percent": batch.progress_percent,
        "error_message": reason if batch.status == ImportBatchStatus.FAILED else None,
        "retry_safe": batch.status == ImportBatchStatus.FAILED,
        "cleanup_safe": batch.status == ImportBatchStatus.FAILED,
        "duration_seconds": duration_seconds,
        "started_at": batch.started_at.isoformat() + "Z" if batch.started_at else None,
        "completed_at": batch.completed_at.isoformat() + "Z" if batch.completed_at else None,
    }


def _cas_status(batch_id: int, from_status: str, to_status: str, **fields) -> bool:
    assignments = [f"status = :to_status"]
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


def _unit_payload(batch: ImportBatch, row: InventoryImportRow, now: datetime) -> dict:
    return {
        "client_id": batch.client_id,
        "warehouse_id": batch.warehouse_id,
        "upc": row.upc,
        "sku": row.sku,
        "description": row.description,
        "style": row.style,
        "color": row.color,
        "size": row.size,
        "location": row.location,
        "status": UnitStatus.AVAILABLE,
        "import_batch_id": batch.id,
        "created_at": now,
        "updated_at": now,
    }


def process_import_batch(
    batch_id: int,
    *,
    chunk_size: int | None = None,
    max_chunks: int | None = None,
    _fail_after: int | None = None,
) -> ImportBatch:
    """Resumable bulk insert of units + IMPORT ledger rows."""
    unit_chunk = int(chunk_size or UNIT_CHUNK_SIZE)
    ledger_chunk = int(chunk_size or LEDGER_CHUNK_SIZE)
    if unit_chunk < 1:
        raise ImportErrorClosed("Chunk size must be at least 1.")
    batch = db.session.get(ImportBatch, batch_id)
    if batch is None:
        raise ImportErrorClosed("Import batch was not found.")
    if batch.type != ImportType.INVENTORY:
        raise ImportErrorClosed("Batch is not an inventory import.")
    if batch.status == ImportBatchStatus.COMPLETED:
        return batch
    if batch.status not in {ImportBatchStatus.PROCESSING, ImportBatchStatus.FAILED}:
        raise ImportErrorClosed("Batch is not ready to process.")
    if not _try_lock(batch_id):
        return batch

    metrics = {
        "unit_insert_ms": 0,
        "ledger_insert_ms": 0,
        "sql_statements": 0,
        "peak_chunk_size": 0,
        "chunks": 0,
    }
    try:
        if batch.status == ImportBatchStatus.FAILED:
            batch.status = ImportBatchStatus.PROCESSING
            batch.error_message = None
            batch.failed_at = None
            if batch.started_at is None:
                batch.started_at = datetime.utcnow()
            db.session.commit()

        already = int(batch.units_created or 0)
        remaining_skip = already
        chunks_done = 0
        pending: list[tuple[InventoryImportRow, int]] = []

        def flush_pending() -> None:
            nonlocal already, chunks_done
            if not pending:
                return
            if _fail_after is not None and already + len(pending) > _fail_after:
                raise LedgerError("injected failure")
            now = datetime.utcnow()
            payloads = [_unit_payload(batch, row, now) for row, _ in pending]
            metrics["peak_chunk_size"] = max(metrics["peak_chunk_size"], len(payloads))
            started = datetime.utcnow()
            inserted = db.session.execute(
                insert(InventoryUnit)
                .values(payloads)
                .returning(
                    InventoryUnit.id,
                    InventoryUnit.client_id,
                    InventoryUnit.warehouse_id,
                    InventoryUnit.upc,
                    InventoryUnit.location,
                )
            ).all()
            metrics["unit_insert_ms"] += int((datetime.utcnow() - started).total_seconds() * 1000)
            metrics["sql_statements"] += 1
            if len(inserted) != len(payloads):
                raise LedgerError("Unit insert count did not match the chunk.")
            txn_payloads = [
                {
                    "client_id": unit.client_id,
                    "warehouse_id": unit.warehouse_id,
                    "inventory_unit_id": unit.id,
                    "import_batch_id": batch.id,
                    "upc": unit.upc,
                    "location": unit.location,
                    "transaction_type": LedgerType.IMPORT,
                    "from_status": None,
                    "to_status": UnitStatus.AVAILABLE,
                    "user_id": batch.created_by_user_id,
                    "reference": f"import:{batch.id}",
                    "created_at": now,
                }
                for unit in inserted
            ]
            started = datetime.utcnow()
            for offset in range(0, len(txn_payloads), ledger_chunk):
                db.session.execute(insert(InventoryTransaction), txn_payloads[offset : offset + ledger_chunk])
                metrics["sql_statements"] += 1
            metrics["ledger_insert_ms"] += int((datetime.utcnow() - started).total_seconds() * 1000)
            already += len(inserted)
            batch.units_created = already
            batch.transactions_created = already
            batch.rows_imported = already
            batch.current_source_row = pending[-1][0].source_row_number
            if batch.units_expected:
                batch.progress_percent = min(99, int(100 * already / batch.units_expected))
            db.session.commit()
            metrics["chunks"] += 1
            chunks_done += 1
            pending.clear()

        last_row_id = 0
        while True:
            page = (
                InventoryImportRow.query.filter(
                    InventoryImportRow.import_batch_id == batch.id,
                    InventoryImportRow.validation_status == "VALID",
                    InventoryImportRow.id > last_row_id,
                )
                .order_by(InventoryImportRow.id.asc())
                .limit(500)
                .all()
            )
            if not page:
                break
            for row in page:
                qty = int(row.quantity or 0)
                if remaining_skip >= qty:
                    remaining_skip -= qty
                    continue
                offset = remaining_skip
                remaining_skip = 0
                left = qty - offset
                while left > 0:
                    take = min(left, unit_chunk - len(pending))
                    for _ in range(take):
                        pending.append((row, 1))
                    left -= take
                    if len(pending) >= unit_chunk:
                        flush_pending()
                        if max_chunks is not None and chunks_done >= max_chunks:
                            return db.session.get(ImportBatch, batch_id)
            last_row_id = page[-1].id
        flush_pending()

        batch = db.session.get(ImportBatch, batch_id)
        units = (
            db.session.query(func.count(InventoryUnit.id))
            .filter_by(import_batch_id=batch.id)
            .scalar()
        )
        txns = (
            db.session.query(func.count(InventoryTransaction.id))
            .filter_by(import_batch_id=batch.id, transaction_type=LedgerType.IMPORT)
            .scalar()
        )
        if units != batch.units_expected or txns != units:
            raise LedgerError(
                f"Import invariant failed: units={units} transactions={txns} expected={batch.units_expected}."
            )
        batch.units_created = units
        batch.transactions_created = txns
        batch.rows_imported = units
        batch.progress_percent = 100
        batch.status = ImportBatchStatus.COMPLETED
        batch.completed_at = datetime.utcnow()
        batch.error_message = None
        batch.message = (
            f"{units} units from {batch.filename} "
            f"(unit_ms={metrics['unit_insert_ms']} ledger_ms={metrics['ledger_insert_ms']} "
            f"chunks={metrics['chunks']} peak={metrics['peak_chunk_size']} sql={metrics['sql_statements']})"
        )
        record_audit(
            "INVENTORY_IMPORT",
            module="Inventory",
            entity_type="import_batch",
            entity_id=batch.id,
            client_id=batch.client_id,
            detail=f"{units} units from {batch.filename}",
        )
        db.session.commit()
        return batch
    except Exception as exc:
        db.session.rollback()
        batch = db.session.get(ImportBatch, batch_id)
        if batch is not None:
            _mark_failed(batch, "Import failed. Inventory from this batch is not available.")
        if isinstance(exc, (LedgerError, ImportErrorClosed, SQLAlchemyError)):
            raise
        raise LedgerError("Import failed.") from exc
    finally:
        try:
            _unlock(batch_id)
        except Exception:
            db.session.rollback()


def start_background_import(app, batch_id: int) -> None:
    with _JOBS_GUARD:
        existing = _JOBS.get(batch_id)
        if existing is not None and existing.is_alive():
            return

        def run():
            with app.app_context():
                try:
                    process_import_batch(batch_id)
                except Exception:
                    current_app.logger.exception("inventory import batch %s failed", batch_id)

        thread = threading.Thread(target=run, name=f"inv-import-{batch_id}", daemon=True)
        _JOBS[batch_id] = thread
        thread.start()


def begin_processing(batch_id: int, *, user, run: str = "async") -> ImportBatch:
    batch = db.session.get(ImportBatch, batch_id)
    if batch is None:
        raise ImportErrorClosed("Import batch was not found.")
    resolve_context(user, batch.client_id, batch.warehouse_id)
    if batch.rows_rejected or batch.rows_validated <= 0 or batch.units_expected <= 0:
        raise ImportErrorClosed("Import is blocked. Fix validation errors and retry.")
    if batch.status == ImportBatchStatus.COMPLETED:
        return batch
    if batch.status == ImportBatchStatus.PROCESSING:
        if run == "async":
            start_background_import(current_app._get_current_object(), batch.id)
        return batch
    if batch.status == ImportBatchStatus.FAILED:
        raise ImportErrorClosed("Import failed. Use Retry Import.")
    if batch.status != ImportBatchStatus.VALIDATED:
        raise ImportErrorClosed("Import is not ready to confirm.")
    now = datetime.utcnow()
    claimed = _cas_status(
        batch.id,
        ImportBatchStatus.VALIDATED,
        ImportBatchStatus.PROCESSING,
        started_at=now,
        progress_percent=0,
        error_message=None,
    )
    batch = db.session.get(ImportBatch, batch_id)
    if not claimed and batch.status not in {
        ImportBatchStatus.PROCESSING,
        ImportBatchStatus.COMPLETED,
    }:
        raise ImportErrorClosed("Import could not be started.")
    if batch.status == ImportBatchStatus.COMPLETED:
        return batch
    if run == "sync":
        return process_import_batch(batch.id)
    start_background_import(current_app._get_current_object(), batch.id)
    return batch


def retry_import(batch_id: int, *, user, run: str = "async") -> ImportBatch:
    batch = db.session.get(ImportBatch, batch_id)
    if batch is None:
        raise ImportErrorClosed("Import batch was not found.")
    resolve_context(user, batch.client_id, batch.warehouse_id)
    if batch.status == ImportBatchStatus.COMPLETED:
        return batch
    if batch.status != ImportBatchStatus.FAILED:
        raise ImportErrorClosed("Retry is only available after a failed import.")
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
        raise ImportErrorClosed("Retry could not be started.")
    if run == "sync":
        return process_import_batch(batch.id)
    start_background_import(current_app._get_current_object(), batch.id)
    return batch


def cleanup_failed_import(batch_id: int, *, user) -> ImportBatch:
    batch = db.session.get(ImportBatch, batch_id)
    if batch is None:
        raise ImportErrorClosed("Import batch was not found.")
    resolve_context(user, batch.client_id, batch.warehouse_id)
    if batch.status != ImportBatchStatus.FAILED:
        raise ImportErrorClosed("Cleanup is only available for a failed import.")
    if not _try_lock(batch.id):
        raise ImportErrorClosed("Import is still busy. Try again in a moment.")
    try:
        db.session.execute(
            text("DELETE FROM inventory_transactions WHERE import_batch_id = :bid"),
            {"bid": batch.id},
        )
        db.session.execute(
            text("DELETE FROM inventory_units WHERE import_batch_id = :bid"),
            {"bid": batch.id},
        )
        batch.units_created = 0
        batch.transactions_created = 0
        batch.rows_imported = 0
        batch.current_source_row = 0
        batch.progress_percent = 0
        batch.message = "Failed import cleaned up. Inventory from this batch was removed."
        db.session.commit()
        return batch
    finally:
        _unlock(batch.id)


def cancel_validated_batch(batch_id: int, *, user) -> None:
    batch = db.session.get(ImportBatch, batch_id)
    if batch is None:
        return
    resolve_context(user, batch.client_id, batch.warehouse_id)
    if batch.status not in {
        ImportBatchStatus.UPLOADED,
        ImportBatchStatus.VALIDATING,
        ImportBatchStatus.VALIDATED,
    }:
        raise ImportErrorClosed("Only an unconfirmed preview can be cancelled.")
    db.session.execute(
        text("DELETE FROM inventory_import_rows WHERE import_batch_id = :bid"),
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
        raise ImportErrorClosed("Import is blocked. Fix validation errors and retry.")
    if preview.get("units", 0) <= 0 and not preview.get("rows"):
        raise ImportErrorClosed("Import is blocked. Fix validation errors and retry.")
    resolve_context(user, preview["client_id"], preview["warehouse_id"])
    batch = db.session.get(ImportBatch, int(preview["batch_id"]))
    if batch is None:
        raise ImportErrorClosed("Import batch was not found.")
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


def template_bytes() -> bytes:
    frame = pd.DataFrame(
        columns=["UPC", "SKU", "Description", "Style", "Color", "Size", "Quantity", "Location"]
    )
    buffer = io.BytesIO()
    frame.to_excel(buffer, index=False)
    return buffer.getvalue()
