# WMS V2 production order import

Status: implemented on `cursor/wms-v2-rebuild-6b85`. Do not treat this as a live deploy.

## Root cause

The first V2 order importer treated raw client Order Number as unique per Client:

- grouped every row solely by Order Number
- rejected files when Customer differed under the same number
- enforced `UNIQUE(client_id, client_order_number)`
- derived `wms_order_id = {client_code}-{client_order_number}`
- issued warehouse and description queries per row and flushed per order

That model is invalid for CELINE wholesale data. One raw Order Number can have multiple fulfillment destinations.

## Destination grouping rule

A physical WMS fulfillment order is:

Client + Division + Warehouse + Client Order Number + Customer + Customer Address

CustomerPhone, Carrier, and Shipping Service are optional attributes and may be blank.

Header consistency is required only inside that group.

## Destination sequence

For each Client + raw Order Number, sort unique fulfillment destinations by:

1. `warehouse_code` ASC
2. Customer normalized ASC (casefold, collapsed whitespace)
3. Customer Address normalized ASC (casefold, collapsed whitespace)

then assign `01`, `02`, `03`….

Normalization is identity-only: `Ada  Smith` and `ada smith` are the same destination.

`wms_order_id = {client_code}-{client_order_number}-{destination_sequence:02d}`

Example: raw `24` with three destinations becomes `01-CEL-24-01`, `01-CEL-24-02`, `01-CEL-24-03`. All three keep `client_order_number = 24`.

CustomerPhone, Carrier, and ShippingService may be blank. Two nonblank conflicting values inside the same fulfillment group are a HEADER reject. Different Customer/Address under one raw Order Number is not a header error — it creates additional WMS orders.

Pick ticket: `{wms_order_id}-01` → `01-CEL-24-02-01`.

Re-upload of the same destinations produces the same IDs and is rejected.

## Import architecture

1. Parse Excel once (`dtype=str`).
2. Persist source rows in `order_import_rows`.
3. Preload authorized warehouses, existing WMS IDs / destination keys, and bulk UPC descriptions.
4. Compact preview (counts + first 100 WMS orders). Preview JSON is not the source of truth.
5. Confirm CAS `VALIDATED → PROCESSING` and returns. No Gunicorn thread.
6. `python -m app.workers.import_worker` claims PROCESSING inventory **and** order batches (`inventory_import_worker` remains a compatibility alias).
7. Bulk insert order headers with `INSERT … RETURNING`, then order lines in bounded chunks.
8. Orders are allocatable / visible only when `ImportBatch.status = COMPLETED`.

## Background execution

Same durable PostgreSQL queue as inventory. Advisory lock class for orders is `87421002`. Resume skips `wms_order_id` values already created for the batch.

## Certification (local, not deployed)

- CELINE-equivalent: 1,075 source rows, 31 raw order numbers, **36 WMS orders**, 1,074 lines, 1,268 units, 472 UPCs
- Order 19 → 2 WMS orders; Order 24 → 3; Order 25 → 3
- Capacity: 100,000 source rows, 5,000 WMS orders, 100,000 lines, 250,000 units in 27.62s (analyze 20.502s, process 7.118s, 60 SQL statements)
- Inventory durable worker, CELINE-scale, and 150k-unit capacity remain PASS
- Full V2 suite: 143 passed / 0 failed
- Status: `DURABLE_ORDER_IMPORT_READY`
- Do not deploy from this branch.
