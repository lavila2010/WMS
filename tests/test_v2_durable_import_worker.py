"""Durable inventory import worker: PostgreSQL queue, no web-thread executor."""

from __future__ import annotations

import threading

from app.constants import ImportBatchStatus, LedgerType, OrderStatus
from app.models import ImportBatch, InventoryTransaction, InventoryUnit, Order
from app.services.allocation import allocate_order
from app.services.inventory_import import (
    analyze,
    begin_processing,
    commit_import,
    process_import_batch,
)
from app.services.inventory_query import status_counts
from app.workers.inventory_import_worker import process_due_batches
from tests.conftest import create_user, form_data, login
from tests.test_v2_phase02_inventory import _masters, _row, _xlsx
from tests.test_v2_phase03_orders import _line
from tests.test_v2_phase04_allocation import _import_order, _world


def _enqueue(user, client, warehouse, rows, filename="job.xlsx"):
    preview = analyze(_xlsx(rows), filename, client, warehouse)
    assert preview["has_blocking"] is False, preview["blocking"]
    batch = begin_processing(preview["batch_id"], user=user, run="async")
    return batch, preview


def test_confirm_returns_without_spawning_thread(app, db, admin_user):
    celine, _, cel_ny, _, _ = _masters(db)
    before = {t.ident for t in threading.enumerate()}
    batch, _ = _enqueue(admin_user, celine, cel_ny, [_row(qty=4)], "quick.xlsx")
    after = {t.ident for t in threading.enumerate()}
    assert batch.status == ImportBatchStatus.PROCESSING
    assert InventoryUnit.query.filter_by(import_batch_id=batch.id).count() == 0
    extra = after - before
    named = [t.name for t in threading.enumerate() if t.ident in extra]
    assert not any(name.startswith("inv-import-") for name in named)
    assert InventoryTransaction.query.filter_by(import_batch_id=batch.id).count() == 0


def test_worker_picks_processing_batch(app, db, admin_user):
    celine, _, cel_ny, _, _ = _masters(db)
    batch, _ = _enqueue(admin_user, celine, cel_ny, [_row(qty=5)], "pick.xlsx")
    done = process_due_batches()
    assert done == [batch.id]
    batch = db.session.get(ImportBatch, batch.id)
    assert batch.status == ImportBatchStatus.COMPLETED
    assert batch.units_created == 5
    assert InventoryTransaction.query.filter_by(
        import_batch_id=batch.id, transaction_type=LedgerType.IMPORT
    ).count() == 5


def test_worker_restart_mid_import_resumes_without_duplicates(app, db, admin_user):
    celine, _, cel_ny, _, _ = _masters(db)
    batch, _ = _enqueue(admin_user, celine, cel_ny, [_row(qty=6)], "resume.xlsx")
    process_import_batch(batch.id, chunk_size=2, max_chunks=1)
    mid = db.session.get(ImportBatch, batch.id)
    assert mid.status == ImportBatchStatus.PROCESSING
    assert mid.units_created == 2
    process_due_batches()
    done = db.session.get(ImportBatch, batch.id)
    assert done.status == ImportBatchStatus.COMPLETED
    assert InventoryUnit.query.filter_by(import_batch_id=batch.id).count() == 6
    assert InventoryTransaction.query.filter_by(
        import_batch_id=batch.id, transaction_type=LedgerType.IMPORT
    ).count() == 6


def test_two_workers_cannot_process_same_batch(app, db, admin_user):
    celine, _, cel_ny, _, _ = _masters(db)
    batch, _ = _enqueue(admin_user, celine, cel_ny, [_row(qty=8)], "race.xlsx")
    errors = []

    def run_worker():
        with app.app_context():
            try:
                process_due_batches()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

    threads = [threading.Thread(target=run_worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    db.session.expire_all()
    assert InventoryUnit.query.filter_by(import_batch_id=batch.id).count() == 8
    assert InventoryTransaction.query.filter_by(
        import_batch_id=batch.id, transaction_type=LedgerType.IMPORT
    ).count() == 8
    assert db.session.get(ImportBatch, batch.id).status == ImportBatchStatus.COMPLETED


def test_browser_not_polling_does_not_stop_import(app, db, admin_user):
    celine, _, cel_ny, _, _ = _masters(db)
    batch, _ = _enqueue(admin_user, celine, cel_ny, [_row(qty=3)], "nopoll.xlsx")
    # No status/advance calls — worker alone finishes the job.
    process_due_batches()
    assert db.session.get(ImportBatch, batch.id).status == ImportBatchStatus.COMPLETED
    assert InventoryUnit.query.filter_by(import_batch_id=batch.id).count() == 3


def test_processing_batch_survives_app_restart(app, db, admin_user):
    celine, _, cel_ny, _, _ = _masters(db)
    batch, _ = _enqueue(admin_user, celine, cel_ny, [_row(qty=4)], "restart.xlsx")
    batch_id = batch.id
    db.session.remove()
    revived = db.session.get(ImportBatch, batch_id)
    assert revived.status == ImportBatchStatus.PROCESSING
    assert revived.units_created == 0
    process_due_batches()
    assert db.session.get(ImportBatch, batch_id).status == ImportBatchStatus.COMPLETED
    assert InventoryUnit.query.filter_by(import_batch_id=batch_id).count() == 4


def test_failed_batch_remains_non_operational(app, db, admin_user):
    celine, _, cel_ny, _, _ = _masters(db)
    preview = analyze(_xlsx([_row(upc="FAIL-1", qty=6)]), "fail.xlsx", celine, cel_ny)
    try:
        commit_import(preview, user=admin_user, chunk_size=2, _fail_after=2)
    except Exception:
        pass
    failed = db.session.get(ImportBatch, preview["batch_id"])
    assert failed.status == ImportBatchStatus.FAILED
    assert status_counts(client_id=celine.id, warehouse_id=cel_ny.id, upc="FAIL-1")["available"] == 0


def test_completed_batch_becomes_allocatable(app, db, admin_user):
    w = _world(db)
    batch, _ = _enqueue(admin_user, w["celine"], w["cel_ny"], [_row(upc="ALLOC-1", qty=2)], "alloc.xlsx")
    process_due_batches()
    assert db.session.get(ImportBatch, batch.id).status == ImportBatchStatus.COMPLETED
    order = _import_order(admin_user, w["celine"], w["cel_ecom"], [_line(upc="ALLOC-1", qty=2)])
    result = allocate_order(order, user=admin_user)
    assert result["reserved"] == 2
    assert Order.query.get(order.id).status == OrderStatus.ALLOCATED


def test_http_confirm_enqueues_only(app, db, admin_client):
    celine, _, cel_ny, _, _ = _masters(db)
    preview = admin_client.post(
        "/inventory/upload/preview",
        data=form_data(
            admin_client,
            {
                "client_id": celine.id,
                "warehouse_id": cel_ny.id,
                "file": (_xlsx([_row(qty=2)]), "http.xlsx"),
            },
        ),
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert preview.status_code == 200
    confirm = admin_client.post(
        "/inventory/upload/confirm",
        data=form_data(admin_client),
        follow_redirects=True,
    )
    assert confirm.status_code == 200
    assert b"data-advance-url" not in confirm.data
    assert b"/advance" not in confirm.data
    batch = ImportBatch.query.filter_by(filename="http.xlsx").one()
    assert batch.status == ImportBatchStatus.PROCESSING
    assert InventoryUnit.query.count() == 0
    process_due_batches()
    assert InventoryUnit.query.count() == 2


def test_advance_is_admin_recovery_only(app, db, admin_user):
    celine, _, cel_ny, _, _ = _masters(db)
    create_user(
        "uploader",
        perms=["INVENTORY_VIEW", "INVENTORY_UPLOAD", "DASHBOARD_VIEW"],
        clients=[celine.id],
    )
    batch, _ = _enqueue(admin_user, celine, cel_ny, [_row(qty=2)], "adv.xlsx")
    other = app.test_client()
    login(other, "uploader")
    denied = other.post(f"/inventory/imports/{batch.id}/advance", data=form_data(other))
    assert denied.status_code == 403
    assert batch.status == ImportBatchStatus.PROCESSING
    assert InventoryUnit.query.filter_by(import_batch_id=batch.id).count() == 0
