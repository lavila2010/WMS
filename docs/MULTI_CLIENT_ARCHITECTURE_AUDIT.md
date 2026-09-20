# WMS Multi-Client Core Architecture Audit

**Status:** PHASE 1 — ANALYSIS ONLY. No architecture was implemented.  
**Inspection date:** 2026-09-20 (UTC)  
**Branch inspected:** `cursor/wms-nextjs-prisma-env-6b85`  
**HEAD at inspection:** `bc31efcc45575a9b88f9e60a6e0253abf4c60d74`  
**Live database:** local PostgreSQL 16.15, database `wms` (read-only SELECT)  
**Companion schema inventory:** `docs/DATABASE_INVENTORY.md`  
**Production posture:** Flask + Jinja + Flask-SQLAlchemy on Render, Aiven PostgreSQL. Do not drop tables, delete data, reset the database, or merge this branch.

This document compares the **deployed Flask WMS** against the proposed multi-client + UPC quantity architecture. It is an audit, not a migration.

Classification key:

| Class | Meaning |
|---|---|
| **PASS** | Existing table/behavior already satisfies the requirement |
| **PARTIAL** | Foundation exists but fields, constraints, or enforcement are incomplete |
| **MISSING** | Required table, field, or behavior does not exist |
| **CONFLICT** | Existing design actively contradicts the proposed architecture and needs a reviewable migration, not a silent rename |

---

## Current architecture summary

The live WMS is a **physical unit (barcode) system** with **Client + Warehouse** inventory isolation and **SKU-based order demand**.

- Tenant masters: `clients`, `warehouses` (`UNIQUE(client_id, code)`), `divisions` (`UNIQUE(client_id, code)`), `order_types` (`UNIQUE(client_id, code)` + `channel`).
- Live client codes are free-text names (`CELINE`, `DIOR`), not sequenced codes (`01-CEL`).
- Inventory is one `inventory_units` row per unique barcode. UPC/SKU/location are attributes of that unit. Quantity at a location is `COUNT(*)` of units in status `AVAILABLE` / `ALLOCATED` / `PACKED`.
- Order identity is `UNIQUE(client_id, warehouse_id, order_type_id, order_number)`. Lines store `sku` + `quantity` only — no UPC.
- Auto-allocation matches `order_lines.sku` to `inventory_units.sku` inside the order's Client + Warehouse. Processing scans **UPC** and consumes one remaining allocated unit.
- Pick tickets are permanent (`UNIQUE(order_id)`) numbered `PT-{year}-{seq:06d}` (live: `PT-2026-000001`). Reprints are `pick_ticket_print_events`.
- Exclusive processing lock is an atomic `UPDATE orders … WHERE processing_user_id IS NULL OR processing_user_id = :uid`.
- RBAC is global (`users` / `permissions` / `user_permissions`). Users are not bound to a client. Detail routes use `get_or_404(id)` without a user-to-client check.
- Import services call `get_or_create_client` / `get_or_create_warehouse` / `get_or_create_order_type` from spreadsheet codes.
- Ledgers: `inventory_movements` (unit status/location) and `transactions` (ops events). There is no quantity `inventory_balances` table and no `inventory_transactions` qty ledger.
- PostgreSQL row-level security is **off** on every public table.
- Leftover unused Prisma tables remain: `Item`, `Location`, `_prisma_migrations`.

---

## Requirement classification matrix

| # | Requirement | Class | Evidence |
|---|---|---|---|
| 1 | Client is root tenant; no cross-client inventory; deterministic `client_id` path | **PARTIAL** | `orders`, `inventory_units`, `invoices`, `inventory_movements` have `client_id`. `allocations`, `pick_tickets`, `boxes`, `box_contents`, `order_lines`, `documents`, `transactions`, `audit_events` do not. Path exists only via joins. |
| 1b | Tenant isolation enforced server-side and at DB, not only UI | **CONFLICT** | Allocation/processing check `unit.client_id == order.client_id`. Lists filter by dropdown. Detail routes do not bind the user to a client. No RLS. `get_or_create_*` can create a new client from a spreadsheet. |
| 2 | Client master: atomic sequence, `01-CEL`, initials, immutable code, ACTIVE/INACTIVE | **PARTIAL** | `clients(id, code unique, name, active, created_at)` exists. Missing `sequence_number`, `initials`, `client_code` format, `created_by` / `updated_*`. Live codes: `CELINE`, `DIOR`. No admin CRUD. Code is mutable. |
| 3 | Divisions belong to exactly one Client; platform-unique `division_code` | **PARTIAL** | `divisions.client_id` NOT NULL FK. Unique is `(client_id, code)`, **not** platform-wide. No `operation_type`. Live codes: `MAIN`, `FASHION`, `BEAUTY` (not `01-CEL-ECOM`). Import auto-creates `MAIN`. |
| 4 | Warehouses Client-specific; `UNIQUE(client_id, warehouse_symbol)`; code `01-CEL-NY` | **PARTIAL** | `UNIQUE(client_id, code)` already allows `CELINE/NY` and `DIOR/NY`. No `warehouse_symbol` vs `warehouse_code` split. Live codes are symbols (`NY`, `NJ`), not `01-CEL-NY`. |
| 5 | `division_warehouses` mapping; reject cross-client | **MISSING** | Table does not exist. Any warehouse of a client can be used with any division of that client. |
| 6 | UPC is merchandise key through the whole flow; SKU is descriptive | **CONFLICT** | UPC exists on units and is the **processing** scan key. Order lines and auto-allocation use **SKU**. SKU is the allocation key today. |
| 6b | Inventory scoped Client + Warehouse + UPC + Location | **PARTIAL** | Units are scoped Client + Warehouse + barcode. Same UPC already exists in multiple locations/clients. There is no unique/qty grain on `(client_id, warehouse_id, upc, location)`. |
| 7 | Inventory import: select Client+Warehouse; spreadsheet tenant validation-only; preview → validate → atomic commit | **PARTIAL** | Two-step preview/confirm and atomic commit exist. Tenant IDs come **from the spreadsheet**. UI Client/Warehouse dropdowns are **not** import context. `get_or_create_*` trusts file codes. Barcode is required. |
| 8 | `inventory_balances` qty model; AVAILABLE = ON_HAND − RESERVED − PACKED; no negatives | **CONFLICT** | No `inventory_balances` table. Qty is a count of `InventoryUnit` rows. Negatives are structurally impossible at unit grain, but the proposed qty ledger does not exist. |
| 9 | Immutable `inventory_transactions` qty ledger with listed types | **PARTIAL** | `inventory_movements` + `transactions` are append-only unit/status ledgers. Missing qty `before_state` / `after_state`, `inventory_balance_id`, carton/allocation/pick-ticket FKs, and types ADJUSTMENT / UNPACK / RETURN / TRANSFER_*. Remove-from-carton records `PACK`. |
| 10 | State flow IMPORT / ALLOCATE / SCAN / REMOVE / CLOSE; invariant AVAILABLE+RESERVED+PACKED = ON_HAND | **PARTIAL** | Unit statuses map: AVAILABLE → ALLOCATED → PACKED → SHIPPED. Derived counts satisfy the invariant if ON_HAND excludes SHIPPED. No qty columns to test as balances. |
| 11 | Order import context: Client + Division; warehouse in file must belong to Client and be authorized for Division | **CONFLICT** | Spreadsheet requires Client + Warehouse + OrderType. Division is auto-`MAIN`. No division-warehouse authorization. Lines keyed by SKU, not UPC. |
| 12 | Order import preview → validate → atomic commit; header consistency; UPC + qty > 0 | **PARTIAL** | Preview/confirm exists. Groups repeated `OrderNumber` into one header + many lines. Consistency checked for Customer / Carrier / Shipping Service only (not address/phone/warehouse). Unknown masters are **created**, not rejected. Duplicates are skipped, not rejected. |
| 13 | Normalized orders + order_lines with mandatory UPC and qty_* counters | **PARTIAL** | Header + lines exist. Lines have `sku` + `quantity` only. Missing `upc`, `qty_allocated`, `qty_packed`, `qty_shipped`, `customer_address`, `customer_phone`, `wms_order_id`, `created_by`. |
| 14 | WMS order id `01-CEL-1251`; `UNIQUE(client_id, client_order_number)` | **MISSING** | No `wms_order_id` / `client_order_number`. Uniqueness includes warehouse + order type, so the same client order number can exist twice for one client. |
| 15 | Statuses UNALLOCATED / PARTIALLY_ALLOCATED / ALLOCATED / PICK_TICKET_READY / PROCESSING / CLOSED / CANCELLED | **CONFLICT** | Live machine: `NEW → VALIDATED → ALLOCATING → ALLOCATED → READY_TO_PICK → PROCESSING → PROCESSED → READY_TO_CLOSE → CLOSED`. No `CANCELLED`. Partial allocation is a **result enum**, not a status. |
| 16 | Allocate Client → Warehouse → UPC → available; never SKU; never other tenant | **CONFLICT** | `_available_units` and `_pick_line_with_demand` filter **SKU**. Cross-client/warehouse rejected. Shortage stays `VALIDATED`/`ALLOCATING` with result `PARTIAL` / `NO INVENTORY`. |
| 17 | `allocations` identify inventory/location; reserve in same DB transaction | **PARTIAL** | `allocations` exist at barcode/unit grain (`inventory_unit_id`). Location is on the unit, not the allocation. Missing `client_id`, `warehouse_id`, `upc`, `location`, `qty_allocated`, `created_by`. Same-session flush with unit status change. No DB unique for one ACTIVE allocation per unit. |
| 18 | Pick ticket `<ClientCode>-<ClientOrderNumber>-<TicketSequence>`; permanent; reprints tracked | **CONFLICT** | Permanent one-per-order + print events: **PASS behavior**. Number scheme `PT-{year}-{seq:06d}` conflicts. Sequence is `MAX+1` in application code (race). Missing denormalized `client_id` / `warehouse_id` / allocated UPC-qty-location snapshot. |
| 19 | Processing starts from pick ticket; resolve tenant graph; one UPC scan = one reserved unit | **PARTIAL** | `lookup_pick_ticket` → order → client/warehouse/lines/allocations. Operator does not re-select Client. `scan_upc_into_box` consumes one remaining allocated unit and re-checks client/warehouse. Still unit-based, not a qty ledger. |
| 20 | Exclusive order lock; transaction-safe inventory mutations | **PARTIAL** | Processing lock is atomic and exclusive. Different orders may run concurrently. Auto-allocation has no `SELECT FOR UPDATE` / unique active-allocation index. Pick-ticket numbering is not atomic. |
| 21 | Server-side isolation / IDOR audit | **CONFLICT** | Any authenticated user with the module permission can open `/orders/<id>`, `/allocation/<id>`, `/processing/<id>`, `/inventory` unit pages, `/reports` documents by guessing a numeric id. Users are not client-scoped. |
| 22 | Admin: Clients, Divisions, Warehouses, mappings, Users, Permissions | **PARTIAL** | Users, permissions, audit exist. No Client / Division / Warehouse / mapping screens. |
| 23 | Audit trail with `user_id`, `client_id`, entity, event, timestamp | **PARTIAL** | `audit_events` has user, event, entity, timestamp, detail. **No `client_id`**. Inventory movements are a separate unit ledger. |

**Counts:** PASS 0 · PARTIAL 16 · MISSING 2 · CONFLICT 9  
(Rows 1b/6b counted with their parent requirement in the narrative; the table lists 23 numbered items plus two sub-rows.)

Overall Phase-1 verdict: **ARCHITECTURE_GAP**. The current system is a working multi-client **unit-barcode WMS**. The proposed system is a **quantity-UPC, coded-tenant, division-authorized** platform. They share Client + Warehouse isolation and several tables, but they are not the same inventory or identity model.

---

## A. Existing tables that already satisfy (or nearly satisfy) this architecture

Reusable as-is or with additive columns (no drop):

| Table | Why it survives |
|---|---|
| `clients` | Root tenant row. Needs coding/sequence columns. |
| `warehouses` | Already `UNIQUE(client_id, code)` — the proposed warehouse-symbol uniqueness. |
| `divisions` | Already client-owned. Needs code format + `operation_type` + optional platform-unique `division_code`. |
| `order_types` | Extra vs the proposal (channel). Keep; do not collapse into Division without a product decision. |
| `orders` | Header exists with `client_id`, `warehouse_id`, `division_id`, `closed_at`, processing lock. |
| `order_lines` | Normalized lines exist; merchandise key and qty counters must be added. |
| `allocations` | Traceability table exists; grain is unit/barcode, not qty/location. |
| `pick_tickets` | Permanent one-per-order identity + `pick_ticket_print_events`. |
| `boxes` / `box_contents` | Carton model (called Box, not Carton). Unit-grain contents. |
| `import_batches` | Import history. Needs `client_id` / `division_id` / `warehouse_id` context FKs. |
| `inventory_movements` | Unit movement history. Not the proposed qty ledger; keep during dual-write. |
| `transactions` | Ops event log. |
| `audit_events` | Human-action audit. Needs `client_id`. |
| `users` / `permissions` / `user_permissions` | Global RBAC. Needs optional client membership. |
| `invoices` / `documents` / `order_exceptions` / `inventory_exceptions` | Downstream artifacts; inherit client via order/unit. |

---

## B. Existing fields that can be reused

| Proposed field | Existing field | Notes |
|---|---|---|
| `clients.id` | `clients.id` | Internal PK. Keep. |
| `clients.name` | `clients.name` | Live: `Celine`, `Dior`. |
| `clients.active` | `clients.active` | Present. |
| `clients.created_at` | `clients.created_at` | Present. |
| `clients.client_code` | `clients.code` | **Reuse column** after a data migration. Live values `CELINE`/`DIOR` are not `01-CEL`. |
| `warehouses.warehouse_symbol` | `warehouses.code` | Live `NY`/`NJ` already repeat across clients. |
| `warehouses.warehouse_name` | `warehouses.name` | Present. |
| `divisions.division_name` | `divisions.name` | Present. |
| `divisions.division_code` | `divisions.code` | Format and uniqueness differ. |
| `orders.client_order_number` | `orders.order_number` | Reuse; tighten uniqueness later. |
| `orders.customer` | `orders.customer` | Present. |
| `orders.carrier` / `shipping_service` | same | Present. |
| `orders.status` | `orders.status` | Values **conflict** (see §15). |
| `orders.closed_at` | `orders.closed_at` | Present. |
| `order_lines.sku` | `order_lines.sku` | Keep as descriptive after UPC becomes mandatory. |
| `order_lines.qty_ordered` | `order_lines.quantity` | Rename or add generated alias; do not drop. |
| `allocations.order_id` / `order_line_id` | same | Present. |
| `allocations.inventory_unit_id` | same | Becomes optional/legacy if balances replace units. |
| `inventory_units.upc` / `sku` / `location` / `client_id` / `warehouse_id` | same | Source data for any balance rollup. |
| `inventory_units.status` | same | Maps AVAILABLE→available, ALLOCATED→reserved, PACKED→packed, SHIPPED→shipped. |
| Pick ticket permanence + print events | `pick_tickets` + `pick_ticket_print_events` | Keep; change number format only for **new** tickets. |
| Processing lock columns | `orders.processing_*` | Keep as-is. |
| `AuditEvent` user/event/entity/time | same | Add `client_id`. |

---

## C. Missing tables

| Table | Required by | Status |
|---|---|---|
| `inventory_balances` | §§8–10, 16 | **MISSING** |
| `inventory_transactions` (qty ledger) | §9 | **MISSING** (do not confuse with `transactions` or `inventory_movements`) |
| `division_warehouses` | §5 | **MISSING** |
| `client_sequences` or equivalent | §2 atomic `01`, `02` | **MISSING** |
| `user_clients` (recommended) | §§21–22 | **MISSING** — users are global |
| Proposed `cartons` | §6 flow | **Not needed** if `boxes` is kept as the carton table |

Leftover non-WMS tables (`Item`, `Location`, `_prisma_migrations`) are **not** candidates for reuse.

---

## D. Missing columns

### `clients`

Missing: `sequence_number`, `initials`, `created_by`, `updated_at`, `updated_by`.  
`code` exists but is not generated `NN-XXX` and is not frozen after first inventory/order.

### `divisions`

Missing: `operation_type`.  
`code` is not platform-unique and not generated from client code.

### `warehouses`

Missing: `warehouse_symbol` (if `code` is reserved for `01-CEL-NY`) or `warehouse_code` (if `code` stays the symbol). Need an explicit split.

### `orders`

Missing: `client_order_number` (or promote `order_number`), `wms_order_id`, `customer_address`, `customer_phone`, `created_by`.  
`division_id` is nullable.  
Uniqueness is 4-part, not `(client_id, order_number)`.

### `order_lines`

Missing: `upc` (mandatory), `sku` optionalization, `qty_allocated`, `qty_packed`, `qty_shipped`.

### `allocations`

Missing: `client_id`, `warehouse_id`, `upc`, `location`, `qty_allocated`, `created_by`, `inventory_balance_id`.  
No unique index for one ACTIVE allocation per unit.

### `pick_tickets`

Missing: `client_id`, `warehouse_id`, snapshot of allocated UPC/qty/location (today these are live-joined from units).

### `boxes` / `box_contents`

Missing: `client_id`. Contents are unit/barcode, not UPC qty.

### `audit_events` / `transactions` / `import_batches`

Missing: `client_id` (and import context `warehouse_id` / `division_id`).

### `inventory_movements`

Missing vs proposed ledger: `inventory_balance_id`, `quantity` signed, `before_state`, `after_state`, `order_line_id`, `allocation_id`, `pick_ticket_id`, `carton_id`, `reference`.  
`client_id` / `warehouse_id` are nullable.

### Inventory merchandise

Missing everywhere: `style`, `color`, `size` (proposed import fields).

---

## E. Incorrect relationships

1. **Order uniqueness includes warehouse + order type.** Proposed identity is Client + client order number only. Live allows `CELINE` + `SO-51021` once per warehouse/type combination.
2. **Division is an order attribute, not an operating authorization.** No `division_warehouses`. Import assigns `MAIN` regardless of warehouse.
3. **Division uniqueness is per-client**, not platform-wide. Two clients can both have `FASHION`.
4. **OrderType vs Division vs Channel.** Live: Division (`MAIN`/`FASHION`/`BEAUTY`) ≠ OrderType (`ECOM`/`RETAIL`/…) ≠ Channel (`ECOMMERCE`/`RETAIL`/`WHOLESALE`). Proposal uses Division `operation_type` (ECOM/RTL/WHLS). These must be mapped, not assumed identical.
5. **Allocation → InventoryUnit**, not → balance/location qty.
6. **OrderLine → SKU**, not UPC. A line cannot be allocated by UPC without joining units.
7. **Barcode unique globally.** Two clients cannot share a barcode string. Proposal allows the same UPC (and does not require a global barcode).
8. **`inventory_units.order_type_id`** deprecated leftover FK (nullable, unused).
9. **`orders.processing_user_id`** has no live FOREIGN KEY (additive INTEGER patch).
10. **No DB check that `warehouse.client_id = order.client_id`** (application assumes it). Same for division.
11. **Users have no client membership**, so there is no relationship to enforce tenant RBAC.

---

## F. Current SKU-based logic that must change to UPC

| Location | Current behavior | Required change |
|---|---|---|
| `app/services/order_import.py` `REQUIRED_COLUMNS` | `sku`, `qtyordered` | Mandatory `upc`; SKU optional/descriptive |
| `OrderLine.sku` / `quantity` | Demand grain | Add `upc`; allocate on UPC |
| `allocation._pick_line_with_demand` | `line.sku == sku` | Match `line.upc` |
| `allocation._available_units` | `filter_by(sku=sku)` | `filter_by(upc=upc)` (or balance row) |
| `allocation.allocation_progress` | per-SKU totals | per-UPC totals |
| `allocation.allocate_barcode` error text | "SKU not on order" | UPC not on order |
| Inventory import | requires `sku` + `barcode` | UPC + qty + location; SKU optional |
| Pick lines (`pick_tickets.pick_lines`) | sort Location → SKU → Barcode, qty 1 | Location → UPC → qty |
| Tests `tests/test_barcode_allocation.py`, `tests/test_imports.py`, `tests/test_multiclient.py` | SKU fixtures | Dual coverage during migration |

**Already UPC:** `processing.scan_upc_into_box`, `find_one_remaining_by_upc`, inventory overview `inventory_by_upc`, unit `upc` NOT NULL.

This split is the sharpest operational CONFLICT: warehouse staff already scan UPC, while planning still reserves by SKU. If SKU and UPC ever diverge on a unit, allocation and processing disagree.

---

## G. Current cross-client contamination risks

| Risk | Severity | Detail |
|---|---|---|
| IDOR on numeric IDs | **High** | `Order.query.get_or_404(order_id)` in `orders`, `allocation`, `processing`, `reports`. Same for `Allocation`, `Box`, `Document`, `PickTicket`. Permission is module-wide, not tenant-wide. |
| Global users | **High** | No `user_clients`. An operator with `ORDERS_VIEW` sees every client if they change `client_id` in the URL or open a guessed id. |
| `get_or_create_*` on import | **High** | A spreadsheet typo (`CELIN`) creates a new client and warehouse and imports into it. |
| Spreadsheet as source of tenant | **High** | Inventory/order files choose the client. UI dropdown is not binding context. |
| Global unique barcode | **Medium** | Client B cannot receive a unit whose barcode already exists for Client A (`BARCODE_CLIENT_CONFLICT`). That prevents some contamination but also blocks legitimate independent barcode spaces. |
| Allocation list unscoped | **Medium** | `allocation.index` loads every allocatable order, all clients. |
| KPI / reports filters optional | **Medium** | Empty `client_id` returns all clients (`order_kpis`, completed-orders export). |
| No RLS | **Medium** | Any raw SQL or future endpoint that forgets `client_id` reads the whole table. |
| Auto-create Division `MAIN` | **Low** | Does not leak across clients (scoped), but hides missing division governance. |
| Prisma leftover `Item.sku` unique | **Low** | Unused; do not join it into WMS inventory. |

Allocation itself **does not** pull another client's stock: `_validate_scope` and `_available_units` filter `client_id` + `warehouse_id`. Tests in `tests/test_multiclient.py` cover wrong-client and wrong-warehouse rejection. The contamination risk is **authorization and master-data creation**, not the matcher.

---

## H. Current InventoryUnit architecture and whether migration is needed

**Yes — a migration is required** if the quantity-balance model is adopted. This is not a column rename.

Current grain:

```
inventory_units: 1 row = 1 physical barcode
UNIQUE(barcode) globally
status ∈ {AVAILABLE, ALLOCATED, PACKED, SHIPPED}
indexes (client_id, warehouse_id, upc|sku|location)
```

Live snapshot (15 units): UPC `194900012345` exists as 8 CELINE/NY units across locations `A01-01-02` and `A01-02-01` in statuses ALLOCATED / PACKED / SHIPPED. DIOR holds a different UPC in NY. This already proves UPC is not unique and is tenant-scoped at the unit level.

Proposed grain:

```
inventory_balances: 1 row = Client + Warehouse + UPC + Location
on_hand_qty / reserved_qty / packed_qty
AVAILABLE = ON_HAND - RESERVED - PACKED
```

Migration implications:

1. **Do not drop `inventory_units`.** Existing allocations, box contents, movements, and invoices point at unit ids.
2. Preferred path: **additive dual model**. Create `inventory_balances`, roll up current units, write both during Phases B–G, then freeze unit writes.
3. Barcode can remain a physical serial (carton content, cycle count) but must stop being the allocation key.
4. Global `UNIQUE(barcode)` may later become `UNIQUE(client_id, barcode)` — only after proving no collisions.
5. Deprecated `order_type_id` should stay until a later cleanup; it is unused.

If product instead **keeps unit grain** and only changes allocation to UPC, Phase B shrinks to: add UPC on order lines, allocate by UPC, derive balances as views. That is a smaller, safer option and should be decided before Phase B starts.

---

## I. Current Client / Division / Warehouse structure

### Live masters (read-only inspect)

| Client id | `code` | `name` | Warehouses | Divisions |
|---|---|---|---|---|
| 1 | `CELINE` | Celine | `NY`, `NJ` | `MAIN`, `FASHION`, `BEAUTY` |
| 2 | `DIOR` | Dior | `NY` | `MAIN`, `FASHION` |

Order types (channel on type, not division): CELINE `ECOM`/`ECOMMERCE`/`RETAIL`/`WHOLESALE`; DIOR `ECOM`/`ECOMMERCE`/`RETAIL`.

### Vs proposed codes

| Proposed | Live equivalent | Gap |
|---|---|---|
| `01-CEL` | `CELINE` | No sequence, no initials, name used as code |
| `01-CEL-ECOM` | OrderType `ECOM` + Division `MAIN`/`FASHION` | Division ≠ channel |
| `01-CEL-NY` | Warehouse `NY` under CELINE | Symbol only; generated code missing |

Admin: **no CRUD** for these masters (`app/blueprints/admin.py` is users/permissions/audit only). Creation path is import `get_or_create_*` and KPI `ensure_kpi_schema` (`MAIN` backfill).

---

## J. Current allocation behavior

File: `app/services/allocation.py`.

1. Scope check: `unit.client_id == order.client_id` and `unit.warehouse_id == order.warehouse_id`. Order type is ignored (correct vs current inventory rule).
2. Demand: first order line with matching **SKU** and `active_allocation_count < line.quantity`.
3. Manual path: `allocate_barcode` — one unit, one `Allocation`, unit → `ALLOCATED`, movement `ALLOCATE`, transaction `ALLOCATE`.
4. Auto path: `auto_allocate_order` validates NEW→VALIDATED, then for each line picks available units by SKU ordered by location/barcode.
5. Results: `FULL` / `PARTIAL` / `NO INVENTORY` / `EXCEPTION`. Status becomes `ALLOCATED` only when fully covered; otherwise remains `VALIDATED` or `ALLOCATING` (not `PARTIALLY_ALLOCATED`).
6. Full allocation calls `ensure_pick_ticket`.
7. Release sets allocation `RELEASED`, unit `AVAILABLE`.
8. Isolation: does **not** search other clients or warehouses. Tests prove this.
9. Gaps vs proposal: SKU key; no qty reservation on a balance; no `created_by`; no DB unique for one ACTIVE allocation per unit; no `SELECT FOR UPDATE`.

---

## K. Current order import behavior

File: `app/services/order_import.py`. Routes: `orders.import_view` (one-shot) and `orders.import_preview` / confirm (two-step).

Required columns: `client`, `warehouse`, `ordertype`, `ordernumber`, `sku`, `qtyordered`.  
Optional: `customer`, `carrier`, `shippingservice`, `description`.  
**Not present:** UPC, CustomerAddress, CustomerPhone, Division.

Behavior:

- Groups rows by `(client, warehouse, order_type, order_number)` → one order, many lines.
- Rejects blank required fields and `qty <= 0`.
- Header consistency: Customer / Carrier / Shipping Service only.
- Existing orders skipped with warning (identity = 4-part key).
- Confirm calls `resolve_scope` → **creates** missing Client/Warehouse/OrderType.
- `ensure_default_division(client)` → `MAIN`.
- Status `NEW`.
- No check that warehouse is active or authorized for a division.

Inventory import (`inventory_import.py`) is closer to the proposed two-step atomic flow, but tenant still comes from file columns `client`/`warehouse`/`upc`/`sku`/`barcode`/`location`, and confirm uses `get_or_create_*`.

---

## L. Current pick-ticket identity scheme

| Rule | Live | Proposed |
|---|---|---|
| Permanence | **PASS** — `UNIQUE(order_id)`; `ensure_pick_ticket` reuses | Same |
| Reprints | **PASS** — `pick_ticket_print_events` + `print_count` | Same |
| Number | `PT-2026-000001` | `01-CEL-1251-01` |
| Sequence | Global per calendar year, `MAX+1` | Per client order, ticket sequence |
| Contents | Live join to allocated units (location, sku, upc, barcode, qty 1) | Retain client, order, warehouse, allocated UPC/qty/location |
| Processing entry | `lookup_pick_ticket(number)` | Same idea |

Do **not** renumber existing tickets. New format applies to tickets created after Phase F, with a documented mapping table if operators must search both.

---

## M. Current inventory transaction / history implementation

Two append-only tables, neither of which is the proposed qty ledger:

| Table | Grain | Types written today |
|---|---|---|
| `inventory_movements` | unit / barcode / status / location | `IMPORT_RECEIVE`, `IMPORT_UPDATE`, `ALLOCATE`, `RELEASE`, `PACK`, `SHIP` |
| `transactions` | order / unit / event | `ALLOCATE`, `RELEASE_ALLOCATION`, `PACK`, `CLOSE_BOX`, `CLOSE_ORDER`, `ORDER_STATUS_CHANGE`, processing events (`UNIT_SCAN`, `CARTON_UNIT_REMOVED`, …) |

Gaps:

- No signed quantity or before/after qty snapshots.
- No `ADJUSTMENT`, `UNRESERVE`, `UNPACK`, `RETURN`, `TRANSFER_IN`, `TRANSFER_OUT`.
- `remove_unit_from_carton` writes movement_type `PACK` (misleading).
- `transactions` has no `client_id`.
- Movements' `client_id` / `warehouse_id` are nullable.
- Silent status edits without `record_movement` are not schema-prevented (application convention only).

---

## N. Current indexes / constraints

Present and useful:

- `ix_clients_code` UNIQUE
- `uq_warehouse_client_code` (`client_id`, `code`)
- `uq_division_client_code` (`client_id`, `code`)
- `uq_order_type_client_code`
- `uq_order_scope_number` (`client_id`, `warehouse_id`, `order_type_id`, `order_number`)
- `ix_inventory_units_barcode` UNIQUE
- `ix_inventory_units_cwu` / `_cws` / `_cwl`
- `ix_allocations_unit_status` (non-unique)
- `pick_tickets` unique number + unique `order_id`
- `uq_box_contents_unit`
- KPI indexes on `orders.created_at`, `orders.division_id`, `order_lines.order_id`

Missing vs proposal:

- `UNIQUE(clients.sequence_number)` / atomic sequence object
- Platform `UNIQUE(divisions.division_code)`
- `UNIQUE(client_id, warehouse_symbol)` **if** `code` is changed to generated warehouse_code (today `code` **is** the symbol — uniqueness already holds)
- `UNIQUE(client_id, client_order_number)`
- `UNIQUE(client_id, warehouse_id, upc, location)` on balances
- Partial unique index: one ACTIVE allocation per unit
- Check constraints: `on_hand >= reserved + packed`, non-negative qtys
- FK `orders.processing_user_id → users.id`
- Trigger or check: `warehouse.client_id = order.client_id`, `division.client_id = order.client_id`
- RLS policies (optional Phase I)

---

## O. Migration risks to existing Aiven data

Inspected local `wms` counts (same schema class as Aiven; treat Aiven as **at least** this shape, possibly more rows):

| Entity | Local count | Risk if migrated carelessly |
|---|---|---|
| clients | 2 (`CELINE`, `DIOR`) | Recoding to `01-CEL` / `02-DIO` breaks imports, reports, and any stored code strings |
| warehouses | 3 | Generating `01-CEL-NY` while keeping symbol `NY` is safe **additive**; overwriting `code` is not |
| divisions | 5 | `MAIN`/`FASHION`/`BEAUTY` ≠ `01-CEL-ECOM`; channel lives on order types |
| orders | 13 | 4-part uniqueness; several share client+number patterns only via warehouse/type |
| order lines | 15 | **No UPC**. Cannot allocate by UPC until lines are backfilled from units or a mapping file |
| inventory units | 15 | Source of truth today; dropping them loses barcode/carton history |
| allocations | 12 | Unit FKs |
| pick tickets | 3 (`PT-2026-*`) | Must remain printable and searchable |
| boxes / contents | 5 / 7 | Unit FKs |
| movements / transactions | 35 / 77 | Historical; do not rewrite |
| leftover Prisma | 3 tables | Ignore; do not drop in this program without a dedicated review |

Aiven-specific risks:

1. **Additive-only migrations.** `db.create_all()` + `ensure_*` patches are the current pattern. Continue that. Never `DROP TABLE` / `TRUNCATE`.
2. **Recoding clients** updates every denormalized `client_code` string (`import_batches.clients`, `inventory_exceptions.client_code`) and every operator habit. Prefer adding `client_code` / `sequence_number` / `initials` and keeping legacy `code` until a cutover flag.
3. **Order uniqueness change** can fail if Aiven already has the same client order number in two warehouses or types. Must inventory collisions **before** adding `UNIQUE(client_id, order_number)`.
4. **SKU→UPC backfill** fails when a SKU maps to multiple UPCs on one order, or a line has no matching units.
5. **Balance rollup** must lock writes or run in a maintenance window so counts do not drift.
6. **Status rename** (`NEW`→`UNALLOCATED`, etc.) breaks `ORDER_TRANSITIONS`, templates, KPI filters, and tests in one cut. Use a mapping layer or dual-read.
7. **Render + Aiven** have no RLS today; enabling RLS without a `SET app.client_id` session variable will break the app.
8. Production row counts are unknown to this audit. Phase A must start with a **read-only Aiven inventory** (same queries as `docs/DATABASE_INVENTORY.md`) before any ALTER.

---

## P. Recommended migration sequence

Do not start implementation until this audit is approved. Sequence is designed so each phase is reversible and production data stays.

```
A masters  →  B balances (additive)  →  C qty ledger (dual-write)
  →  D order import  →  E UPC allocation  →  F pick-ticket codes
  →  G processing movements  →  H reports/KPI  →  I isolation tests
  →  J reconciliation certification
```

InventoryUnit remains readable through J. Decommission (not drop) only after J certifies.

---

## Recommended implementation phases

No code in this document is approved for implementation.

### PHASE A — Client / Division / Warehouse master integrity

- **Files / tables:** `app/models.py` (`Client`, `Division`, `Warehouse`), new `division_warehouses`, `app/services/scope.py`, `app/schema.py`, `app/blueprints/admin.py`, new admin templates, `app/services/filters.py`.
- **Schema (additive):**  
  - `clients`: `sequence_number`, `initials`, `created_by`, `updated_at`, `updated_by`; unique sequence.  
  - Optional `client_sequences` table or `SERIAL`/`nextval` owned by the DB.  
  - `divisions.operation_type`; optional `division_code` generated column.  
  - `warehouses.warehouse_code` generated (`{client_code}-{symbol}`) **or** keep `code` as symbol and add `warehouse_code`.  
  - `division_warehouses(division_id, warehouse_id, active)` + trigger/check same `client_id`.
- **Backward compatibility:** Keep `clients.code` as-is (`CELINE`). New `client_code` nullable until backfill. `get_or_create_*` must stop auto-creating once admin exists (feature flag). Existing dropdowns keep working on `id`.
- **Tests:** Atomic sequence under concurrency; initials uppercased; cannot assign division to another client; mapping rejects cross-client; existing import still resolves `CELINE`/`NY`.
- **Rollback:** Drop new columns/tables. No data rewrite in A if backfill is a separate reversible UPDATE.

### PHASE B — Inventory balance + UPC architecture

- **Files / tables:** new `inventory_balances`; `inventory_query.py`; inventory templates; **keep** `inventory_units`.
- **Schema:** `inventory_balances` with `UNIQUE(client_id, warehouse_id, upc, location)`, non-negative check, optional style/color/size. View `AVAILABLE = on_hand - reserved - packed`.
- **Backward compatibility:** Nightly/on-write rollup from units. UI can read balances while allocation still writes units.
- **Tests:** Same UPC in two clients / two warehouses / two locations; no negative; rollup equals `COUNT` of units.
- **Rollback:** Stop writing balances; drop table. Units unchanged.

**Decision gate:** confirm whether units remain the system of record or become a serial sub-ledger. Do not delete units in B.

### PHASE C — Inventory transaction ledger

- **Files / tables:** new `inventory_transactions`; `app/services/movements.py`; packing/processing writers.
- **Schema:** proposed columns; `CHECK (quantity <> 0)`; types enum/check. Dual-write with existing `inventory_movements`.
- **Backward compatibility:** Old Transactions tab keeps reading `inventory_movements` until H.
- **Tests:** Every existing movement type emits a ledger row; failed txn emits none; unpack ≠ pack type.
- **Rollback:** Disable dual-write; drop `inventory_transactions`.

### PHASE D — Order import normalization

- **Files / tables:** `order_import.py`, `orders` blueprint/templates, `Order`, `OrderLine`.
- **Schema:** `orders.wms_order_id`, `customer_address`, `customer_phone`, `created_by`; `order_lines.upc` nullable then NOT NULL after backfill; keep `sku`.
- **Behavior:** Upload context = selected Client + Division. File warehouse validated (exists, active, same client, mapped to division). File Client/Division if present must match context. Preview groups `OrderNumber` as one order. Reject inconsistent headers. **Stop `get_or_create` for unknown masters.**
- **Backward compatibility:** Old workbooks fail closed with a clear column error; provide a new template. Existing orders untouched.
- **Tests:** Multi-line 1251 example; warehouse not on division rejected; typo client rejected; qty ≤ 0 rejected; atomic rollback.
- **Rollback:** Feature-flag old import path; new columns nullable.

### PHASE E — UPC allocation + reservation

- **Files / tables:** `allocation.py`, `Allocation`, `inventory_balances`, tests `test_barcode_allocation.py` / `test_multiclient.py`.
- **Schema:** allocation columns `client_id`, `warehouse_id`, `upc`, `location`, `qty_allocated`, `inventory_balance_id`; partial unique ACTIVE per unit/balance; `SELECT FOR UPDATE` on balance.
- **Behavior:** Match Client → Warehouse → UPC → available qty. Same DB transaction as reserve. Status mapping: 0 qty → remain unallocated; some → `PARTIALLY_ALLOCATED` (new status or mapped `ALLOCATING`); full → `ALLOCATED`.
- **Backward compatibility:** Barcode allocate remains for in-flight unit orders until G. Dual-write reserved_qty + unit status.
- **Tests:** Never SKU fallback; never other client/warehouse; concurrent allocate cannot oversell; release restores qty.
- **Rollback:** Flag back to SKU/unit allocate; new columns unused.

### PHASE F — Pick Ticket linkage

- **Files / tables:** `pick_tickets.py`, `PickTicket`, documents PDF.
- **Schema:** `client_id`, `warehouse_id` on ticket; optional snapshot table `pick_ticket_lines(upc, location, qty)`.
- **Behavior:** New numbers `{client_code}-{order_number}-{seq:02d}`. Existing `PT-*` never rewritten. Reprints still only insert print events. Sequence allocated atomically (`INSERT … RETURNING` or advisory lock).
- **Backward compatibility:** `lookup_pick_ticket` accepts both formats.
- **Tests:** Reprint same number; two clients with order `1251` get distinct tickets; race on two full-allocates.
- **Rollback:** Stop issuing new format; old tickets remain.

### PHASE G — Order Processing inventory movements

- **Files / tables:** `processing.py`, `packing.py`, balances + qty ledger.
- **Behavior:** Keep pick-ticket entry and exclusive lock. One UPC scan = −1 reserved, +1 packed, ledger `PACK`. Remove = reverse (`UNPACK`). Close = −packed, −on_hand, `SHIP`. Enforce invariant. Lock balance rows with the order lock.
- **Backward compatibility:** Carton IDs `ORDERNUMBER-BOXnn` stay. Unit `box_contents` remain until balances are trusted.
- **Tests:** Concurrent scans cannot consume the same reserved unit/qty; lock message unchanged; close blocked when invariant fails.
- **Rollback:** Processing continues on units only.

### PHASE H — Reports / KPI migration

- **Files / tables:** `order_kpis.py`, `completed_orders.py`, `reports.py`, documents, invoices.
- **Schema:** KPI queries group by new `client_code` / `division_code` without breaking date/channel logic. Documents/invoices already have `client_id`.
- **Backward compatibility:** Default KPI client filter required (no all-clients unless ADMIN). Status labels mapped.
- **Tests:** Existing KPI fixtures still count; close does not double-count; export cannot include another client's rows for a scoped user.
- **Rollback:** Revert query layer; columns remain.

### PHASE I — Cross-client isolation / security tests

- **Files / tables:** all blueprints using `get_or_404`; new `require_tenant(entity)`; optional `user_clients`; optional RLS.
- **Schema:** `user_clients(user_id, client_id, role)`; later `ALTER TABLE … ENABLE ROW LEVEL SECURITY` only after session GUC is set on every request.
- **Behavior:** Resolve entity, then assert `entity.client_id ∈ user.clients` (ADMIN may be all-clients). Import context must match membership.
- **Tests:** IDOR suite for `/orders/<id>`, `/allocation/<id>`, `/processing/<id>`, `/inventory` unit, `/reports` doc, `/admin` (admin remains global).
- **Rollback:** Remove decorator; RLS off. Do not enable RLS in the same deploy as the first decorator.

### PHASE J — End-to-end reconciliation certification

- **Files / tables:** read-only certification scripts + fixtures; no destructive SQL.
- **Checks:** For every Client+Warehouse+UPC+Location: `available + reserved + packed = on_hand`; on_hand equals surviving unit counts (or documented exceptions); every balance delta has a ledger row; every closed order has SHIP rows totaling `qty_shipped`; no allocation without a reserved qty; no pick ticket without allocations; no processing session without a lock owner.
- **Rollback:** Certification is read-only.
- **Exit criterion:** Written sign-off that Aiven production (or a restored copy) passes the same queries.

---

## Database changes required (when approved)

Additive only, in phase order:

1. Client coding columns + sequence object.  
2. Division `operation_type` + `division_warehouses`.  
3. Warehouse generated code column (keep symbol).  
4. `inventory_balances` + unique grain + checks.  
5. `inventory_transactions` qty ledger.  
6. Order `wms_order_id`, address/phone; line `upc` + qty counters.  
7. Allocation denormalized tenant/UPC/qty/location + `inventory_balance_id`.  
8. Pick ticket tenant columns + optional line snapshot.  
9. `audit_events.client_id`; import batch context FKs.  
10. Optional `user_clients`.  
11. Harden FKs (`processing_user_id`) and same-client checks.  
12. **Never** drop `inventory_units`, Prisma leftovers, or recode live pick tickets in the same change.

---

## Application changes required (when approved)

| Area | Change |
|---|---|
| Admin | Client / Division / Warehouse / mapping CRUD; freeze client code after first tx |
| Scope | Replace `get_or_create_*` with resolve-or-reject; bind import to selected ids |
| Inventory import | Context Client+Warehouse; qty rows; validate file tenant if present |
| Order import | Context Client+Division; UPC lines; warehouse authorization |
| Allocation | UPC matcher; reserve balances atomically |
| Pick tickets | New number format for new tickets only |
| Processing | Qty movements; keep lock and UPC scan = 1 |
| Auth | Tenant membership + IDOR guards |
| KPI/Reports | Require client scope; consume new codes/statuses via mapping |
| Tests | New isolation, invariant, import, and concurrency suites; keep unit-barcode tests until decommission |

---

## Data migration risks (summary)

1. Recoding `CELINE`→`01-CEL` is a **business identity change**, not a technical rename.  
2. Order uniqueness tightening can fail on existing duplicates.  
3. Order lines have **no UPC** — backfill is lossy without a SKU↔UPC map.  
4. Unit→balance rollup can drift if done while the warehouse is packing.  
5. Status vocabulary change touches every screen and test.  
6. Pick ticket renumbering would break printed paper — **forbidden**.  
7. Enabling RLS without a session variable will take production down.  
8. Aiven may have more rows / collision cases than this local `wms` copy.

---

## Certification plan (Phase J preview)

Run on a **restore of Aiven**, never on production writes:

1. **Inventory invariant:** for each balance (or rolled-up units), `available + reserved + packed = on_hand` and all ≥ 0.  
2. **Ledger completeness:** every status change after cutover has an `inventory_transactions` row; sums reconstruct on_hand.  
3. **Allocation isolation:** attempt allocate client A order against client B stock → reject; same UPC other warehouse → reject.  
4. **Import atomicity:** invalid file → 0 rows inserted; valid file → all-or-nothing.  
5. **Order identity:** `01-CEL-1251` and `02-DIO-1251` coexist; internal `orders.id` unchanged.  
6. **Pick ticket:** reprint count increments; number unchanged; processing lookup works for `PT-*` and new format.  
7. **Lock:** two users, one order → second gets *Order is currently being processed by another user.*  
8. **IDOR:** user bound to CELINE receives 404/403 for DIOR ids.  
9. **Close:** SHIP qty = packed = ordered; units/balances leave on_hand.  
10. **KPI:** client/division totals match certified order set; no cross-client bleed.

---

## Explicit non-actions (this phase)

- No tables dropped.  
- No data deleted or updated.  
- No database reset.  
- No production writes.  
- No merge to `main`.  
- No application architecture implementation.

**STOP. Wait for approval before modifying the architecture.**
