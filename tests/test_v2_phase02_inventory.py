import io

import pandas as pd
import pytest

from app.constants import LedgerType, UnitStatus
from app.models import ImportBatch, InventoryTransaction, InventoryUnit, User
from app.services.inventory_import import (
    ImportErrorClosed,
    analyze,
    commit_import,
)
from app.services.inventory_ledger import LedgerError, create_available_unit, transition_unit
from app.services.inventory_query import aggregate_rows, status_counts
from app.services.invariants import assert_invariants
from app.services.masters import create_client, create_warehouse
from tests.conftest import create_user, form_data, login


def _xlsx(rows):
    frame = pd.DataFrame(rows)
    buffer = io.BytesIO()
    frame.to_excel(buffer, index=False)
    buffer.seek(0)
    return buffer


def _masters(db):
    celine = create_client("Celine", "CEL")
    dior = create_client("Dior", "DIO")
    db.session.commit()
    cel_ny = create_warehouse(celine, "NY", "New York")
    cel_nj = create_warehouse(celine, "NJ", "New Jersey")
    dio_ny = create_warehouse(dior, "NY", "New York")
    db.session.commit()
    return celine, dior, cel_ny, cel_nj, dio_ny


def _row(upc="123456789012", qty=5, location="A-01", **extra):
    data = {
        "UPC": upc,
        "SKU": extra.get("sku", "SKU-1"),
        "Style": extra.get("style", "ST"),
        "Color": extra.get("color", "BLK"),
        "Size": extra.get("size", "M"),
        "Quantity": qty,
        "Location": location,
    }
    if "client" in extra:
        data["Client"] = extra["client"]
    if "warehouse" in extra:
        data["Warehouse"] = extra["warehouse"]
    return data


def test_p2_01_quantity_expands_to_units(app, db, admin_user):
    celine, _, cel_ny, _, _ = _masters(db)
    preview = analyze(_xlsx([_row(qty=5)]), "inv.xlsx", celine, cel_ny)
    assert preview["has_blocking"] is False
    assert preview["units"] == 5
    batch = commit_import(preview, user=admin_user)
    assert batch.rows_imported == 5
    units = InventoryUnit.query.filter_by(client_id=celine.id, warehouse_id=cel_ny.id).all()
    assert len(units) == 5
    assert all(u.status == UnitStatus.AVAILABLE for u in units)
    assert all(u.upc == "123456789012" for u in units)
    assert_invariants()


def test_p2_02_blank_upc_blocks_entire_file(app, db, admin_user):
    celine, _, cel_ny, _, _ = _masters(db)
    preview = analyze(
        _xlsx([_row(upc="111", qty=2), _row(upc="", qty=1)]),
        "bad.xlsx",
        celine,
        cel_ny,
    )
    assert preview["has_blocking"] is True
    with pytest.raises(ImportErrorClosed):
        commit_import(preview, user=admin_user)
    assert InventoryUnit.query.count() == 0
    assert InventoryTransaction.query.count() == 0


def test_p2_03_atomic_rollback_on_injected_failure(app, db, admin_user):
    celine, _, cel_ny, _, _ = _masters(db)
    preview = analyze(_xlsx([_row(qty=4)]), "fail.xlsx", celine, cel_ny)
    with pytest.raises(LedgerError, match="injected"):
        commit_import(preview, user=admin_user, _fail_after=2)
    assert InventoryUnit.query.count() == 0
    assert InventoryTransaction.query.count() == 0
    assert_invariants()


def test_p2_04_file_context_mismatch_rejected(app, db, admin_user):
    celine, _, cel_ny, _, _ = _masters(db)
    preview = analyze(
        _xlsx([_row(qty=2, client="02-DIO", warehouse="NY")]),
        "mismatch.xlsx",
        celine,
        cel_ny,
    )
    assert preview["has_blocking"] is True
    assert any("does not match" in e["message"] for e in preview["blocking"])
    with pytest.raises(ImportErrorClosed):
        commit_import(preview, user=admin_user)
    assert InventoryUnit.query.count() == 0


def test_p2_05_same_upc_isolated_by_client(app, db, admin_user):
    celine, dior, cel_ny, _, dio_ny = _masters(db)
    for client, warehouse in ((celine, cel_ny), (dior, dio_ny)):
        preview = analyze(_xlsx([_row(upc="999", qty=3)]), "a.xlsx", client, warehouse)
        commit_import(preview, user=admin_user)
    cel_qty = status_counts(client_id=celine.id, warehouse_id=cel_ny.id, upc="999")
    dio_qty = status_counts(client_id=dior.id, warehouse_id=dio_ny.id, upc="999")
    assert cel_qty["available"] == 3
    assert dio_qty["available"] == 3
    combined = [r for r in aggregate_rows() if r["upc"] == "999"]
    assert {r["client_id"] for r in combined} == {celine.id, dior.id}
    assert all(r["available"] == 3 for r in combined)
    assert_invariants()


def test_p2_06_same_upc_isolated_by_warehouse(app, db, admin_user):
    celine, _, cel_ny, cel_nj, _ = _masters(db)
    for warehouse in (cel_ny, cel_nj):
        preview = analyze(_xlsx([_row(upc="888", qty=2, location="B-01")]), "w.xlsx", celine, warehouse)
        commit_import(preview, user=admin_user)
    ny = status_counts(client_id=celine.id, warehouse_id=cel_ny.id, upc="888")
    nj = status_counts(client_id=celine.id, warehouse_id=cel_nj.id, upc="888")
    assert ny["available"] == 2
    assert nj["available"] == 2
    rows = [r for r in aggregate_rows(client_id=celine.id) if r["upc"] == "888"]
    assert {r["warehouse_id"] for r in rows} == {cel_ny.id, cel_nj.id}


def test_p2_07_on_hand_is_sum_of_active_statuses(app, db, admin_user):
    celine, _, cel_ny, _, _ = _masters(db)
    preview = analyze(_xlsx([_row(qty=6)]), "q.xlsx", celine, cel_ny)
    commit_import(preview, user=admin_user)
    units = InventoryUnit.query.order_by(InventoryUnit.id).all()
    transition_unit(units[0], to_status=UnitStatus.RESERVED, transaction_type=LedgerType.RESERVE)
    transition_unit(units[1], to_status=UnitStatus.RESERVED, transaction_type=LedgerType.RESERVE)
    transition_unit(
        units[1],
        to_status=UnitStatus.PACKED,
        transaction_type=LedgerType.PACK,
    )
    db.session.commit()
    qty = status_counts(client_id=celine.id, warehouse_id=cel_ny.id)
    assert qty["available"] == 4
    assert qty["reserved"] == 1
    assert qty["packed"] == 1
    assert qty["on_hand"] == 6
    assert qty["on_hand"] == qty["available"] + qty["reserved"] + qty["packed"]
    assert_invariants()


def test_p2_08_import_and_transition_always_write_ledger(app, db, admin_user):
    celine, _, cel_ny, _, _ = _masters(db)
    preview = analyze(_xlsx([_row(qty=2)]), "led.xlsx", celine, cel_ny)
    commit_import(preview, user=admin_user)
    units = InventoryUnit.query.all()
    assert InventoryTransaction.query.filter_by(transaction_type=LedgerType.IMPORT).count() == 2
    for unit in units:
        txn = InventoryTransaction.query.filter_by(inventory_unit_id=unit.id).one()
        assert txn.transaction_type == LedgerType.IMPORT
        assert txn.to_status == UnitStatus.AVAILABLE
        assert txn.from_status is None
    transition_unit(units[0], to_status=UnitStatus.RESERVED, transaction_type=LedgerType.RESERVE)
    db.session.commit()
    types = [
        t.transaction_type
        for t in InventoryTransaction.query.filter_by(inventory_unit_id=units[0].id).order_by(
            InventoryTransaction.id
        )
    ]
    assert types == [LedgerType.IMPORT, LedgerType.RESERVE]
    from app.services import inventory_query

    assert not hasattr(inventory_query, "set_status")
    assert not hasattr(inventory_query, "update_unit")
    assert_invariants()


def test_p2_09_unit_cannot_belong_to_other_client_warehouse(app, db, admin_user):
    celine, dior, cel_ny, _, dio_ny = _masters(db)
    with pytest.raises(LedgerError, match="does not belong"):
        create_available_unit(
            client_id=celine.id,
            warehouse_id=dio_ny.id,
            upc="111",
            location="A-01",
        )
    db.session.rollback()
    preview = analyze(_xlsx([_row(qty=1)]), "ok.xlsx", celine, cel_ny)
    commit_import(preview, user=admin_user)
    unit = InventoryUnit.query.one()
    assert unit.client_id == cel_ny.client_id
    assert_invariants(client_id=celine.id)
    assert InventoryUnit.query.filter_by(client_id=dior.id).count() == 0


def test_p2_http_import_and_isolation(app, db, admin_client):
    celine, dior, cel_ny, _, dio_ny = _masters(db)
    create_user(
        "celuser",
        perms=["INVENTORY_VIEW", "INVENTORY_UPLOAD", "DASHBOARD_VIEW"],
        clients=[celine.id],
    )
    preview_resp = admin_client.post(
        "/inventory/upload/preview",
        data=form_data(
            admin_client,
            {
                "client_id": celine.id,
                "warehouse_id": cel_ny.id,
                "file": (_xlsx([_row(qty=3)]), "stock.xlsx"),
            },
        ),
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert preview_resp.status_code == 200
    assert b"Physical Units" in preview_resp.data
    confirm = admin_client.post(
        "/inventory/upload/confirm",
        data=form_data(admin_client),
        follow_redirects=True,
    )
    assert confirm.status_code == 200
    assert InventoryUnit.query.count() == 3

    other = app.test_client()
    login(other, "celuser")
    html = other.get("/inventory/").get_data(as_text=True)
    assert "123456789012" in html
    preview = analyze(_xlsx([_row(upc="777", qty=1)]), "d.xlsx", dior, dio_ny)
    admin = User.query.filter_by(username="admin").one()
    commit_import(preview, user=admin)
    dio_batch = ImportBatch.query.filter_by(client_id=dior.id).one()
    assert other.get(f"/inventory/imports/{dio_batch.id}").status_code == 404
    assert_invariants()
