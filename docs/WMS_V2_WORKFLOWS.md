# WMS V2 Workflows

All mutations fail closed. Tenant and permission checks run on the server before any write.

---

## 1. Client create

1. ADMIN/CLIENTS_CREATE submits Name + Initials.  
2. Server uppercases initials (`CEL`). Rejects blank/non-alpha initials.  
3. `sequence_number = nextval('client_code_seq')`.  
4. `client_code = lpad(sequence_number, 2, '0') || '-' || initials`.  
5. Insert `clients`. On unique conflict, abort (do not invent a new code in the browser).  
6. Audit `CLIENT_CREATED`.  
7. `client_code` cannot be updated afterward.

---

## 2. Division create

1. Select Client (must be in `user_clients` unless ADMIN).  
2. Name + `operation_type` in {ECOM, RTL, WHLS}.  
3. `code = client.client_code || '-' || operation_type`.  
4. Reject if that code or (`client_id`, `operation_type`) exists.  
5. Audit `DIVISION_CREATED`.

---

## 3. Warehouse create

1. Select Client.  
2. User enters symbol `NY` (uppercase).  
3. `warehouse_code = client.client_code || '-' || symbol`.  
4. UNIQUE(`client_id`, `warehouse_symbol`) allows `02-DIO-NY`.  
5. Audit `WAREHOUSE_CREATED`.

---

## 4. Division ↔ Warehouse map

1. Select Client, then Division and Warehouse belonging to that Client.  
2. If `division.client_id != warehouse.client_id`, reject.  
3. Insert `division_warehouses` with `client_id`.  
4. Trigger rejects cross-client rows.

---

## 5. Inventory import

1. User selects Client + Warehouse (both active; warehouse belongs to client; user may access client).  
2. Upload Excel: UPC, SKU, Description, Style, Color, Size, Quantity, Location.  
3. Optional Client/Warehouse columns: must match selected context or the file is rejected.  
4. Preview parses once into `inventory_import_rows`. Quantity must be integer > 0. UPC, Description, Style, Color, Size, and Location required. SKU may be blank. Description is copied onto every physical unit created from Quantity. Preview JSON stores only batch identity, counts, and the first 100 rows.  
5. Confirm authorizes, marks the batch `PROCESSING`, and returns immediately. It does not start a thread. The browser polls status only.  
6. Render worker `wms-v2-import-worker` (`python -m app.workers.inventory_import_worker`) claims `PROCESSING` batches from PostgreSQL, bulk-inserts units and IMPORT ledger rows in 2,000-unit chunks, and resumes from `units_created` after restart. Advisory locks prevent two workers from mutating the same batch. `POST /inventory/imports/<id>/advance` is admin-only recovery. `flask process-inventory-import --batch-id` is the operator fallback.  
7. Units stay invisible to allocation and operational availability until `ImportBatch.status = COMPLETED`. A `FAILED` batch is also invisible; Retry resumes remaining chunks; Cleanup deletes the batch's units and ledger rows.  
8. Audit `INVENTORY_IMPORT` when the batch completes.

---

## 6. Order import

1. User selects Client + Division (division belongs to client; user may access client).  
2. Excel: Warehouse, Order Number, Customer, CustomerAddress, UPC, QTY required. CustomerPhone, Carrier, Shipping Service, SKU, Description optional and may be blank.  
3. Warehouse symbol/code must belong to Client, be active, and be mapped to the Division. Warehouses are preloaded once per file.  
4. Rows group into fulfillment orders by Client + Division + Warehouse + raw Order Number + Customer + Customer Address. Header consistency is required only inside that group. Different customers under one raw Order Number become separate WMS orders.  
5. Destination sequence is deterministic: for each Client + raw Order Number, sort groups by warehouse_code, customer, customer_address and assign 01, 02, 03…. `wms_order_id = {client_code}-{client_order_number}-{seq:02d}`.  
6. UPC is stored as text (leading zeros preserved). Same fulfillment group + UPC aggregates into one OrderLine. Qty integer > 0. Blank Description may inherit the unique Client+UPC inventory description (bulk lookup).  
7. Duplicate existing WMS order ID / destination identity rejects that destination. Confirm marks `PROCESSING` and returns. The durable worker bulk-inserts headers (`INSERT … RETURNING`) then lines. Operational visibility requires `ImportBatch.status = COMPLETED`.  
8. Audit `ORDER_IMPORT` when the batch completes. Pick ticket number is `{wms_order_id}-01`.

Example: raw Order Number 24 with three customers → `01-CEL-24-01`, `01-CEL-24-02`, `01-CEL-24-03`, all with `client_order_number = 24`.

---

## 7. Allocation

1. Eligible: order `UNALLOCATED` or `PARTIALLY_ALLOCATED` (re-run allowed for remaining demand).  
2. Audit `ALLOCATION_STARTED`.  
3. For each line, `need = qty_ordered - qty_allocated`.  
4. Select AVAILABLE units where `client_id`, `warehouse_id`, `upc` match, `ORDER BY location ASC, id ASC`, `FOR UPDATE SKIP LOCKED`, `LIMIT need`.  
5. For each unit, in the same transaction: unit `AVAILABLE→RESERVED`, `allocated_order_id`/`allocation_id` set, `allocations` ACTIVE row, ledger `RESERVE`, `qty_allocated += 1`.  
6. Status: all lines full → `ALLOCATED`; some → `PARTIALLY_ALLOCATED`; none → remain `UNALLOCATED`.  
7. Audit `ALLOCATION_COMPLETED` or `ALLOCATION_PARTIAL`.  
8. Release: ACTIVE→RELEASED, unit `RESERVED→AVAILABLE`, ledger `UNRESERVE`, decrement `qty_allocated`. Never SKU match. Never other client/warehouse.

Two concurrent allocators cannot reserve the same `inventory_units.id`.

---

## 8. Pick ticket

1. Created only when order is `ALLOCATED` (full reservation).  
2. One ticket per WMS fulfillment order. Number `{wms_order_id}-01`.  
3. Order → `PICK_TICKET_READY`. Audit `PICK_TICKET_CREATED`.  
4. Reprint increments `print_count`, inserts `pick_ticket_print_events`, updates last printed; number unchanged. Audit `PICK_TICKET_PRINTED`.  
5. Lines resolved from ACTIVE allocations: Location, UPC, SKU, Description, Qty (count).

---

## 9. Processing

1. Operator enters pick ticket number. Resolve ticket → client, division, warehouse, order, customer, carrier, reserved UPCs. Do not ask for Client.  
2. Tenant + permission check. If another user holds the lock: *Order is currently being processed by another user.*  
3. Atomic lock acquire. Status → `PROCESSING`. Audit `PROCESSING_STARTED`.  
4. Create carton `{client_order_number}-BOX01` if none. Dimensions required before scan.  
5. One UPC scan = one RESERVED unit for this order/client/warehouse/UPC (`ORDER BY location, id`, `FOR UPDATE`). Unit → `PACKED`, carton link, ledger `PACK`, `qty_packed += 1`. Excess scan fails.  
6. Close carton requires weight. Recall allowed by permission; content change sets `reweigh_required`, stores previous weight, audit invalidate/reweigh events. Remove unit: `PACKED→RESERVED`, `UNPACK`.  
7. Close order: ordered = allocated = packed; remaining 0; all cartons closed; dimensions and current weights present; no reweigh_required. Confirm modal. One transaction: units `PACKED→SHIPPED`, ledger `SHIP`, `qty_shipped` set, order `CLOSED`, `closed_at`, lock released, invoice + document record, audits `ORDER_RECONCILED` / `ORDER_CLOSED` / `PDF_GENERATED`.  
8. UI returns to pick-ticket input.

---

## 10. KPI Orders

- Default From/To = operational current day in `America/New_York` (`WMS_TIMEZONE`).  
- Group by Client + Division.  
- Total Orders = distinct orders created in range (does not increase on close).  
- Open = not CLOSED/CANCELLED; Processed = CLOSED. Close moves Open → Processed.  
- Units from `SUM(order_lines.qty_ordered)` (and packed/shipped analogs) — never `JOIN` that duplicates order counts.  
- Channel from `division.operation_type`.  
- Client filter required unless ADMIN.

---

## 11. Order status machine

```
UNALLOCATED
    → PARTIALLY_ALLOCATED (some reserved)
    → ALLOCATED (all reserved)
        → PICK_TICKET_READY
            → PROCESSING
                → CLOSED
UNALLOCATED / PARTIALLY_ALLOCATED / ALLOCATED / PICK_TICKET_READY
    → CANCELLED (authorized; release reservations)
CLOSED and CANCELLED are terminal.
```

No V1 names (`NEW`, `VALIDATED`, `ALLOCATING`, `READY_TO_PICK`, `PROCESSED`, `READY_TO_CLOSE`).

---

## 12. Inventory unit lifecycle

```
IMPORT → AVAILABLE
AVAILABLE → RESERVED (RESERVE)
RESERVED → AVAILABLE (UNRESERVE)
RESERVED → PACKED (PACK)
PACKED → RESERVED (UNPACK)
PACKED → SHIPPED (SHIP)
SHIPPED → AVAILABLE (RETURN) when return workflow is used
```

Each arrow = one DB transaction including the ledger row.

---

## 13. Fail-closed examples

| Situation | Result |
|---|---|
| URL order id for another client | 404 |
| Spreadsheet client ≠ selected context | reject file |
| Warehouse not mapped to division | reject file |
| Scan UPC not reserved on this order | reject |
| Fourth scan after three reserved | reject |
| Second user confirm processing | lock message |
| Close with reweigh_required | reject |
| Allocation finds no AVAILABLE | line remains short; no other warehouse |
