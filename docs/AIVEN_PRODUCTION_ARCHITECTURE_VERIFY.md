# WMS Aiven Production Architecture Verification

**Mission:** WMS-AIVEN-PRODUCTION-ARCHITECTURE-VERIFY-012  
**Date:** 2026-09-20 (UTC)  
**Branch:** `cursor/wms-nextjs-prisma-env-6b85`  
**Mode:** SELECT / catalog inspection only. No CREATE / ALTER / DROP / INSERT / UPDATE / DELETE / TRUNCATE / seed / migrate / recode / merge.  
**Credentials:** omitted. `DATABASE_URL` and password hashes are never printed.

---

## DATABASE TARGET VERIFICATION

| Check | Result |
|---|---|
| Injected `DATABASE_URL` present | yes |
| Engine / scheme | `postgresql` |
| Host | `127.0.0.1` |
| Port | `5432` |
| Database name | `wms` |
| Schema | not queried (target rejected before catalog work) |
| SSL | none (`sslmode` absent) |
| Host fingerprint / domain | `127.0.0.1` (loopback) |
| Aiven hostname markers (`aivencloud`, `aiven.com`, `avns.net`) | **absent** |
| Matches Render production Aiven | **no** |

`app/config.py` treats `127.0.0.1` / `localhost` as local and does **not** force `sslmode=require`. Aiven production requires SSL on a non-local host.

Additional search (values never printed):

- No `.env` with a production URI in the workspace (only `.env.example` pointing at local `wms`).
- `render.yaml` declares `DATABASE_URL` as `sync: false` (set in the Render dashboard, not in git).
- GitHub Actions secrets/variables: not readable (HTTP 403).
- No Render API key, Aiven CLI config, or `~/.pgpass`.
- Process environment has no `AIVEN_*` or `RENDER_*` keys.

```
DATABASE_TARGET=NOT_AIVEN
DATABASE_TARGET_NOT_VERIFIED
```

The previous Phase 1 audit connected to **local** PostgreSQL 16.15 Ubuntu, database `wms`. That target is the same class as this injected URL. It is **not** reused here as a production substitute.

**Local substitution: refused.**

Production table counts, master dumps, collision analysis, and UPC percentages below are therefore **not available**. Application/schema conclusions in later sections are from repository code only and are labeled as such.

---

## PRODUCTION TABLE COUNTS

| Table / metric | Production count |
|---|---|
| clients | **NOT VERIFIED** |
| warehouses | **NOT VERIFIED** |
| divisions | **NOT VERIFIED** |
| order_types | **NOT VERIFIED** |
| users | **NOT VERIFIED** |
| permissions | **NOT VERIFIED** |
| user_permissions | **NOT VERIFIED** |
| inventory_units | **NOT VERIFIED** |
| inventory_movements | **NOT VERIFIED** |
| transactions | **NOT VERIFIED** |
| orders | **NOT VERIFIED** |
| order_lines | **NOT VERIFIED** |
| allocations | **NOT VERIFIED** |
| pick_tickets | **NOT VERIFIED** |
| pick_ticket_print_events | **NOT VERIFIED** |
| boxes (cartons) | **NOT VERIFIED** |
| box_contents (packed units) | **NOT VERIFIED** |
| audit_events | **NOT VERIFIED** |
| import_batches | **NOT VERIFIED** |
| order_exceptions | **NOT VERIFIED** |
| inventory_exceptions | **NOT VERIFIED** |
| invoices | **NOT VERIFIED** |
| documents | **NOT VERIFIED** |
| leftover Prisma `Item` / `Location` / `_prisma_migrations` | **NOT VERIFIED** |

No production row was selected. Local demo counts from `docs/DATABASE_INVENTORY.md` must not be treated as Aiven production.

---

## PRODUCTION MASTER STRUCTURE

| Entity | Safe identifiers requested | Production result |
|---|---|---|
| Clients (`id`, `code`, `name`, `active`) | **NOT VERIFIED** | No Aiven SELECT |
| Warehouses (`id`, `client_id`, symbol/code, `active`) | **NOT VERIFIED** | No Aiven SELECT |
| Divisions (`id`, `client_id`, `code`, `active`) | **NOT VERIFIED** | No Aiven SELECT |
| Pick tickets (number, `order_id` only) | **NOT VERIFIED** | No Aiven SELECT |
| Orders (collision aggregates only) | **NOT VERIFIED** | No Aiven SELECT |

Expected application columns (from models, not from Aiven):

- `clients`: `id`, `code`, `name`, `active`, `created_at`
- `warehouses`: `id`, `client_id`, `code`, `name`, `active`, `created_at`; `UNIQUE(client_id, code)`
- `divisions`: `id`, `client_id`, `code`, `name`, `active`, `created_at`; `UNIQUE(client_id, code)`
- `pick_tickets`: `pick_ticket_number`, `order_id` unique

Whether production already uses `CELINE`/`DIOR` versus `01-CEL` **cannot be certified** without Aiven.

---

## COLLISION ANALYSIS

All collision checks require production `SELECT` aggregates. Status of each:

| Check | Production result |
|---|---|
| A. Duplicate client initials candidates | **NOT VERIFIED** |
| B. Same `order_number` within one client across warehouses / order types / divisions | **NOT VERIFIED** |
| C. UPC ambiguity (SKU→many UPC, UPC→many SKU, missing line UPC, null inventory UPC) | **NOT VERIFIED** |
| D. Warehouse symbol repetitions between clients | **NOT VERIFIED** |
| E. Duplicate / conflicting division codes | **NOT VERIFIED** |
| F. Collision with proposed identifiers (`01-CEL`, `01-CEL-1251`, `01-CEL-1251-01`) | **NOT VERIFIED** |

Queries prepared for a future Aiven read-only pass (do not run against local and label as production):

```sql
-- A. initials candidates from current client codes
SELECT upper(left(regexp_replace(name, '[^A-Za-z]', '', 'g'), 3)) AS initials_guess,
       count(*) AS n, array_agg(code ORDER BY id) AS codes
FROM clients
GROUP BY 1
HAVING count(*) > 1;

-- B. order_number reused inside one client across warehouse / type / division
SELECT client_id, order_number, count(*) AS n,
       count(DISTINCT warehouse_id) AS warehouses,
       count(DISTINCT order_type_id) AS order_types,
       count(DISTINCT division_id) AS divisions
FROM orders
GROUP BY client_id, order_number
HAVING count(*) > 1;

-- C1. SKU -> multiple UPCs in inventory
SELECT client_id, warehouse_id, sku, count(DISTINCT upc) AS upc_n
FROM inventory_units
GROUP BY 1,2,3
HAVING count(DISTINCT upc) > 1;

-- C2. UPC -> multiple SKUs in inventory
SELECT client_id, warehouse_id, upc, count(DISTINCT sku) AS sku_n
FROM inventory_units
GROUP BY 1,2,3
HAVING count(DISTINCT sku) > 1;

-- C3. order_lines have no upc column today; inventory UPC null/blank
SELECT count(*) FILTER (WHERE upc IS NULL OR btrim(upc) = '') AS missing_upc,
       count(*) AS total
FROM inventory_units;

-- D. warehouse symbol repeated across clients
SELECT code, count(DISTINCT client_id) AS clients
FROM warehouses
GROUP BY code
HAVING count(DISTINCT client_id) > 1;

-- E. division code repeated across clients
SELECT code, count(DISTINCT client_id) AS clients
FROM divisions
GROUP BY code
HAVING count(DISTINCT client_id) > 1;
```

Schema precondition already known from models (not Aiven): `order_lines` has **no `upc` column**. Collision check C “UPC missing from order_lines” is therefore **100% of lines by application schema**, even before production counts exist. That is a schema fact, not a production row fact.

---

## UPC MIGRATION FEASIBILITY

| Metric | Production result | Application/schema fact |
|---|---|---|
| % `inventory_units` with UPC | **NOT VERIFIED** | Model: `upc` is `nullable=False`. If production matches the model, fill rate should be ~100% unless raw SQL inserted blanks. |
| % `order_lines` with UPC | **NOT VERIFIED** | Model: **no `upc` column**. Fill rate is **0%** until a column is added. |
| SKU→UPC mapping 1:1 / 1:N / N:1 | **NOT VERIFIED** | Allocation today matches SKU; processing matches UPC. Ambiguity is possible and must be measured on Aiven. |
| % order lines deterministically backfillable | **NOT VERIFIED** | A line can be backfilled only if every unit (or a catalog) for that client+warehouse+SKU maps to exactly one UPC. |
| Records that cannot be safely backfilled | **NOT VERIFIED** | Candidates: SKU with multiple UPCs; SKU with no inventory UPC; lines whose SKU never appears in `inventory_units`. |

Do not modify rows. A backfill is not approved.

---

## INVENTORY UNIT VS BALANCE ANALYSIS

`inventory_balances` was **not created**. The comparison below is from application code and the Phase 1 model audit. Production derivation of quantities is **NOT VERIFIED** because Aiven unit rows were not read.

### Can quantities be derived from `inventory_units`?

Application definition today (`app/services/inventory_query.py`):

| Proposed qty | Derivation from units | Code status |
|---|---|---|
| ON_HAND | `COUNT(*)` where `status IN ('AVAILABLE','ALLOCATED','PACKED')` | Implemented as `total` |
| AVAILABLE | `COUNT(*)` where `status = 'AVAILABLE'` | Implemented |
| RESERVED / ALLOCATED | `COUNT(*)` where `status = 'ALLOCATED'` | Implemented as `reserved` |
| PACKED | `COUNT(*)` where `status = 'PACKED'` | Implemented |

Grain already indexed: `(client_id, warehouse_id, upc)` and `(client_id, warehouse_id, location)`. A location-level rollup is `GROUP BY client_id, warehouse_id, upc, location`.

Invariant if ON_HAND excludes `SHIPPED`:

```
AVAILABLE + ALLOCATED + PACKED = ON_HAND
```

Each unit has `client_id`, `warehouse_id`, `upc`, `sku`, `location`, `barcode` (globally unique), `status`, and optional `order_id`. Allocations and carton contents point at `inventory_unit_id`. That is enough physical traceability for Option A **if** production rows are complete (NOT VERIFIED on Aiven).

### OPTION A — `InventoryUnit` remains authoritative

Quantities derived by aggregation. Add an immutable transaction ledger. Do not introduce `inventory_balances` as a second writer.

| | |
|---|---|
| **Benefits** | No dual source of truth. Existing allocations, `box_contents`, movements, and invoices keep working. Matches current processing (one UPC scan = one unit). No negative-qty class of bugs. Smaller migration. |
| **Risks** | Qty at a location is a count, not a stored balance. Large warehouses pay `COUNT`/`GROUP BY` cost. Global unique barcode may block two clients from sharing a barcode string. SKU allocation must still move to UPC. |
| **Migration complexity** | Low–medium. Additive UPC on `order_lines`, change matcher, add qty ledger rows that reference `inventory_unit_id`. No rollup job. |
| **Drift risk** | Low. There is only one writer (unit status). Ledger is descriptive. |
| **Concurrency** | Need `SELECT FOR UPDATE` on the chosen unit (or a partial unique ACTIVE allocation). Exclusive order lock already exists. Oversell risk is “two sessions take the same AVAILABLE unit,” not qty underflow. |

### OPTION B — Introduce `inventory_balances` and dual-write

| | |
|---|---|
| **Benefits** | Direct `AVAILABLE = ON_HAND - RESERVED - PACKED` on a single row. Cheaper reservation updates. Aligns with the proposed qty architecture. Easier location-level checks without grouping units. |
| **Risks** | Two sources of truth. Any missed writer (import, allocate, pack, unpack, ship, release) drifts balances vs units. Existing carton/allocation FKs stay on units, so both models must stay consistent. |
| **Migration complexity** | High. Create table, roll up production units (needs Aiven lock or quiet window), dual-write every path, certify, then freeze unit writes. |
| **Drift risk** | High until every path dual-writes and Phase J certifies. Production row volume is unknown. |
| **Concurrency** | Balance row `SELECT FOR UPDATE` plus unit locks, or risk reserved_qty ≠ count of ALLOCATED units. Harder than Option A. |

**No option is chosen. No table was created.**

Aiven must still answer: unit row completeness, null UPC/location, orphan allocations, and whether `COUNT` performance is acceptable on production volume.

---

## TENANT SECURITY RISKS

This section is **application-code analysis**. It does not depend on Aiven connectivity. Production user/client membership rows are **NOT VERIFIED**.

### User-to-client authorization

**Does not exist.**

- `users` has no `client_id`.
- No `user_clients` (or equivalent) table in models.
- RBAC is global: `role` `ADMIN`/`USER` plus `user_permissions` codes.
- `User.has_permission(code)` does not consult a client.
- UI dropdowns filter lists by `client_id` query param. That is not authorization.

### Object lookup by raw ID without Client authorization

Permission is checked (`@permission_required`). Tenant is not.

| Area | Route / lookup | Guard today | Client check |
|---|---|---|---|
| Orders | `GET /orders/<order_id>` | `ORDERS_VIEW` + `get_or_404` | **none** |
| Orders | `POST /orders/<order_id>/validate` | `ORDERS_VIEW` + `get_or_404` | **none** |
| Orders | `GET/POST` pick ticket by `ticket_id` | pick-ticket permissions + `get_or_404` | **none** |
| Allocation | `GET /allocation/<order_id>` | `ALLOCATION_VIEW` + `get_or_404` | **none** |
| Allocation | `POST /allocation/<order_id>/scan` | `ALLOCATION_EXECUTE` + `get_or_404` | **none** |
| Allocation | `POST /allocation/<order_id>/mark-allocated` | `ALLOCATION_EXECUTE` | **none** |
| Allocation | `POST /allocation/<order_id>/ready-to-pick` | `ALLOCATION_EXECUTE` | **none** |
| Allocation | `POST /allocation/release/<allocation_id>` | `ALLOCATION_RELEASE` + `get_or_404` | **none** |
| Processing | `POST /processing/<order_id>/confirm` and later order_id routes | `PROCESSING_*` + `get_or_404` | **none** (lock is per-order, not per-client) |
| Processing | box_id / content_id / document_id `get_or_404` | processing permissions | **none** |
| Inventory | `GET /inventory/barcode/<barcode>` | `INVENTORY_VIEW` | dropdown scope **not** applied; any barcode returns |
| Inventory | `GET /inventory/import-history/<batch_id>` | `INVENTORY_VIEW` + `get_or_404` | **none** |
| Reports | `GET` order / box / document by id | `REPORTS_*` + `get_or_404` | **none** |
| KPI | empty `client_id` | `REPORTS_VIEW` | **all clients** |
| Allocation index | `GET /allocation/` | `ALLOCATION_VIEW` | **all clients** |
| Admin | `/admin/users/<user_id>` | admin permissions | N/A (users are global) |

Operational matchers **do** reject cross-client **inventory consumption** (`allocation._validate_scope`, `processing.scan_upc_into_box`). That is not the same as hiding another client's order, ticket, document, or barcode page.

PostgreSQL RLS: not enabled on the local catalog previously inspected; **Aiven RLS state NOT VERIFIED**.

No patches were applied.

---

## PROPOSED MIGRATION PRECONDITIONS

Do not start Phases A–J data work until all of the following are true.

1. **Verified Aiven target.** Agent (or a reviewed operator session) connects with the Render production `DATABASE_URL`. Host must be Aiven (non-loopback, SSL required). Confirm:
   - `DATABASE_TARGET=AIVEN_PRODUCTION`
   - engine PostgreSQL
   - version, database name, schema `public`
   - host domain fingerprint without user/password
2. **Read-only role preferred.** A SELECT-only Aiven user is enough for this mission. The app role must not be used for writes during verification.
3. **Re-run this document’s count / master / collision / UPC queries** on that connection and replace every `NOT VERIFIED` cell.
4. **Inventory decision remains open** until production unit completeness and volume are known (Option A vs B).
5. **Order uniqueness collisions** must be counted before adding `UNIQUE(client_id, order_number)`.
6. **SKU↔UPC ambiguity** must be counted before making UPC the allocation key.
7. **Do not recode** `clients.code` until production codes and initials collisions are known.
8. **Do not drop** `inventory_units`, pick-ticket numbers, or leftover Prisma tables.
9. **Tenant authorization** is an application change (Phase I). Schema inspection alone cannot close IDOR.
10. **No merge to `main`** as part of verification.

---

## What was and was not done

| Action | Done? |
|---|---|
| Connect to injected `DATABASE_URL` identity | yes (parsed, credentials omitted) |
| Treat local `127.0.0.1/wms` as Aiven | **no** |
| SELECT production counts / masters | **no** |
| CREATE / ALTER / DROP / DML | **no** |
| Create `inventory_balances` | **no** |
| Recode clients / migrate / seed | **no** |
| Patch IDOR / implement architecture | **no** |
| Merge | **no** |

---

## Final status

```
DATABASE_TARGET_NOT_VERIFIED
AIVEN_ARCHITECTURE_NOT_VERIFIED
```

**STOP.** Wait for a verified Aiven production `DATABASE_URL` (SSL, non-local host). Then re-run this mission as SELECT-only. Do not implement architecture changes on an unverified target.
