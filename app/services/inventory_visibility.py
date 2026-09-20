"""Operational inventory is visible only after its import batch is COMPLETED.

Units created outside an import (import_batch_id IS NULL) remain visible.
PROCESSING and FAILED batch units exist for resume/cleanup but cannot be
allocated, counted as available, or used in pick/processing availability.
"""

from __future__ import annotations

from sqlalchemy import or_

from ..constants import ImportBatchStatus
from ..models import ImportBatch, InventoryUnit


def operational_batch_clause():
    return or_(
        InventoryUnit.import_batch_id.is_(None),
        ImportBatch.status == ImportBatchStatus.COMPLETED,
    )


def apply_operational_visibility(query):
    """Outer-join ImportBatch and hide units from incomplete/failed imports."""
    return query.outerjoin(
        ImportBatch, InventoryUnit.import_batch_id == ImportBatch.id
    ).filter(operational_batch_clause())
