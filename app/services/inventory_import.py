"""Two-step inventory import: VALIDATE -> PREVIEW -> CONFIRM.

Preview performs **no** database writes. Only an explicit confirm inserts /
updates ``InventoryUnit`` rows, records movements, and creates an
``ImportBatch`` — atomically, with rollback on failure.

Inventory is partitioned only by Client + Warehouse. Order Type is not part of
the inventory model.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from ..constants import (
    ImportType,
    InventoryExceptionType,
    MovementType,
    UnitStatus,
)
from ..extensions import db
from ..models import ImportBatch, InventoryException, InventoryUnit
from .movements import record_movement
from .scope import get_or_create_client, get_or_create_warehouse

REQUIRED_COLUMNS = ["client", "warehouse", "upc", "sku", "barcode", "location"]


def _norm(col: str) -> str:
    import re

    return re.sub(r"[^a-z0-9]", "", str(col).lower())


def _val(row, key) -> str:
    return str(row.get(key, "")).strip()


class InventoryPreview:
    def __init__(self, filename: str):
        self.filename = filename
        self.total_rows = 0
        self.clients: set[str] = set()
        self.warehouses: set[str] = set()
        self.upcs: set[str] = set()
        self.skus: set[str] = set()
        self.barcodes: set[str] = set()
        self.locations: set[str] = set()
        self.valid_rows = 0
        self.warnings: list[dict] = []
        self.blocking: list[dict] = []
        self.breakdown: dict[str, dict[str, int]] = {}
        self.row_actions: list[dict] = []

    @property
    def has_blocking(self) -> bool:
        return len(self.blocking) > 0

    def as_dict(self) -> dict:
        return {
            "filename": self.filename,
            "total_rows": self.total_rows,
            "clients": sorted(self.clients),
            "warehouses": sorted(self.warehouses),
            "unique_upcs": len(self.upcs),
            "unique_skus": len(self.skus),
            "unique_barcodes": len(self.barcodes),
            "unique_locations": len(self.locations),
            "valid_rows": self.valid_rows,
            "warnings": self.warnings,
            "blocking": self.blocking,
            "breakdown": self.breakdown,
            "has_blocking": self.has_blocking,
        }


def analyze(source, filename: str) -> InventoryPreview:
    """Parse and validate an inventory workbook without writing to the DB."""
    preview = InventoryPreview(filename)

    try:
        df = pd.read_excel(source, engine="openpyxl", dtype=str)
    except Exception as exc:  # noqa: BLE001 - report as blocking
        preview.blocking.append(
            {"row": None, "type": "INVALID_WORKBOOK", "message": f"Invalid workbook: {exc}"}
        )
        return preview

    df.columns = [_norm(c) for c in df.columns]
    df = df.fillna("")

    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        preview.blocking.append(
            {
                "row": None,
                "type": "MISSING_COLUMNS",
                "message": f"Missing required column(s): {', '.join(missing)}",
            }
        )
        return preview

    seen_barcodes: dict[str, int] = {}
    for idx, row in df.iterrows():
        rownum = int(idx) + 2  # header is row 1
        preview.total_rows += 1

        client = _val(row, "client")
        warehouse = _val(row, "warehouse")
        upc = _val(row, "upc")
        sku = _val(row, "sku")
        barcode = _val(row, "barcode")
        location = _val(row, "location")
        description = _val(row, "description")

        row_blocking = False

        def block(exc_type, message):
            nonlocal row_blocking
            row_blocking = True
            preview.blocking.append({
                "row": rownum, "type": exc_type, "message": message,
                "barcode": barcode or None, "upc": upc or None,
                "client": client or None, "warehouse": warehouse or None,
            })

        if not client:
            block(InventoryExceptionType.UNKNOWN_CLIENT, f"Row {rownum}: blank Client.")
        if not warehouse:
            block(InventoryExceptionType.UNKNOWN_WAREHOUSE, f"Row {rownum}: blank Warehouse.")
        if not upc:
            block(InventoryExceptionType.INVALID_UPC, f"Row {rownum}: blank UPC.")
        if not sku:
            block("BLANK_SKU", f"Row {rownum}: blank SKU.")
        if not barcode:
            block(InventoryExceptionType.DUPLICATE_BARCODE, f"Row {rownum}: blank Barcode.")
        if not location:
            block(InventoryExceptionType.MISSING_LOCATION, f"Row {rownum}: blank Location.")

        if barcode:
            if barcode in seen_barcodes:
                block(
                    InventoryExceptionType.DUPLICATE_BARCODE,
                    f"Row {rownum}: barcode {barcode} duplicated in file (also row {seen_barcodes[barcode]}).",
                )
            else:
                seen_barcodes[barcode] = rownum

        action = None
        if barcode and client and warehouse:
            existing = InventoryUnit.query.filter_by(barcode=barcode).first()
            if existing is not None:
                if existing.client and existing.client.code != client:
                    block(
                        InventoryExceptionType.BARCODE_CLIENT_CONFLICT,
                        f"Row {rownum}: barcode {barcode} already assigned to client "
                        f"{existing.client.code}.",
                    )
                elif existing.warehouse and existing.warehouse.code != warehouse:
                    block(
                        InventoryExceptionType.BARCODE_WAREHOUSE_CONFLICT,
                        f"Row {rownum}: barcode {barcode} already assigned to warehouse "
                        f"{existing.warehouse.code}.",
                    )
                else:
                    # Same client+warehouse -> update path with change warnings.
                    action = "update"
                    if existing.description != (description or None):
                        preview.warnings.append({"row": rownum, "type": "DESCRIPTION_CHANGED", "message": f"Row {rownum}: description changed for {barcode}."})
                    if existing.location != location:
                        preview.warnings.append({"row": rownum, "type": "LOCATION_CHANGED", "message": f"Row {rownum}: location {existing.location} -> {location} for {barcode}."})
                    if existing.upc != upc:
                        preview.warnings.append({"row": rownum, "type": "UPC_CHANGED", "message": f"Row {rownum}: UPC changed for {barcode}."})
                    if existing.sku != sku:
                        preview.warnings.append({"row": rownum, "type": "SKU_CHANGED", "message": f"Row {rownum}: SKU changed for {barcode}."})
            else:
                action = "create"

        if row_blocking or action is None:
            continue

        preview.valid_rows += 1
        preview.clients.add(client)
        preview.warehouses.add(warehouse)
        preview.upcs.add(upc)
        preview.skus.add(sku)
        preview.barcodes.add(barcode)
        preview.locations.add(location)
        preview.breakdown.setdefault(client, {}).setdefault(warehouse, 0)
        preview.breakdown[client][warehouse] += 1
        preview.row_actions.append(
            {
                "action": action,
                "client": client,
                "warehouse": warehouse,
                "upc": upc,
                "sku": sku,
                "barcode": barcode,
                "location": location,
                "description": description or None,
            }
        )

    return preview


def confirm_import(source, filename: str, actor: str = "system") -> dict:
    """Validate again and, if there are no blocking errors, import atomically.

    Returns a result dict with counts. If blocking errors exist, records
    inventory exceptions, creates a REJECTED ImportBatch, and imports nothing.
    """
    preview = analyze(source, filename)

    if preview.has_blocking:
        batch = ImportBatch(
            type=ImportType.INVENTORY,
            filename=filename,
            status="REJECTED",
            rows_submitted=preview.total_rows,
            rows_imported=0,
            rows_updated=0,
            rows_rejected=preview.total_rows,
            warnings_count=len(preview.warnings),
            clients=", ".join(sorted(preview.clients)) or None,
            warehouses=", ".join(sorted(preview.warehouses)) or None,
            created_by=actor,
            message=f"Rejected: {len(preview.blocking)} blocking error(s).",
        )
        db.session.add(batch)
        db.session.flush()
        for err in preview.blocking:
            etype = err["type"] if err["type"] in InventoryExceptionType.ALL else InventoryExceptionType.DUPLICATE_BARCODE
            db.session.add(
                InventoryException(
                    type=etype,
                    barcode=err.get("barcode"),
                    upc=err.get("upc"),
                    client_code=err.get("client"),
                    warehouse_code=err.get("warehouse"),
                    details=err["message"],
                    status="OPEN",
                )
            )
        db.session.commit()
        return {
            "status": "REJECTED",
            "batch_id": batch.id,
            "rows_submitted": preview.total_rows,
            "rows_imported": 0,
            "rows_updated": 0,
            "rows_rejected": preview.total_rows,
            "warnings": len(preview.warnings),
            "blocking": preview.blocking,
            "timestamp": batch.created_at,
            "created_by": actor,
        }

    try:
        batch = ImportBatch(
            type=ImportType.INVENTORY,
            filename=filename,
            status="COMPLETED",
            rows_submitted=preview.total_rows,
            warnings_count=len(preview.warnings),
            clients=", ".join(sorted(preview.clients)) or None,
            warehouses=", ".join(sorted(preview.warehouses)) or None,
            created_by=actor,
        )
        db.session.add(batch)
        db.session.flush()

        imported = 0
        updated = 0
        for act in preview.row_actions:
            client = get_or_create_client(act["client"])
            warehouse = get_or_create_warehouse(client, act["warehouse"])
            if act["action"] == "update":
                unit = InventoryUnit.query.filter_by(barcode=act["barcode"]).first()
                prev_location = unit.location
                unit.upc = act["upc"]
                unit.sku = act["sku"]
                unit.description = act["description"]
                unit.location = act["location"]
                unit.import_batch_id = batch.id
                record_movement(
                    unit,
                    from_status=unit.status,
                    to_status=unit.status,
                    from_location=prev_location,
                    to_location=act["location"],
                    movement_type=MovementType.IMPORT_UPDATE,
                    actor=actor,
                    reason=f"Import update ({filename})",
                )
                updated += 1
            else:
                unit = InventoryUnit(
                    barcode=act["barcode"],
                    upc=act["upc"],
                    sku=act["sku"],
                    description=act["description"],
                    location=act["location"],
                    status=UnitStatus.AVAILABLE,
                    client_id=client.id,
                    warehouse_id=warehouse.id,
                    import_batch_id=batch.id,
                )
                db.session.add(unit)
                db.session.flush()
                record_movement(
                    unit,
                    from_status=None,
                    to_status=UnitStatus.AVAILABLE,
                    from_location=None,
                    to_location=act["location"],
                    movement_type=MovementType.IMPORT_RECEIVE,
                    actor=actor,
                    reason=f"Import receive ({filename})",
                )
                imported += 1

        batch.rows_imported = imported
        batch.rows_updated = updated
        batch.rows_rejected = 0
        batch.row_count = imported + updated
        batch.message = f"Imported {imported}, updated {updated}, warnings {len(preview.warnings)}."
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    return {
        "status": "COMPLETED",
        "batch_id": batch.id,
        "rows_submitted": preview.total_rows,
        "rows_imported": imported,
        "rows_updated": updated,
        "rows_rejected": 0,
        "warnings": len(preview.warnings),
        "timestamp": batch.created_at,
        "created_by": actor,
    }
