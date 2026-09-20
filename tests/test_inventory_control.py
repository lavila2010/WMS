import io

import pytest

from app.constants import OrderStatus, UnitStatus
from app.models import (
    Client,
    ImportBatch,
    InventoryException,
    InventoryMovement,
    InventoryUnit,
    Warehouse,
)
from app.services import inventory_query as invq
from app.services.allocation import allocate_barcode
from app.services.imports import build_inventory_workbook
from app.services.inventory_import import analyze, confirm_import
from app.workflow import transition
from tests.conftest import make_order, make_unit


def _wb(rows):
    return build_inventory_workbook(rows)


def _row(client, warehouse, upc, sku, barcode, location, description=""):
    return {
        "client": client, "warehouse": warehouse, "upc": upc, "sku": sku,
        "barcode": barcode, "location": location, "description": description,
    }


def _client_id(code):
    return Client.query.filter_by(code=code).first().id


def _warehouse_id(client_code, wh_code):
    c = Client.query.filter_by(code=client_code).first()
    return Warehouse.query.filter_by(client_id=c.id, code=wh_code).first().id


# 1. Inventory import requires UPC
def test_import_requires_upc(db):
    preview = analyze(_wb([_row("ACME", "WH1", "", "SKU-A", "B-1", "A-01")]), "inv.xlsx")
    assert preview.has_blocking
    assert any(e["type"] == "INVALID_UPC" for e in preview.blocking)


# 2. Inventory import does NOT require OrderType
def test_import_does_not_require_order_type(db):
    # Workbook has no OrderType column at all.
    result = confirm_import(_wb([_row("ACME", "WH1", "U1", "SKU-A", "B-1", "A-01")]), "inv.xlsx")
    assert result["status"] == "COMPLETED"
    assert InventoryUnit.query.filter_by(barcode="B-1").count() == 1


# 3. Multiple clients in one inventory file
def test_multiple_clients_one_file(db):
    confirm_import(_wb([
        _row("ACME", "WH1", "U1", "SKU-A", "B-1", "A-01"),
        _row("GLOBEX", "WHX", "U2", "SKU-A", "B-2", "G-01"),
    ]), "inv.xlsx")
    assert Client.query.count() == 2
    assert InventoryUnit.query.filter_by(barcode="B-1").first().client.code == "ACME"
    assert InventoryUnit.query.filter_by(barcode="B-2").first().client.code == "GLOBEX"


# 4. Multiple warehouses in one inventory file
def test_multiple_warehouses_one_file(db):
    confirm_import(_wb([
        _row("ACME", "NY", "U1", "SKU-A", "B-1", "A-01"),
        _row("ACME", "NJ", "U2", "SKU-A", "B-2", "A-01"),
    ]), "inv.xlsx")
    c = Client.query.filter_by(code="ACME").first()
    assert Warehouse.query.filter_by(client_id=c.id).count() == 2


# 5. Duplicate barcode within upload blocks import
def test_duplicate_barcode_in_upload_blocks(db):
    preview = analyze(_wb([
        _row("ACME", "WH1", "U1", "SKU-A", "DUP", "A-01"),
        _row("ACME", "WH1", "U1", "SKU-A", "DUP", "A-02"),
    ]), "inv.xlsx")
    assert preview.has_blocking
    assert any(e["type"] == "DUPLICATE_BARCODE" for e in preview.blocking)


# 6. Preview performs no DB inventory writes
def test_preview_no_db_writes(db):
    analyze(_wb([_row("ACME", "WH1", "U1", "SKU-A", "B-1", "A-01")]), "inv.xlsx")
    assert InventoryUnit.query.count() == 0
    assert ImportBatch.query.count() == 0


# 7. Confirmation performs writes
def test_confirmation_writes(db):
    result = confirm_import(_wb([_row("ACME", "WH1", "U1", "SKU-A", "B-1", "A-01")]), "inv.xlsx")
    assert result["rows_imported"] == 1
    assert InventoryUnit.query.count() == 1
    assert ImportBatch.query.filter_by(type="INVENTORY", status="COMPLETED").count() == 1


# 8. Blocking errors reject confirmation
def test_blocking_errors_reject_confirmation(db):
    result = confirm_import(_wb([
        _row("ACME", "WH1", "U1", "SKU-A", "DUP", "A-01"),
        _row("ACME", "WH1", "U1", "SKU-A", "DUP", "A-02"),
    ]), "inv.xlsx")
    assert result["status"] == "REJECTED"
    assert InventoryUnit.query.count() == 0
    assert InventoryException.query.count() >= 1


# 9 & 10. UPC summary quantities + location breakdown
def test_upc_summary_and_location_breakdown(db):
    confirm_import(_wb([
        _row("ACME", "WH1", "U1", "SKU-A", "B-1", "A-01"),
        _row("ACME", "WH1", "U1", "SKU-A", "B-2", "A-01"),
        _row("ACME", "WH1", "U1", "SKU-A", "B-3", "A-02"),
    ]), "inv.xlsx")
    cid, wid = _client_id("ACME"), _warehouse_id("ACME", "WH1")
    summary = invq.upc_summary(cid, wid, "U1")
    assert summary["total"] == 3 and summary["available"] == 3
    assert summary["num_locations"] == 2
    locs = {l["location"]: l["total"] for l in summary["locations"]}
    assert locs == {"A-01": 2, "A-02": 1}


# 11. Barcode lookup
def test_barcode_lookup(client, db):
    confirm_import(_wb([_row("ACME", "WH1", "U1", "SKU-A", "BC-LOOK", "A-01")]), "inv.xlsx")
    resp = client.get("/inventory/barcode/BC-LOOK")
    assert resp.status_code == 200
    assert b"BC-LOOK" in resp.data


# 12. Client inventory isolation
def test_client_inventory_isolation(db):
    confirm_import(_wb([
        _row("ACME", "WH1", "U1", "SKU-A", "B-1", "A-01"),
        _row("GLOBEX", "WH1", "U1", "SKU-A", "B-2", "A-01"),
    ]), "inv.xlsx")
    assert invq.kpis(_client_id("ACME"))["total"] == 1
    assert invq.kpis(_client_id("GLOBEX"))["total"] == 1


# 13. Warehouse inventory isolation
def test_warehouse_inventory_isolation(db):
    confirm_import(_wb([
        _row("ACME", "NY", "U1", "SKU-A", "B-1", "A-01"),
        _row("ACME", "NJ", "U1", "SKU-A", "B-2", "A-01"),
    ]), "inv.xlsx")
    cid = _client_id("ACME")
    assert invq.kpis(cid, _warehouse_id("ACME", "NY"))["total"] == 1
    assert invq.kpis(cid, _warehouse_id("ACME", "NJ"))["total"] == 1


# 14/15/16/19. Same client/warehouse inventory available to different order types
def test_same_pool_across_order_types(db):
    make_unit("P-1", "SKU-A", client="ACME", warehouse="WH1")
    make_unit("P-2", "SKU-A", client="ACME", warehouse="WH1")
    ecom = make_order("SO-E", client="ACME", warehouse="WH1", order_type="ECOM", lines=[("SKU-A", 1)])
    retail = make_order("SO-R", client="ACME", warehouse="WH1", order_type="RETAIL", lines=[("SKU-A", 1)])
    transition(ecom, OrderStatus.VALIDATED); transition(retail, OrderStatus.VALIDATED)
    db.session.commit()
    assert allocate_barcode(ecom, "P-1").order_id == ecom.id
    assert allocate_barcode(retail, "P-2").order_id == retail.id
    db.session.commit()


# 20. Inventory transaction history
def test_inventory_transaction_history(db):
    confirm_import(_wb([_row("ACME", "WH1", "U1", "SKU-A", "B-1", "A-01")]), "inv.xlsx")
    movements = InventoryMovement.query.filter_by(barcode="B-1").all()
    assert any(m.movement_type == "IMPORT_RECEIVE" for m in movements)
    assert movements[0].upc == "U1" and movements[0].client is not None


# 21. Import history
def test_import_history(db):
    confirm_import(_wb([_row("ACME", "WH1", "U1", "SKU-A", "B-1", "A-01")]), "inv.xlsx")
    batch = ImportBatch.query.filter_by(type="INVENTORY").first()
    assert batch.rows_submitted == 1 and batch.rows_imported == 1
    assert "ACME" in (batch.clients or "")


# 22. Inventory search by UPC
def test_search_by_upc(client, db):
    confirm_import(_wb([_row("ACME", "WH1", "UPC-XYZ", "SKU-A", "B-1", "A-01")]), "inv.xlsx")
    resp = client.get("/inventory/search?q=UPC-XYZ")
    assert resp.status_code == 200
    assert b"B-1" in resp.data


# 23. Inventory search by location
def test_search_by_location(client, db):
    confirm_import(_wb([_row("ACME", "WH1", "U1", "SKU-A", "B-1", "RACK-9")]), "inv.xlsx")
    resp = client.get("/inventory/search?q=RACK-9")
    assert resp.status_code == 200
    assert b"B-1" in resp.data
