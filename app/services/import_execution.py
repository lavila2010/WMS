"""Durable import execution: worker heartbeat + bounded web recovery.

Primary executor is ``python -m app.workers.import_worker``.
When that worker's heartbeat is missing or stale, authenticated UI polling
POSTs bounded recovery work. GET status never mutates.
"""

from __future__ import annotations

import logging
import os
import socket
from datetime import datetime, timedelta

from sqlalchemy import text

from ..constants import ImportBatchStatus, ImportType
from ..extensions import db
from ..models import ImportBatch, WorkerHeartbeat

logger = logging.getLogger("wms.import_execution")

IMPORT_WORKER_NAME = "wms-v2-import-worker"
IMPORT_WORKER_TYPE = "import"
HEARTBEAT_STALE_SECONDS = 20
STALE_PROGRESS_SECONDS = 30
RECOVERY_MAX_CHUNKS = 2
EXECUTOR_WORKER = "worker"
EXECUTOR_RECOVERY = "web-recovery"
EXECUTOR_CLI = "cli"

_active_instance_id: str | None = None


def worker_instance_id() -> str:
    override = os.environ.get("WMS_IMPORT_WORKER_INSTANCE")
    if override:
        return override[:128]
    return f"{socket.gethostname()}:{os.getpid()}"[:128]


def activate_worker_instance(instance_id: str | None = None) -> str:
    global _active_instance_id
    _active_instance_id = instance_id or worker_instance_id()
    return _active_instance_id


def deactivate_worker_instance() -> None:
    global _active_instance_id
    _active_instance_id = None


def is_worker_instance_active() -> bool:
    return _active_instance_id is not None


def beat_import_worker(
    *,
    instance_id: str | None = None,
    current_batch_id: int | None = None,
    status: str = "ONLINE",
) -> WorkerHeartbeat:
    ident = instance_id or _active_instance_id or worker_instance_id()
    now = datetime.utcnow()
    db.session.execute(
        text(
            """
            INSERT INTO worker_heartbeats (
                worker_name, worker_type, instance_id, last_seen_at, status,
                current_batch_id, created_at, updated_at
            )
            VALUES (
                :worker_name, :worker_type, :instance_id, :now, :status,
                :current_batch_id, :now, :now
            )
            ON CONFLICT (worker_name, instance_id) DO UPDATE SET
                last_seen_at = EXCLUDED.last_seen_at,
                status = EXCLUDED.status,
                current_batch_id = EXCLUDED.current_batch_id,
                updated_at = EXCLUDED.updated_at
            """
        ),
        {
            "worker_name": IMPORT_WORKER_NAME,
            "worker_type": IMPORT_WORKER_TYPE,
            "instance_id": ident,
            "now": now,
            "status": status,
            "current_batch_id": current_batch_id,
        },
    )
    db.session.commit()
    return WorkerHeartbeat.query.filter_by(
        worker_name=IMPORT_WORKER_NAME, instance_id=ident
    ).one()


def notify_progress(batch: ImportBatch) -> None:
    """Refresh last_progress_at and, if this process is the dedicated worker, heartbeat."""
    batch.last_progress_at = datetime.utcnow()
    if _active_instance_id:
        beat_import_worker(instance_id=_active_instance_id, current_batch_id=batch.id)


def worker_health() -> dict:
    now = datetime.utcnow()
    cutoff = now - timedelta(seconds=HEARTBEAT_STALE_SECONDS)
    row = (
        WorkerHeartbeat.query.filter(
            WorkerHeartbeat.worker_name == IMPORT_WORKER_NAME,
            WorkerHeartbeat.worker_type == IMPORT_WORKER_TYPE,
            WorkerHeartbeat.status == "ONLINE",
            WorkerHeartbeat.last_seen_at >= cutoff,
        )
        .order_by(WorkerHeartbeat.last_seen_at.desc())
        .first()
    )
    latest = (
        WorkerHeartbeat.query.filter_by(worker_name=IMPORT_WORKER_NAME)
        .order_by(WorkerHeartbeat.last_seen_at.desc())
        .first()
    )
    last_seen = latest.last_seen_at if latest is not None else None
    age = int((now - last_seen).total_seconds()) if last_seen is not None else None
    return {
        "healthy": row is not None,
        "worker_status": "Online" if row is not None else "Offline",
        "last_seen_at": last_seen.isoformat() + "Z" if last_seen else None,
        "heartbeat_age_seconds": age,
        "current_batch_id": row.current_batch_id if row is not None else None,
        "instance_id": row.instance_id if row is not None else None,
    }


def is_import_worker_healthy() -> bool:
    return bool(worker_health()["healthy"])


def is_batch_recovery_eligible(batch: ImportBatch | None) -> bool:
    """Recover when the dedicated worker is absent. Do not touch a live worker's batch."""
    if batch is None or batch.status != ImportBatchStatus.PROCESSING:
        return False
    if is_import_worker_healthy():
        return False
    return True


def execution_status(batch: ImportBatch | None = None) -> dict:
    health = worker_health()
    eligible = is_batch_recovery_eligible(batch) if batch is not None else False
    mode = "worker" if health["healthy"] else "recovery"
    last_progress = batch.last_progress_at if batch is not None else None
    return {
        "worker_healthy": health["healthy"],
        "worker_status": health["worker_status"],
        "executor": batch.last_executor if batch is not None else None,
        "executor_mode": mode if batch is not None and batch.status == ImportBatchStatus.PROCESSING else None,
        "recovery_eligible": eligible,
        "last_progress_at": last_progress.isoformat() + "Z" if last_progress else None,
        "heartbeat_age_seconds": health["heartbeat_age_seconds"],
        "heartbeat_last_seen_at": health["last_seen_at"],
    }


def attach_execution_status(payload: dict, batch: ImportBatch) -> dict:
    payload.update(execution_status(batch))
    return payload


def dispatch_import_batch(
    batch_id: int,
    *,
    max_chunks: int | None = None,
    chunk_size: int | None = None,
    executor: str = EXECUTOR_WORKER,
) -> ImportBatch | None:
    batch = db.session.get(ImportBatch, batch_id)
    if batch is None:
        return None
    batch_type = batch.type
    logger.info(
        "dispatch batch_id=%s type=%s executor=%s max_chunks=%s",
        batch_id,
        batch_type,
        executor,
        max_chunks,
    )
    if batch_type == ImportType.ORDERS:
        from .order_import import process_import_batch as process_order_batch

        result = process_order_batch(batch_id, chunk_size=chunk_size, max_chunks=max_chunks)
    else:
        from .inventory_import import process_import_batch as process_inventory_batch

        result = process_inventory_batch(batch_id, chunk_size=chunk_size, max_chunks=max_chunks)
    if result is None:
        return None
    result.last_executor = executor
    if result.last_progress_at is None or result.status in {
        ImportBatchStatus.COMPLETED,
        ImportBatchStatus.FAILED,
    }:
        result.last_progress_at = datetime.utcnow()
    db.session.commit()
    logger.info(
        "progress batch_id=%s type=%s executor=%s status=%s "
        "units_created=%s orders_created=%s lines_created=%s progress=%s",
        result.id,
        result.type,
        executor,
        result.status,
        result.units_created,
        result.orders_created,
        result.order_lines_created,
        result.progress_percent,
    )
    return result


def recover_import_batch(
    batch_id: int,
    *,
    user=None,
    max_chunks: int = RECOVERY_MAX_CHUNKS,
    chunk_size: int | None = None,
    executor: str = EXECUTOR_RECOVERY,
    force: bool = False,
) -> dict:
    """Bounded recovery. No-op when the dedicated worker is healthy or the batch is busy."""
    from .tenant import resolve_import_access

    batch = db.session.get(ImportBatch, batch_id)
    if batch is None:
        raise ValueError("Import batch was not found.")
    if user is not None:
        resolve_import_access(user, batch)
    if batch.status == ImportBatchStatus.COMPLETED:
        return _recovery_payload(batch, recovered=False, reason="already_complete")
    if batch.status == ImportBatchStatus.FAILED:
        return _recovery_payload(batch, recovered=False, reason="failed")
    if batch.status != ImportBatchStatus.PROCESSING:
        return _recovery_payload(batch, recovered=False, reason="not_processing")
    if not force and is_import_worker_healthy():
        logger.info(
            "skip recovery batch_id=%s reason=worker_healthy heartbeat_age=%s",
            batch.id,
            worker_health()["heartbeat_age_seconds"],
        )
        return _recovery_payload(batch, recovered=False, reason="worker_healthy")
    if not force and not is_batch_recovery_eligible(batch):
        return _recovery_payload(batch, recovered=False, reason="not_stale")
    before_units = int(batch.units_created or 0)
    before_orders = int(batch.orders_created or 0)
    health = worker_health()
    logger.info(
        "recover batch_id=%s type=%s executor=%s heartbeat_age=%s",
        batch.id,
        batch.type,
        executor,
        health["heartbeat_age_seconds"],
    )
    result = dispatch_import_batch(
        batch.id, max_chunks=max_chunks, chunk_size=chunk_size, executor=executor
    )
    if result is None:
        batch = db.session.get(ImportBatch, batch_id)
        return _recovery_payload(batch, recovered=False, reason="missing")
    advanced = (
        result.status != ImportBatchStatus.PROCESSING
        or int(result.units_created or 0) != before_units
        or int(result.orders_created or 0) != before_orders
    )
    return _recovery_payload(
        result,
        recovered=advanced,
        reason="advanced" if advanced else "locked_or_unchanged",
    )


def recover_stale_imports(
    *,
    max_chunks: int | None = None,
    executor: str = EXECUTOR_CLI,
    limit: int = 25,
) -> list[dict]:
    """Emergency/CLI: process stale or orphaned PROCESSING batches."""
    inspect_orphaned_import_batches()
    results = []
    rows = (
        ImportBatch.query.filter_by(status=ImportBatchStatus.PROCESSING)
        .order_by(ImportBatch.started_at.asc().nullsfirst(), ImportBatch.id.asc())
        .limit(limit)
        .all()
    )
    for batch in rows:
        if is_import_worker_healthy() and not is_batch_recovery_eligible(batch):
            results.append(_recovery_payload(batch, recovered=False, reason="worker_owns"))
            continue
        current = batch
        cycles = 0
        while current is not None and current.status == ImportBatchStatus.PROCESSING and cycles < 500:
            payload = recover_import_batch(
                current.id,
                max_chunks=max_chunks or RECOVERY_MAX_CHUNKS,
                executor=executor,
                force=not is_import_worker_healthy(),
            )
            results.append(payload)
            if not payload.get("recovered"):
                break
            current = db.session.get(ImportBatch, batch.id)
            cycles += 1
    return results


def inspect_orphaned_import_batches() -> list[int]:
    """Startup/CLI inspect. Does not process work or change PROCESSING to FAILED."""
    orphaned: list[int] = []
    worker_down = not is_import_worker_healthy()
    rows = ImportBatch.query.filter_by(status=ImportBatchStatus.PROCESSING).all()
    for batch in rows:
        if batch.last_progress_at is None:
            batch.last_progress_at = batch.started_at or batch.created_at
        if worker_down or is_batch_recovery_eligible(batch):
            orphaned.append(batch.id)
            logger.info(
                "orphan PROCESSING batch_id=%s type=%s progress=%s last_progress_at=%s",
                batch.id,
                batch.type,
                batch.progress_percent,
                batch.last_progress_at,
            )
    if rows:
        db.session.commit()
    if orphaned:
        logger.info("orphaned_processing count=%s worker_healthy=%s", len(orphaned), not worker_down)
    return orphaned


def _recovery_payload(batch: ImportBatch | None, *, recovered: bool, reason: str) -> dict:
    if batch is None:
        return {"recovered": False, "reason": reason, "batch_id": None}
    from .inventory_import import batch_progress as inventory_progress
    from .order_import import batch_progress as order_progress

    progress = (
        order_progress(batch) if batch.type == ImportType.ORDERS else inventory_progress(batch)
    )
    progress["recovered"] = recovered
    progress["reason"] = reason
    progress["executor_used"] = batch.last_executor
    return progress


def clear_import_worker_heartbeats() -> None:
    """Test helper: pretend the dedicated worker is absent."""
    db.session.execute(text("DELETE FROM worker_heartbeats WHERE worker_name = :name"), {"name": IMPORT_WORKER_NAME})
    db.session.commit()
    deactivate_worker_instance()
