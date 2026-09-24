# WMS V2 import execution failsafe

Status: implemented on `cursor/wms-import-failsafe-6b85`. Do not deploy from this branch.

## Root cause

Confirm only CAS-marks `import_batches.status = PROCESSING`. The durable worker is the primary executor. If that Render process is absent, batches stay at 0% even though `flask process-*-import` can finish them. The engine works; execution availability was missing.

## Permanent model

1. Dedicated worker `python -m app.workers.import_worker` remains primary.
2. Worker writes `worker_heartbeats` every loop / chunk (`last_seen_at` fresh ≤ 20s = Online).
3. GET `/inventory|orders/imports/<id>/status` never mutates. It reports worker health and recovery eligibility.
4. When the worker is Offline, the upload UI automatically POSTs CSRF-protected `/imports/<id>/recover` (bounded chunks, advisory lock).
5. `flask --app wsgi recover-imports` is emergency/manual only.

## Eligibility

A PROCESSING batch is recovery-eligible when the import worker is unhealthy and either no units/orders have been created yet, or `last_progress_at` is older than 30 seconds.
