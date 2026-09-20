"""V2 inventory import: context → parse → preview → validate all → atomic commit.

Client and Warehouse come from the server-validated selected context.
Legacy Client/Warehouse spreadsheet columns may only confirm that context.
"""

from __future__ import annotations

import io
import json
import re
import uuid
from pathlib import Path

import pandas as pd
from flask import current_app

from ..auth import current_actor, record_audit
from ..constants import ImportType, LedgerType, UnitStatus
from ..extensions import db
from ..models import Client, ImportBatch, InventoryUnit, Warehouse
from .inventory_ledger import LedgerError, create_available_unit
from .tenant import user_can_access_client

REQUIRED = ("upc", "sku", "description", "style", "color", "size", "quantity", "location")
OPTIONAL_CONTEXT = ("client", "warehouse")
DESCRIPTION_MAX = 255

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


class ImportErrorClosed(ValueError):
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
    path = Path(current_app.config["DOCUMENTS_DIR"]) / "_inventory_previews"
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


def analyze(source, filename: str, client: Client, warehouse: Warehouse) -> dict:
    raw = source.read() if hasattr(source, "read") else source
    try:
        frame = pd.read_excel(io.BytesIO(raw))
    except Exception as exc:  # noqa: BLE001
        raise ImportErrorClosed(f"Unable to read Excel file: {exc}") from exc

    mapped = _map_columns(frame.columns)
    missing = [col for col in REQUIRED if col not in mapped]
    blocking: list[dict] = []
    warnings: list[dict] = []
    rows: list[dict] = []
    if missing:
        blocking.append(
            {
                "type": "COLUMNS",
                "message": "Missing required columns: " + ", ".join(c.upper() for c in missing),
            }
        )
        return _preview_payload(filename, client, warehouse, frame, blocking, warnings, rows)

    for index, series in frame.iterrows():
        excel_row = int(index) + 2
        upc = _cell(series[mapped["upc"]])
        quantity_raw = _cell(series[mapped["quantity"]])
        location = _cell(series[mapped["location"]])
        sku = _cell(series[mapped["sku"]])
        description = _cell(series[mapped["description"]])
        style = _cell(series[mapped["style"]])
        color = _cell(series[mapped["color"]])
        size = _cell(series[mapped["size"]])
        file_client = _cell(series[mapped["client"]]) if "client" in mapped else ""
        file_warehouse = _cell(series[mapped["warehouse"]]) if "warehouse" in mapped else ""

        row_errors = []
        if not upc:
            row_errors.append("UPC is required.")
        try:
            quantity = int(float(quantity_raw)) if quantity_raw else 0
        except ValueError:
            quantity = 0
            row_errors.append("Quantity must be an integer greater than 0.")
        else:
            if quantity <= 0:
                row_errors.append("Quantity must be an integer greater than 0.")
        if not description:
            row_errors.append("Description is required.")
        elif len(description) > DESCRIPTION_MAX:
            row_errors.append(f"Description must be {DESCRIPTION_MAX} characters or fewer.")
        if not location:
            row_errors.append("Location is required.")
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

        if row_errors:
            blocking.append({"type": "ROW", "message": f"Row {excel_row}: " + " ".join(row_errors)})
            continue
        rows.append(
            {
                "excel_row": excel_row,
                "upc": upc,
                "sku": sku,
                "description": description,
                "style": style,
                "color": color,
                "size": size,
                "quantity": quantity,
                "location": location,
            }
        )

    return _preview_payload(filename, client, warehouse, frame, blocking, warnings, rows)


def _preview_payload(filename, client, warehouse, frame, blocking, warnings, rows) -> dict:
    units = sum(r["quantity"] for r in rows)
    return {
        "filename": filename,
        "client_id": client.id,
        "warehouse_id": warehouse.id,
        "client_code": client.client_code,
        "warehouse_code": warehouse.warehouse_code,
        "total_rows": int(len(frame)),
        "valid_rows": len(rows),
        "units": units,
        "unique_upcs": len({r["upc"] for r in rows}),
        "unique_skus": len({r["sku"] for r in rows if r["sku"]}),
        "unique_locations": len({r["location"] for r in rows}),
        "blocking": blocking,
        "warnings": warnings,
        "has_blocking": len(blocking) > 0,
        "rows": rows,
    }


def commit_import(
    preview: dict,
    *,
    user,
    _fail_after: int | None = None,
) -> ImportBatch:
    if preview.get("has_blocking") or not preview.get("rows"):
        raise ImportErrorClosed("Import is blocked. Fix validation errors and retry.")
    client, warehouse = resolve_context(user, preview["client_id"], preview["warehouse_id"])
    uid, uname = current_actor()
    batch = ImportBatch(
        client_id=client.id,
        warehouse_id=warehouse.id,
        type=ImportType.INVENTORY,
        filename=preview["filename"],
        status="COMPLETED",
        rows_submitted=preview["total_rows"],
        rows_imported=0,
        rows_rejected=0,
        message=None,
        created_by_user_id=uid,
        created_by_username=uname,
    )
    db.session.add(batch)
    db.session.flush()
    created = 0
    try:
        for row in preview["rows"]:
            for _ in range(row["quantity"]):
                create_available_unit(
                    client_id=client.id,
                    warehouse_id=warehouse.id,
                    upc=row["upc"],
                    location=row["location"],
                    sku=row["sku"],
                    description=row["description"],
                    style=row["style"],
                    color=row["color"],
                    size=row["size"],
                    import_batch_id=batch.id,
                    user_id=uid,
                    reference=f"import:{batch.id}",
                    transaction_type=LedgerType.IMPORT,
                )
                created += 1
                if _fail_after is not None and created >= _fail_after:
                    raise LedgerError("injected failure")
        batch.rows_imported = created
        record_audit(
            "INVENTORY_IMPORT",
            module="Inventory",
            entity_type="import_batch",
            entity_id=batch.id,
            client_id=client.id,
            detail=f"{created} units from {preview['filename']}",
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return batch


def template_bytes() -> bytes:
    frame = pd.DataFrame(
        columns=["UPC", "SKU", "Description", "Style", "Color", "Size", "Quantity", "Location"]
    )
    buffer = io.BytesIO()
    frame.to_excel(buffer, index=False)
    return buffer.getvalue()
