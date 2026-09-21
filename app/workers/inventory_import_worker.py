"""Compatibility alias for the unified V2 import worker.

Render service name remains ``wms-v2-import-worker``. Prefer:

    python -m app.workers.import_worker

This module keeps the previous command working so inventory jobs and
existing tests continue without a second architecture.
"""

from app.workers.import_worker import (  # noqa: F401
    main,
    process_due_batches,
    processing_import_batch_rows,
    run_forever,
)

if __name__ == "__main__":
    main()
