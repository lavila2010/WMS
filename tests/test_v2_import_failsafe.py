"""Import execution failsafe: worker heartbeat + bounded web recovery."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

from app.constants import ImportBatchStatus, LedgerType, OrderStatus
from app.models import ImportBatch, InventoryTransaction, InventoryUnit, Order
from app.services.allocation import AllocationError, allocate_order
from app.services.import_execution import (
    EXECUTOR_RECOVERY,
    beat_import_worker,
    clear_import_worker_heartbeats,
    deactivate_worker_instance,
    inspect_orphaned_import_batches,
    is_batch_recovery_eligible,
    is_import_worker_healthy,
    recover_import_batch,
    recover_stale_imports,
)
from app.services.inventory_import import (
    analyze as inv_analyze,
    begin_processing as inv_begin,
    commit_import as inv_commit,
    process_import_batch as inv_process,
)
from app.services.inventory_ledger import create_available_unit
from app.services.inventory_query import status_counts
from app.services.order_import import (
    OrderImportError,
    analyze as ord_analyze,
    begin_processing as ord_begin,
    commit_import as ord_commit,
    process_import_batch as ord_process,
)
from app.workers.import_worker import process_due_batches
from tests.conftest import create_user, form_data, login
from tests.test_v2_phase02_inventory import _masters, _row, _xlsx
from tests.test_v2_phase03_orders import _line, _setup, _xlsx as ord_xlsx
from tests.test_v2_phase04_allocation import _world


@pytest.fixture(autouse=True)
def _reset_import_execution(db):
    clear_import_worker_heartbeats()
    deactivate_worker_instance()
    db.session.execute(text("SELECT pg_advisory_unlock_all()"))
    db.session.commit()
    yield
    deactivate_worker_instance()
    clear_import_worker_heartbeats()


def _enqueue_inv(user, client, warehouse, rows, filename="job.xlsx"):
    preview = inv_analyze(_xlsx(rows), filename, client, warehouse)
    assert preview["has_blocking"] is False, preview["blocking"]
    return inv_begin(preview["batch_id"], user=user, run="async"), preview


def _enqueue_ord(user, client, division, rows, filename="orders.xlsx"):
    preview = ord_analyze(ord_xlsx(rows), filename, client, division, user=user)
    assert preview["has_blocking"] is False, preview["blocking"]
    return ord_begin(preview["batch_id"], user=user, run="async"), preview


def _recover_until_done(batch_id, user, *, chunk_size=None, limit=20):
    last = None
    for _ in range(limit):
        last = recover_import_batch(batch_id, user=user, max_chunks=2, chunk_size=chunk_size)
        batch = ImportBatch.query.get(batch_id)
        if batch.status in {ImportBatchStatus.COMPLETED, ImportBatchStatus.FAILED}:
            return last
    return last


def test_a_worker_healthy_recovery_does_not_run(app, db, admin_user):
    clear_import_worker_heartbeats()
    beat_import_worker(instance_id="healthy-a")
    assert is_import_worker_healthy() is True
    celine, _, cel_ny, _, _ = _masters(db)
    batch, _ = _enqueue_inv(admin_user, celine, cel_ny, [_row(qty=4)], "a.xlsx")
    skipped = recover_import_batch(batch.id, user=admin_user)
    assert skipped["recovered"] is False
    assert skipped["reason"] == "worker_healthy"
    assert InventoryUnit.query.filter_by(import_batch_id=batch.id).count() == 0
    done = process_due_batches()
    assert done == [batch.id]
    assert db.session.get(ImportBatch, batch.id).status == ImportBatchStatus.COMPLETED
    assert db.session.get(ImportBatch, batch.id).last_executor == "worker"


def test_b_worker_missing_inventory_completes_via_recovery(app, db, admin_user):
    clear_import_worker_heartbeats()
    assert is_import_worker_healthy() is False
    celine, _, cel_ny, _, _ = _masters(db)
    batch, _ = _enqueue_inv(admin_user, celine, cel_ny, [_row(qty=5)], "b.xlsx")
    assert is_batch_recovery_eligible(batch) is True
    result = _recover_until_done(batch.id, admin_user)
    done = db.session.get(ImportBatch, batch.id)
    assert done.status == ImportBatchStatus.COMPLETED
    assert done.last_executor == EXECUTOR_RECOVERY
    assert InventoryUnit.query.filter_by(import_batch_id=batch.id).count() == 5
    assert result["recovered"] is True


def test_c_worker_dies_mid_job_web_recovery_resumes(app, db, admin_user):
    clear_import_worker_heartbeats()
    celine, _, cel_ny, _, _ = _masters(db)
    batch, _ = _enqueue_inv(admin_user, celine, cel_ny, [_row(qty=6)], "c.xlsx")
    inv_process(batch.id, chunk_size=2, max_chunks=1)
    mid = db.session.get(ImportBatch, batch.id)
    assert mid.status == ImportBatchStatus.PROCESSING
    assert mid.units_created == 2
    mid.last_progress_at = datetime.utcnow() - timedelta(seconds=35)
    db.session.commit()
    clear_import_worker_heartbeats()
    assert is_batch_recovery_eligible(mid) is True
    _recover_until_done(batch.id, admin_user, chunk_size=2)
    done = db.session.get(ImportBatch, batch.id)
    assert done.status == ImportBatchStatus.COMPLETED
    assert InventoryUnit.query.filter_by(import_batch_id=batch.id).count() == 6
    assert InventoryTransaction.query.filter_by(
        import_batch_id=batch.id, transaction_type=LedgerType.IMPORT
    ).count() == 6


def test_d_recovery_and_worker_do_not_duplicate(app, db, admin_user):
    clear_import_worker_heartbeats()
    celine, _, cel_ny, _, _ = _masters(db)
    batch, _ = _enqueue_inv(admin_user, celine, cel_ny, [_row(qty=8)], "d.xlsx")
    errors = []

    def run_worker():
        with app.app_context():
            try:
                beat_import_worker(instance_id="live-d")
                process_due_batches()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

    def run_recovery():
        with app.app_context():
            try:
                recover_import_batch(batch.id, user=admin_user, force=True)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

    threads = [threading.Thread(target=run_worker), threading.Thread(target=run_recovery)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    db.session.expire_all()
    assert InventoryUnit.query.filter_by(import_batch_id=batch.id).count() == 8
    assert db.session.get(ImportBatch, batch.id).status == ImportBatchStatus.COMPLETED


def test_e_two_browser_recover_posts_are_safe(app, db, admin_user):
    clear_import_worker_heartbeats()
    celine, _, cel_ny, _, _ = _masters(db)
    batch, _ = _enqueue_inv(admin_user, celine, cel_ny, [_row(qty=4)], "e.xlsx")
    errors = []

    def recover_once():
        with app.app_context():
            try:
                recover_import_batch(batch.id, user=admin_user)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

    threads = [threading.Thread(target=recover_once) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    db.session.expire_all()
    assert InventoryUnit.query.filter_by(import_batch_id=batch.id).count() == 4
    assert db.session.get(ImportBatch, batch.id).status == ImportBatchStatus.COMPLETED


def test_f_duplicate_recover_post_is_idempotent(app, db, admin_user):
    clear_import_worker_heartbeats()
    celine, _, cel_ny, _, _ = _masters(db)
    batch, _ = _enqueue_inv(admin_user, celine, cel_ny, [_row(qty=3)], "f.xlsx")
    first = _recover_until_done(batch.id, admin_user)
    second = recover_import_batch(batch.id, user=admin_user)
    assert first["status"] == ImportBatchStatus.COMPLETED
    assert second["reason"] == "already_complete"
    assert InventoryUnit.query.filter_by(import_batch_id=batch.id).count() == 3


def test_g_inventory_stuck_batch_completes_automatically(app, db, admin_user):
    clear_import_worker_heartbeats()
    celine, _, cel_ny, _, _ = _masters(db)
    batch, _ = _enqueue_inv(admin_user, celine, cel_ny, [_row(qty=3)], "g.xlsx")
    assert batch.progress_percent == 0
    _recover_until_done(batch.id, admin_user)
    assert db.session.get(ImportBatch, batch.id).status == ImportBatchStatus.COMPLETED


def test_h_order_stuck_batch_completes_automatically(app, db, admin_user):
    clear_import_worker_heartbeats()
    celine, _, cel_ecom, _, _, _ = _setup(db)
    batch, _ = _enqueue_ord(admin_user, celine, cel_ecom, [_line(qty=2)], "h.xlsx")
    assert is_import_worker_healthy() is False
    _recover_until_done(batch.id, admin_user)
    done = db.session.get(ImportBatch, batch.id)
    assert done.status == ImportBatchStatus.COMPLETED
    assert done.last_executor == EXECUTOR_RECOVERY
    assert Order.query.filter_by(import_batch_id=batch.id).count() == 1


def test_i_existing_processing_zero_recovers_after_inspect(app, db, admin_user):
    clear_import_worker_heartbeats()
    celine, _, cel_ny, _, _ = _masters(db)
    batch, _ = _enqueue_inv(admin_user, celine, cel_ny, [_row(qty=2)], "i.xlsx")
    batch.last_progress_at = None
    db.session.commit()
    orphaned = inspect_orphaned_import_batches()
    assert batch.id in orphaned
    results = recover_stale_imports()
    assert any(row.get("batch_id") == batch.id for row in results)
    assert db.session.get(ImportBatch, batch.id).status == ImportBatchStatus.COMPLETED


def test_j_retry_then_recovery_is_safe(app, db, admin_user):
    clear_import_worker_heartbeats()
    celine, _, cel_ny, _, _ = _masters(db)
    preview = inv_analyze(_xlsx([_row(upc="RETRY-1", qty=4)]), "j.xlsx", celine, cel_ny)
    try:
        inv_commit(preview, user=admin_user, chunk_size=2, _fail_after=2)
    except Exception:
        pass
    failed = db.session.get(ImportBatch, preview["batch_id"])
    assert failed.status == ImportBatchStatus.FAILED
    from app.services.inventory_import import retry_import

    retried = retry_import(failed.id, user=admin_user, run="async")
    assert retried.status == ImportBatchStatus.PROCESSING
    _recover_until_done(retried.id, admin_user, chunk_size=2)
    done = db.session.get(ImportBatch, retried.id)
    assert done.status == ImportBatchStatus.COMPLETED
    assert InventoryUnit.query.filter_by(import_batch_id=done.id).count() == 4


def test_k_failed_batch_stays_non_operational(app, db, admin_user):
    clear_import_worker_heartbeats()
    celine, _, cel_ecom, _, _, _ = _setup(db)
    preview = ord_analyze(
        ord_xlsx([_line(order="A", qty=1), _line(order="B", qty=1)]),
        "k.xlsx",
        celine,
        cel_ecom,
    )
    try:
        ord_commit(preview, user=admin_user, chunk_size=1, _fail_after=1)
    except OrderImportError:
        pass
    batch = db.session.get(ImportBatch, preview["batch_id"])
    assert batch.status == ImportBatchStatus.FAILED
    order = Order.query.filter_by(import_batch_id=batch.id).one()
    with pytest.raises(AllocationError, match="not operational"):
        allocate_order(order, user=admin_user)


def test_l_completed_recovery_batch_is_operational(app, db, admin_user):
    clear_import_worker_heartbeats()
    w = _world(db)
    batch, _ = _enqueue_ord(admin_user, w["celine"], w["cel_ecom"], [_line(upc="FS-OK", qty=2)], "l.xlsx")
    _recover_until_done(batch.id, admin_user)
    create_available_unit(
        client_id=w["celine"].id,
        warehouse_id=w["cel_ny"].id,
        upc="FS-OK",
        location="A-01",
        sku="SKU",
    )
    create_available_unit(
        client_id=w["celine"].id,
        warehouse_id=w["cel_ny"].id,
        upc="FS-OK",
        location="A-01",
        sku="SKU",
    )
    db.session.commit()
    order = Order.query.filter_by(import_batch_id=batch.id).one()
    result = allocate_order(order, user=admin_user)
    assert result["reserved"] == 2
    assert Order.query.get(order.id).status == OrderStatus.ALLOCATED


def test_status_get_does_not_mutate(app, db, admin_client, admin_user):
    clear_import_worker_heartbeats()
    celine, _, cel_ny, _, _ = _masters(db)
    batch, _ = _enqueue_inv(admin_user, celine, cel_ny, [_row(qty=2)], "get.xlsx")
    status = admin_client.get(f"/inventory/imports/{batch.id}/status")
    assert status.status_code == 200
    body = status.get_json()
    assert body["status"] == ImportBatchStatus.PROCESSING
    assert body["recovery_eligible"] is True
    assert body["worker_healthy"] is False
    assert InventoryUnit.query.count() == 0


def test_http_recover_post_completes_inventory(app, db, admin_client, admin_user):
    clear_import_worker_heartbeats()
    celine, _, cel_ny, _, _ = _masters(db)
    batch, _ = _enqueue_inv(admin_user, celine, cel_ny, [_row(qty=2)], "post.xlsx")
    response = admin_client.post(
        f"/imports/{batch.id}/recover",
        data=form_data(admin_client),
        follow_redirects=True,
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == ImportBatchStatus.COMPLETED
    assert InventoryUnit.query.filter_by(import_batch_id=batch.id).count() == 2


def test_http_recover_requires_permission(app, db, admin_user):
    clear_import_worker_heartbeats()
    celine, _, cel_ny, _, _ = _masters(db)
    batch, _ = _enqueue_inv(admin_user, celine, cel_ny, [_row(qty=2)], "perm.xlsx")
    create_user("viewer", perms=["INVENTORY_VIEW", "DASHBOARD_VIEW"], clients=[celine.id])
    client = app.test_client()
    login(client, "viewer")
    denied = client.post(f"/imports/{batch.id}/recover", data=form_data(client))
    assert denied.status_code == 403
    assert db.session.get(ImportBatch, batch.id).status == ImportBatchStatus.PROCESSING


def test_confirm_page_auto_recover_hook_present(app, db, admin_client):
    celine, _, cel_ny, _, _ = _masters(db)
    preview = admin_client.post(
        "/inventory/upload/preview",
        data=form_data(
            admin_client,
            {
                "client_id": celine.id,
                "warehouse_id": cel_ny.id,
                "file": (_xlsx([_row(qty=2)]), "ui.xlsx"),
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
    assert b"data-recover-url" in confirm.data
    assert b"Recovering import automatically" in confirm.data
    batch = ImportBatch.query.filter_by(filename="ui.xlsx").one()
    assert batch.status == ImportBatchStatus.PROCESSING
    assert InventoryUnit.query.count() == 0
