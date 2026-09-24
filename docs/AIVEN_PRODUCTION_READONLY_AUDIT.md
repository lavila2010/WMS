# WMS Aiven Production Read-Only Audit

**Mission:** WMS-AIVEN-READONLY-PRODUCTION-AUDIT-013  
**Date:** 2026-09-20 (UTC)  
**Branch:** `cursor/wms-nextjs-prisma-env-6b85`  
**Mode:** SELECT / catalog only. `DATABASE_URL` was not used.  
**Credentials:** omitted. Username, password, and full URI are never printed.

---

## DATABASE TARGET

| Check | Result |
|---|---|
| `AIVEN_AUDIT_DATABASE_URL` present in this VM | **no** (unset, length 0) |
| Parsed host / port / db / SSL | **not parsed** — variable absent |
| Non-local host | **not verified** |
| SSL enabled | **not verified** |
| Aiven domain pattern | **not verified** |
| PostgreSQL connection | **not attempted** |
| `DATABASE_URL` used | **no** (`DATABASE_URL` is local `127.0.0.1` / `wms`; refused) |

Search performed (values not printed):

- Process environment of this shell and a login shell
- `/proc/*/environ` key names
- Secret mount paths (`/run/secrets`, `/opt/cursor/secrets`, …)
- Workspace files (no `AIVEN_AUDIT_DATABASE_URL` key)
- GitHub Actions secrets: not readable (HTTP 403)

This agent session started before the secret was announced and has **no linked Cursor environment** (`environment: null`). Secrets added in the dashboard are not visible here.

```
DATABASE_TARGET_NOT_VERIFIED
```

Required label `DATABASE_TARGET=AIVEN_PRODUCTION_READONLY` was **not** assigned.

**STOP condition met.** No production SQL was executed. Local substitution refused.

---

## READ-ONLY ROLE STATUS

| Privilege | Production role |
|---|---|
| Connected role name | **NOT VERIFIED** (not connected) |
| SELECT | **NOT VERIFIED** |
| INSERT | **NOT VERIFIED** |
| UPDATE | **NOT VERIFIED** |
| DELETE | **NOT VERIFIED** |
| CREATE | **NOT VERIFIED** |
| ALTER / DROP / ownership | **NOT VERIFIED** |

```
READONLY_ROLE_WARNING
```

Reason: privilege inspection requires a live Aiven session. No `has_table_privilege` / `pg_roles` query was run. No write or destructive statement was attempted.

Target (SELECT allowed, writes denied) is **unconfirmed**.

---

## TABLE COUNTS

Every `NOT VERIFIED` cell from `docs/AIVEN_PRODUCTION_ARCHITECTURE_VERIFY.md` remains unverified. No production row was counted.

| Table | Production count |
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
| boxes | **NOT VERIFIED** |
| box_contents | **NOT VERIFIED** |
| audit_events | **NOT VERIFIED** |
| import_batches | **NOT VERIFIED** |
| order_exceptions | **NOT VERIFIED** |
| inventory_exceptions | **NOT VERIFIED** |
| invoices | **NOT VERIFIED** |
| documents | **NOT VERIFIED** |
| leftover Prisma tables | **NOT VERIFIED** |

Companion catalog dump: `docs/AIVEN_PRODUCTION_SCHEMA.json` records `catalog_inspected: false`.

---

## CLIENT / DIVISION / WAREHOUSE INTEGRITY

| Check | Production result |
|---|---|
| Clients (`id`, `code`, `name`, `active`) | **NOT VERIFIED** |
| Warehouses (`id`, `client_id`, symbol/code, `active`) | **NOT VERIFIED** |
| Divisions (`id`, `client_id`, `code`, `active`) | **NOT VERIFIED** |
| Duplicate warehouse symbols across clients | **NOT VERIFIED** |
| Invalid cross-client warehouse FKs | **NOT VERIFIED** |
| Duplicate division codes | **NOT VERIFIED** |
| Orphan warehouses / divisions / orders | **NOT VERIFIED** |
| Inactive masters still referenced | **NOT VERIFIED** |

No customer or order PII was selected (no connection).

---

## ORDER COLLISION ANALYSIS

Proposed identity: `<ClientCode>-<ClientOrderNumber>` with `UNIQUE(client_id, client_order_number)` (today `orders.order_number`).

| Category | Count | Cause | Migration risk |
|---|---|---|---|
| Same `order_number` in one client, different warehouses | **NOT VERIFIED** | Live unique key includes `warehouse_id` + `order_type_id` | Tightening uniqueness can fail if any collision exists |
| Same `order_number` in one client, different order types | **NOT VERIFIED** | Same | Same |
| Same `order_number` in one client, different divisions | **NOT VERIFIED** | `division_id` is not in the unique key | Same |
| Cross-client same `order_number` | **NOT VERIFIED** | Allowed by proposal | Not a conflict |

Aggregate SQL is listed in `docs/AIVEN_PRODUCTION_ARCHITECTURE_VERIFY.md` and must be run only against `AIVEN_AUDIT_DATABASE_URL`.

---

## UPC MIGRATION FEASIBILITY

| Metric | Production |
|---|---|
| `inventory_units` total | **NOT VERIFIED** |
| `inventory_units` UPC non-null % | **NOT VERIFIED** |
| `inventory_units` SKU non-null % | **NOT VERIFIED** |
| Same SKU → multiple UPCs | **NOT VERIFIED** |
| Same UPC → multiple SKUs | **NOT VERIFIED** |
| `order_lines` total | **NOT VERIFIED** |
| `order_lines` has UPC column | **NOT VERIFIED on Aiven** |
| Deterministic SKU→UPC backfill % | **NOT VERIFIED** |
| Ambiguous backfill % | **NOT VERIFIED** |
| Impossible backfill % | **NOT VERIFIED** |

Application/schema fact (models, not Aiven): `OrderLine` has `sku` + `quantity` only — **no `upc` column**. Until production catalog confirms otherwise, line-level UPC fill is 0% by model. `InventoryUnit.upc` and `.sku` are `nullable=False` in the model.

No records were altered.

---

## INVENTORY SOURCE-OF-TRUTH ANALYSIS

`inventory_balances` was not created. Production feasibility of derived quantities is **NOT VERIFIED** (no unit rows read).

From application code (`inventory_query.py`), if production rows match the model:

| Qty | Derivation | Reliable? |
|---|---|---|
| AVAILABLE | `status = 'AVAILABLE'` | Yes, if status is always one of the four unit statuses |
| ALLOCATED / RESERVED | `status = 'ALLOCATED'` | Yes, same |
| PACKED | `status = 'PACKED'` | Yes, same |
| ON_HAND | AVAILABLE + ALLOCATED + PACKED | Yes if ON_HAND excludes `SHIPPED` |

Grain: `GROUP BY client_id, warehouse_id, upc, location`. Indexes `ix_inventory_units_cwu` / `_cwl` exist in the model. Aiven index presence **NOT VERIFIED**.

### OPTION A — units authoritative, aggregate qty, add ledger

| Axis | Assessment without production volume |
|---|---|
| Data migration risk | Low–medium once Aiven UPC/status completeness is known |
| Concurrency risk | Unit-row lock / unique ACTIVE allocation |
| Drift risk | Low (single writer) |
| Operational complexity | Lowest of the two options |
| Performance | Depends on Aiven unit count — **NOT VERIFIED** |
| Rollback | Remove ledger writers; units unchanged |

### OPTION B — `inventory_balances` + dual-write

| Axis | Assessment without production volume |
|---|---|
| Data migration risk | High (rollup + quiet window; volume unknown) |
| Concurrency risk | Dual locks on balance + unit |
| Drift risk | High until every writer dual-writes |
| Operational complexity | High |
| Performance | Better qty updates; worse write path |
| Rollback | Drop unused balances if dual-write never cut over |

**Neither option implemented. Neither option chosen.**

---

## TENANT SECURITY RISKS

Application-code analysis (no Aiven user/client rows). Production membership counts **NOT VERIFIED**.

| Finding | Severity |
|---|---|
| No `user_clients` / user.`client_id`. Users are global. | **CRITICAL** |
| `Order.query.get_or_404(order_id)` on orders, allocation, processing, reports | **HIGH** |
| `Allocation` / `Box` / `BoxContent` / `Document` / `PickTicket` / `ImportBatch` by raw id | **HIGH** |
| `GET /inventory/barcode/<barcode>` ignores Client dropdown | **HIGH** |
| KPI / allocation index with empty `client_id` returns all clients | **HIGH** |
| Import `get_or_create_*` can create a new client from a spreadsheet | **HIGH** |
| Allocation/processing reject cross-client **unit consumption** | **LOW** (works; does not hide other clients’ documents) |
| PostgreSQL RLS on Aiven | **NOT VERIFIED** (treat as MEDIUM until catalog is read) |

No patches applied.

---

## SCHEMA VS MODELS

Aiven catalog was **not** compared. `docs/AIVEN_PRODUCTION_SCHEMA.json` is a placeholder with `catalog_inspected: false`.

| Comparison | Result |
|---|---|
| Missing tables | **NOT VERIFIED** |
| Extra tables | **NOT VERIFIED** |
| Missing columns | **NOT VERIFIED** |
| Extra columns | **NOT VERIFIED** |
| Type mismatches | **NOT VERIFIED** |
| Nullability mismatches | **NOT VERIFIED** |
| FK mismatches | **NOT VERIFIED** |
| Unique constraint mismatches | **NOT VERIFIED** |
| Index mismatches | **NOT VERIFIED** |

No migrations were applied.

---

## MIGRATION PRECONDITIONS

1. Inject `AIVEN_AUDIT_DATABASE_URL` into **this** agent VM (or start a new agent after the secret exists).
2. Parse: non-local host, SSL, Aiven domain, connection succeeds → `DATABASE_TARGET=AIVEN_PRODUCTION_READONLY`.
3. Confirm SELECT-only privileges; if writes exist, keep `READONLY_ROLE_WARNING` and still use SELECT only.
4. Replace every `NOT VERIFIED` cell with production evidence.
5. Count `UNIQUE(client_id, order_number)` collisions before Phase A uniqueness work.
6. Count SKU↔UPC ambiguity before UPC allocation.
7. Do not recode clients, drop units, or implement Option B until this audit status is PASS or PASS_WITH_RISKS.
8. Do not use `DATABASE_URL` for production certification.
9. Do not merge.

---

## GO / NO-GO FOR PHASE A

**NO-GO**

Phase A (Client / Division / Warehouse master integrity) must not start. Production masters, collisions, and the Aiven catalog are unseen. Implementing against local `wms` would not be a production migration.

---

## What was done

| Action | Done? |
|---|---|
| Use only `AIVEN_AUDIT_DATABASE_URL` | yes (absent → stop) |
| Use `DATABASE_URL` | **no** |
| Connect to Aiven | **no** |
| DML / DDL / destructive tests | **no** |
| Implement architecture | **no** |
| Merge | **no** |
| Request secret injection for this VM | yes (`AIVEN_AUDIT_DATABASE_URL`) |

---

## Final status

```
DATABASE_TARGET_NOT_VERIFIED
READONLY_ROLE_WARNING
AIVEN_PRODUCTION_AUDIT_FAIL
```

**STOP.** Re-run this mission after `AIVEN_AUDIT_DATABASE_URL` is visible in the agent process environment. Do not implement. Do not merge.
