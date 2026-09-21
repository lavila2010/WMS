"""Operational orders are visible only after their import batch is COMPLETED.

Manually created orders (import_batch_id IS NULL) remain visible.
PROCESSING and FAILED batch orders exist for resume/cleanup but cannot be
allocated, listed as operational, or used in pick/processing/KPI/reports.
"""

from __future__ import annotations

from sqlalchemy import or_

from ..constants import ImportBatchStatus
from ..extensions import db
from ..models import ImportBatch, Order


def operational_order_batch_clause():
    return or_(
        Order.import_batch_id.is_(None),
        ImportBatch.status == ImportBatchStatus.COMPLETED,
    )


def apply_operational_order_visibility(query):
    """Outer-join ImportBatch and hide orders from incomplete/failed imports."""
    return query.outerjoin(ImportBatch, Order.import_batch_id == ImportBatch.id).filter(
        operational_order_batch_clause()
    )


def order_is_operational(order: Order) -> bool:
    if order is None:
        return False
    if order.import_batch_id is None:
        return True
    batch = db.session.get(ImportBatch, order.import_batch_id)
    return batch is not None and batch.status == ImportBatchStatus.COMPLETED
