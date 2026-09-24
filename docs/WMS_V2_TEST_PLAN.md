# WMS V2 Test Plan

Regression rule: Phase N runs the union of tests from Phases 1..N.

Harness: `pytest` against `TEST_DATABASE_URL` (default `postgresql://wms:wms@127.0.0.1:5432/wms_test`). Each test function gets a clean schema (`drop_all` / `create_all` + seed permissions).

Pass criterion for a phase: all required cases PASS, zero P0/P1 defects, integrity queries green, evidence in the phase report.

---

## Phase 1 — Foundation

| ID | Case |
|---|---|
| P1-01 | Concurrent client creates get distinct monotonic `sequence_number` / codes |
| P1-02 | `client_code` unique; initials uppercased; code immutable after insert |
| P1-03 | Division belongs to selected client; code `01-CEL-ECOM`; cannot attach to another client |
| P1-04 | Warehouse UNIQUE(client_id, symbol); `01-CEL-NY` vs `02-DIO-NY` both valid |
| P1-05 | Mapping rejects cross-client division/warehouse |
| P1-06 | USER without `user_clients` cannot read/write any client |
| P1-07 | USER with client A cannot GET/POST client B ids (IDOR → 404) |
| P1-08 | ADMIN sees all clients |
| P1-09 | Permission catalog seeded; USER missing CLIENTS_CREATE cannot create |
| P1-10 | Admin CRUD: client, division, warehouse, mapping, user, client access |
| P1-11 | Production config rejects default SECRET_KEY when `ENV=production` |
| P1-12 | `/health` returns JSON without secrets |

## Phase 2 — Inventory

| ID | Case |
|---|---|
| P2-01 | Qty 5 → 5 AVAILABLE units |
| P2-02 | Blank UPC blocks entire file (0 rows) |
| P2-03 | Atomic import: injected failure after N units → 0 committed |
| P2-04 | File Client/Warehouse mismatch vs context → reject |
| P2-05 | Same UPC two clients never aggregates together |
| P2-06 | Same UPC two warehouses of one client stay separate |
| P2-07 | ON_HAND = AVAILABLE+RESERVED+PACKED |
| P2-08 | Every insert has IMPORT ledger; no silent status update helper without ledger |
| P2-09 | No unit with warehouse.client_id ≠ unit.client_id |

## Phase 3 — Orders

| ID | Case |
|---|---|
| P3-01 | Three UPC rows same destination → 1 WMS order `01-CEL-1251-01`, 3 lines |
| P3-02 | Same raw Order Number + different Customer/Address → split WMS orders, not reject |
| P3-03 | Division not belonging to Client → reject |
| P3-04 | Warehouse not mapped to Division → reject |
| P3-05 | Missing UPC → reject |
| P3-06 | Duplicate same destination / WMS order ID for same client → reject |
| P3-07 | Same order number two clients → `01-CEL-1251-01`, `02-DIO-1251-01` |
| P3-08 | Injected failure leaves FAILED batch non-operational |
| P3-09 | `wms_order_id = {client_code}-{client_order_number}-{seq:02d}` unique |

## Phase 4 — Allocation

| ID | Case |
|---|---|
| P4-01 | Only same client units reserved |
| P4-02 | Only same warehouse |
| P4-03 | Only same UPC (SKU different, UPC match still allocates) |
| P4-04 | Never match on SKU when UPC differs |
| P4-05 | Partial / full / zero inventory statuses |
| P4-06 | Two threads cannot reserve the same unit |
| P4-07 | RESERVE ledger + qty_allocated in one commit |
| P4-08 | Exception mid-batch rolls back reservations |

## Phase 5 — Pick tickets

| ID | Case |
|---|---|
| P5-01 | Ticket only when fully ALLOCATED |
| P5-02 | Number stable on reprint |
| P5-03 | Sequence `01`; format `{code}-{orderno}-01` |
| P5-04 | Other client cannot open ticket id |
| P5-05 | Lines show allocated location + UPC + qty |
| P5-06 | Print event + print_count |

## Phase 6 — Processing

| ID | Case |
|---|---|
| P6-01 | One scan packs one unit |
| P6-02 | Excess scan blocked |
| P6-03 | Wrong UPC / client / warehouse / order blocked |
| P6-04 | Two users, one lock winner; loser message exact |
| P6-05 | Simulated refresh keeps lock |
| P6-06 | Owner cancel releases; other user cannot |
| P6-07 | Recall UNPACK + reweigh_required |
| P6-08 | Close atomic SHIP + CLOSED + lock cleared + PDF record |
| P6-09 | Close blocked when reweigh_required or remaining > 0 |

## Phase 7 — Reports / documents

| ID | Case |
|---|---|
| P7-01 | Closure PDF totals match units |
| P7-02 | Completed-orders export scoped to user clients |
| P7-03 | Carton dims/weights/UPCs correct |
| P7-04 | Closed-by user recorded |
| P7-05 | Reprint permission + audit |
| P7-06 | Store abstraction used (no raw ephemeral path as SoR) |

## Phase 8 — KPI / dashboard

| ID | Case |
|---|---|
| P8-01 | Default range is operational today in `America/New_York` (tests may pin UTC) |
| P8-02 | Group Client+Division |
| P8-03 | Channel buckets from operation_type |
| P8-04 | COUNT DISTINCT orders; SUM line qtys without duplication |
| P8-05 | Close: Open−1, Processed+1, Total unchanged |
| P8-06 | Non-admin cannot see other clients |

## Phase 9 — Security / concurrency

Anonymous 401/302; IDOR; permission escalation; POST without perm; concurrent allocate; concurrent lock; duplicate scan; double close; CSRF/session cookie flags; password hashed; no secret in `/health` or logs.

Every HIGH/CRITICAL must be fixed before GATE 9 PASS.

## Phase 10 — Reconciliation dataset

≥3 clients; shared symbol NY; ≥3 divisions each; multi warehouse; same UPC across clients and locations; same client_order_number across clients; full/partial/zero/multi-line/multi-carton.

Prove ordered=allocated=packed=shipped on closed orders.  
Prove inventory identity: imported ± adjustments ± transfers − shipped = on_hand.  
No unit on two orders/cartons. No cross-client/warehouse contamination.

## Phase 11 — UAT

`docs/UAT_CHECKLIST.md` + bootstrap users via CLI (passwords from env/prompt, never hardcoded).

## Phases 12–14

Deployed smoke, `PLATFORM_CERTIFIED` only with evidence, cutover docs without destroying V1.

---

## Integrity SQL (run after mutating tests)

```sql
-- I1 on-hand
-- I2 one active allocation per unit
-- I3 one carton content per unit
-- I4 order masters same client
-- I5 mappings same client
-- I6 ledger coverage for non-import-created updates
```

These are asserted in Python helpers `assert_invariants(client_id=None)`.
