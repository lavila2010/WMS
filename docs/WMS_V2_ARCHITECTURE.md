# WMS V2 Architecture Contract

**Status:** Phase 0 — frozen contract  
**Branch:** `cursor/wms-v2-rebuild-6b85`  
**Stack:** Flask + Jinja + Flask-SQLAlchemy + PostgreSQL (Aiven in production)  
**UI:** Frozen V1 approved design tokens. No React/Next. No dark theme.

V1 on `main` is a prototype. V2 is a greenfield rebuild on this branch and a separate Render service. V1 is not deleted by this program.

---

## 1. Tenant principle

CLIENT is the root operational tenant.

```
Client
 ├── Divisions
 ├── Warehouses
 │    └── Inventory Units
 ├── Division ↔ Warehouse mappings
 ├── Users (via user_clients; ADMIN may be global)
 ├── Orders
 │    └── Order Lines
 ├── Allocations
 ├── Pick Tickets
 ├── Processing / Cartons
 ├── Inventory Transactions
 ├── Documents / Reports
 └── KPI
```

No operational activity may cross Client boundaries.

Every operational row has a deterministic path to `client_id` (column on the row, or a required FK chain that is also denormalized onto the row where the object is queried by raw id).

Server-side tenant authorization is mandatory. Query params, URL ids, hidden fields, and spreadsheet tenant columns are never trusted as the source of Client identity.

---

## 2. Identity rules

| Entity | Business id | Internal PK | Uniqueness |
|---|---|---|---|
| Client | `client_code` e.g. `01-CEL` | `clients.id` | `client_code` unique; `sequence_number` unique |
| Division | `code` e.g. `01-CEL-ECOM` | `divisions.id` | `code` unique platform-wide; belongs to one client |
| Warehouse | `warehouse_code` e.g. `01-CEL-NY` | `warehouses.id` | `UNIQUE(client_id, warehouse_symbol)`; `warehouse_code` unique |
| Order | `wms_order_id` e.g. `01-CEL-1251` | `orders.id` | `UNIQUE(client_id, client_order_number)`; `wms_order_id` unique |
| Pick ticket | e.g. `01-CEL-1251-01` | `pick_tickets.id` | number unique; one ticket per order |
| Carton | `{client_order_number}-BOX01` | `cartons.id` | unique per order sequence |
| Inventory unit | `inventory_units.id` | same | one physical unit per row; UPC not unique |

Client sequence is assigned by a PostgreSQL `SEQUENCE` (`client_code_seq`) inside the create transaction. Initials are uppercased server-side. `client_code` is immutable after insert.

---

## 3. Channel / operation type (resolved)

Orders have **no** `order_type` table in V2.

KPI and report channels come from `divisions.operation_type`:

| `operation_type` | Division code suffix | KPI channel |
|---|---|---|
| `ECOM` | `-ECOM` | `ECOMMERCE` |
| `RTL` | `-RTL` | `RETAIL` |
| `WHLS` | `-WHLS` | `WHOLESALE` |

This is not a redesign; it is the mapping required because V2 order headers have `division_id` and no order-type FK.

---

## 4. Inventory model (Option A — frozen)

Authoritative physical inventory is `inventory_units`.

**Do not create `inventory_balances`.** Quantities are `COUNT(*)` of unit rows grouped by Client + Warehouse + UPC + Location + Status.

| Qty | Definition |
|---|---|
| AVAILABLE | `status = AVAILABLE` |
| RESERVED | `status = RESERVED` |
| PACKED | `status = PACKED` |
| ON_HAND | AVAILABLE + RESERVED + PACKED |
| SHIPPED | `status = SHIPPED` — not on-hand |

UPC is the merchandise key. SKU, Description, style, color, and size are descriptive. Description is required on new inventory imports and is Client-scoped.

Statuses used in V2: `AVAILABLE`, `RESERVED`, `PACKED`, `SHIPPED`.  
`HOLD` / `DAMAGED` / `RETURN_PENDING` are **not** introduced unless a later gate proves they are required. Ledger type `RETURN` restores a shipped/packed unit to `AVAILABLE` when that workflow is implemented.

Every status change writes one `inventory_transactions` row in the same PostgreSQL transaction.

---

## 5. Security model

- Roles: `ADMIN`, `USER`.
- Permissions: catalog codes listed in `docs/WMS_V2_SCHEMA.md` §permissions.
- `user_clients(user_id, client_id)`: explicit membership.
- `ADMIN` may access all clients.
- `USER` may access only associated clients (one, many). Zero memberships ⇒ no operational data.
- Every `get_or_404` on an operational entity must call `require_client_access(user, entity.client_id)` (or equivalent). Failure is 404 (fail closed, no existence leak) or 403 for explicit forbidden actions.
- Import context (Client, Warehouse or Client, Division) is taken from the **authenticated session + posted ids that are re-validated**, never from the spreadsheet as authority.

---

## 6. Concurrency

| Operation | Control |
|---|---|
| Client code | `nextval('client_code_seq')` in the insert transaction |
| Allocation | `SELECT … FOR UPDATE SKIP LOCKED` on candidate `AVAILABLE` units |
| Processing lock | Single `UPDATE orders SET … WHERE id=:id AND (processing_user_id IS NULL OR processing_user_id=:uid)` returning the row. 0 rows ⇒ *Order is currently being processed by another user.* |
| UPC scan | Lock the chosen `RESERVED` unit row; fail if already `PACKED` |
| Order close | Lock order + remaining packed units in one transaction |

Refresh does not release the processing lock. Release only on successful close or authorized cancel/reset.

---

## 7. Document storage

`DocumentStore` protocol:

- `save(name, bytes, content_type) -> storage_key`
- `open(storage_key) -> bytes`
- `url_or_path(storage_key) -> str`

Adapters: `LocalDocumentStore` (dev / default). Production object storage adapter is configured by env (`DOCUMENT_STORE=s3` + credentials). If credentials are absent, V2 uses local adapter and **must not** claim durable Render-disk storage.

---

## 8. Database configuration

| Env | Purpose |
|---|---|
| `WMS_V2_DATABASE_URL` | Preferred V2 database (Aiven V2) |
| `DATABASE_URL` | Fallback only if `WMS_V2_DATABASE_URL` unset (local/dev) |
| `TEST_DATABASE_URL` | Automated tests (local `wms_test`) |
| `AIVEN_AUDIT_DATABASE_URL` | Read-only V1/production audit — **never** a V2 reset target |

`flask --app wsgi init-db` creates V2 tables. It does **not** run on Gunicorn startup. No reset command may run automatically in production.

---

## 9. UI freeze

Tokens in `app/static/css/styles.css` (`:root`) remain the source of visual truth.

Navigation (visibility by permission only):

WMS SYSTEM · Dashboard · Inventory · Orders · Allocation · Order Processing · Order Reports · KPI Orders · Administration · User / Role

Administration tabs add: Users, Permissions, Client Access, Clients, Divisions, Warehouses, Mappings, Audit. Layout, colors, radius, and fonts stay frozen.

---

## 10. Module map (implementation)

| Module | Responsibility |
|---|---|
| `app/config.py` | Env, SSL, reject insecure `SECRET_KEY` in production |
| `app/models.py` | V2 schema |
| `app/auth.py` | Login, RBAC, tenant require_* |
| `app/services/masters.py` | Client/division/warehouse/mapping writes |
| `app/services/inventory_import.py` | Stage Excel → compact preview → bulk unit/ledger chunks |
| `app/services/order_import.py` | Context + atomic header/lines |
| `app/services/allocation.py` | UPC reserve + ledger |
| `app/services/pick_tickets.py` | Permanent numbers + print events |
| `app/services/processing.py` | Lock, scan, cartons, close |
| `app/services/documents.py` | Store + PDFs |
| `app/services/order_kpis.py` | Client+Division aggregates |
| `app/blueprints/*` | HTTP, permission + tenant checks |

---

## 11. Invariants (must hold after every committed transaction)

I1. `AVAILABLE + RESERVED + PACKED = ON_HAND` per Client+Warehouse+UPC+Location.  
I2. A unit has at most one ACTIVE allocation.  
I3. A unit is in at most one carton.  
I4. `order.client_id = warehouse.client_id = division.client_id`.  
I5. Every `division_warehouses` pair shares one `client_id`.  
I6. Every inventory status change has exactly one `inventory_transactions` row.  
I7. Closed order: `qty_ordered = qty_allocated = qty_packed = qty_shipped` and remaining units = 0.  
I8. `wms_order_id = client_code || '-' || client_order_number`.  
I9. Pick ticket number never changes after insert.  
I10. No inventory_unit.client_id differs from its warehouse.client_id.

---

## 12. Phase 0 consistency statement

No unresolved relationship ambiguity remains in this contract:

- Channel is `division.operation_type`, not a separate order-type master.
- Inventory SoR is units, not balances.
- Unit status `RESERVED` (not V1 `ALLOCATED`) matches the V2 state machine.
- Order statuses are exactly the seven names in the master mission.
- Carton business id uses `client_order_number` (`1251-BOX01`), scoped by order id.

GATE 0 requires this document plus schema, workflows, and test plan to agree. They do.
