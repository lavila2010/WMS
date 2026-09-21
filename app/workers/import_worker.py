"""Unified durable import worker for inventory and order batches.

Render Background Worker command:

    python -m app.workers.import_worker

Compatibility alias (same dispatcher):

    python -m app.workers.inventory_import_worker

PostgreSQL ``import_batches`` (status=PROCESSING) is the queue. The worker
dispatches by ``ImportBatch.type`` and writes a heartbeat so the web failsafe
stays idle while this process is healthy.

A restart resumes from ``units_created`` / ``orders_created``.
Logs only batch/client identifiers and progress counters.
Never logs database URIs, passwords, SECRET_KEY, or merchandise attributes.
"""

from __future__ import annotations

import logging
import os
import time

from app.constants import ImportBatchStatus
from app.extensions import db
from app.models import ImportBatch
from app.services.import_execution import (
    EXECUTOR_WORKER,
    activate_worker_instance,
    beat_import_worker,
    deactivate_worker_instance,
    dispatch_import_batch,
    is_worker_instance_active,
    worker_instance_id,
)

logger = logging.getLogger("wms.import_worker")

DEFAULT_IDLE_SECONDS = 2.0


def _idle_seconds() -> float:
    raw = os.environ.get("WMS_IMPORT_WORKER_IDLE_SECONDS", str(DEFAULT_IDLE_SECONDS))
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_IDLE_SECONDS
    return max(0.2, min(value, 60.0))


def _safe_log_batch(batch: ImportBatch, event: str) -> None:
    logger.info(
        "%s batch_id=%s type=%s client_id=%s status=%s executor=worker "
        "units_expected=%s units_created=%s orders_expected=%s orders_created=%s progress=%s",
        event,
        batch.id,
        batch.type,
        batch.client_id,
        batch.status,
        batch.units_expected,
        batch.units_created,
        batch.orders_expected,
        batch.orders_created,
        batch.progress_percent,
    )


def processing_import_batch_rows() -> list[tuple[int, str]]:
    rows = (
        ImportBatch.query.filter_by(status=ImportBatchStatus.PROCESSING)
        .order_by(ImportBatch.started_at.asc().nullsfirst(), ImportBatch.id.asc())
        .with_entities(ImportBatch.id, ImportBatch.type)
        .all()
    )
    return [(row[0], row[1]) for row in rows]


def process_due_batches(*, limit: int = 1) -> list[int]:
    """Claim and process up to ``limit`` PROCESSING inventory or order batches."""
    owned = not is_worker_instance_active()
    if owned:
        activate_worker_instance()
    try:
        return _process_due_batches(limit=limit)
    finally:
        if owned:
            deactivate_worker_instance()


def _process_due_batches(*, limit: int = 1) -> list[int]:
    finished: list[int] = []
    for batch_id, batch_type in processing_import_batch_rows():
        batch = db.session.get(ImportBatch, batch_id)
        if batch is None or batch.status != ImportBatchStatus.PROCESSING:
            continue
        units_before = int(batch.units_created or 0)
        orders_before = int(batch.orders_created or 0)
        status_before = batch.status
        _safe_log_batch(batch, "claim")
        try:
            beat_import_worker(current_batch_id=batch_id)
        except Exception:
            logger.exception("heartbeat failed batch_id=%s", batch_id)
        try:
            result = dispatch_import_batch(batch_id, executor=EXECUTOR_WORKER)
        except Exception:
            logger.exception("import failed batch_id=%s type=%s executor=worker", batch_id, batch_type)
            result = db.session.get(ImportBatch, batch_id)
        try:
            beat_import_worker(current_batch_id=None)
        except Exception:
            logger.exception("heartbeat failed batch_id=%s", batch_id)
        if result is None:
            continue
        advanced = (
            result.status != status_before
            or int(result.units_created or 0) != units_before
            or int(result.orders_created or 0) != orders_before
            or result.status in {ImportBatchStatus.COMPLETED, ImportBatchStatus.FAILED}
        )
        if not advanced:
            continue
        _safe_log_batch(result, "progress" if result.status == ImportBatchStatus.PROCESSING else "done")
        finished.append(result.id)
        if len(finished) >= limit:
            break
    return finished


def run_forever(*, idle_seconds: float | None = None) -> None:
    pause = _idle_seconds() if idle_seconds is None else idle_seconds
    instance = activate_worker_instance(worker_instance_id())
    beat_import_worker(instance_id=instance, status="ONLINE")
    logger.info("import worker starting idle_seconds=%s instance=%s", pause, instance)
    while True:
        beat_import_worker(instance_id=instance, status="ONLINE")
        done = process_due_batches(limit=1)
        if not done:
            time.sleep(pause)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from app import create_app
    from app.config import Config

    app = create_app(Config())
    with app.app_context():
        run_forever()


if __name__ == "__main__":
    main()
