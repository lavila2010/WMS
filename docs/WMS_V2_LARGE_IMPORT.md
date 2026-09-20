# WMS V2 production inventory import

Status: implemented on `cursor/wms-v2-rebuild-6b85`. Do not treat this as a live deploy.

## Root cause of the CELINE-scale failure

The first V2 importer:

- read the whole workbook into memory
- serialized every validated row into preview JSON
- expanded `Quantity` with Python loops
- called `create_available_unit()` + `record_transaction()` once per physical unit
- flushed the SQLAlchemy session twice per unit

An 18,436-row / 50,336-unit file therefore issued 100,000+ statements inside one browser request.

## New architecture

1. Parse Excel once (`dtype=str`).
2. Persist source rows in `inventory_import_rows`.
3. Store a compact preview only (batch id, counts, first 100 rows, capped errors).
4. Confirm marks the batch `PROCESSING` and returns.
5. A Gunicorn worker thread bulk-inserts `inventory_units` with `INSERT … RETURNING`, then bulk-inserts matching `IMPORT` ledger rows.
6. Allocation and operational availability require `InventoryUnit.status = AVAILABLE` **and** (`import_batch_id IS NULL` or `ImportBatch.status = COMPLETED`).

Unit architecture is unchanged: Quantity 5,105 creates 5,105 unit rows and 5,105 IMPORT transactions.

## Background execution on Render

There is **no dedicated Render worker** on the current blueprint (`wms-v2` is a web service only).

Confirm Import:

1. Authorizes Client/Warehouse.
2. CAS `VALIDATED → PROCESSING`.
3. Starts a daemon thread in the handling Gunicorn worker.
4. Returns the processing page immediately.

The job is durable in PostgreSQL (`units_created`, `current_source_row`, advisory lock `87421001, batch_id`). Closing the browser does not stop the thread. If the worker is recycled, the processing page polls every 1.5s and `POST /inventory/imports/<id>/advance` resumes remaining chunks. Operators can also run:

```text
flask process-inventory-import --batch-id <id>
```

on the same web service (Render one-off / shell). That is the supported fallback until a worker service is provisioned.

`MAX_UPLOAD_MB=50` is configured for future files. It is **not** the fix for this performance issue.

## Chunk sizes

| Path | Size | Why |
|---|---|---|
| Staging insert | 2,000 source rows | keeps parameter batches modest |
| Unit insert | 2,000 physical units | inside the 1,000–5,000 target; one `INSERT … RETURNING` |
| Ledger insert | 2,000 IMPORT rows | matches the unit chunk so the invariant can be checked per commit |

A source row with Quantity 5,105 is split 2,000 / 2,000 / 1,105.

## Idempotency

- Confirm CAS from `VALIDATED` only.
- A second Confirm on `PROCESSING` or `COMPLETED` is a no-op.
- Resume skips already-created units using `units_created` against staged quantities.
- Retry is `FAILED → PROCESSING` and continues; it does not re-insert completed chunks.
- Cleanup deletes only that batch's units and ledger rows.

## Allocation visibility

Operational queries join `import_batches` and hide `PROCESSING` / `FAILED` / unvalidated batches. Administrative import-progress views use batch counters, not operational availability.

## Indexes (final)

Added, matching query paths and not the bulk-write hot path beyond `import_batch_id`:

- `inventory_units (client_id, warehouse_id, upc, status)`
- `inventory_units (import_batch_id)`
- `inventory_units (client_id, warehouse_id, location)`
- `inventory_transactions (import_batch_id)`
- `inventory_transactions (client_id, warehouse_id, upc)`
- `inventory_import_rows (import_batch_id)`
- `inventory_import_rows (import_batch_id, validation_status)`
- `import_batches (status)`
- `import_batches (client_id, warehouse_id, created_at)`

Kept existing `(client_id, warehouse_id, upc)` and `(client_id, warehouse_id, upc, location, status)`.
