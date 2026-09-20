# WMS V2 Phase 1 Report

PHASE: 1 — Foundation  
STATUS: PASS

## OBJECTIVE

Implement V2 app factory/config, schema, authentication, users, permissions, user_clients, clients, divisions, warehouses, division_warehouses, audit foundation, and Administration UI. Prove tenant isolation, RBAC, IDOR denial, and client-code concurrency.

## FILES CREATED

- `app/services/masters.py`
- `app/services/tenant.py`
- `app/templates/admin/client_access.html`
- `app/templates/admin/client_form.html`
- `app/templates/admin/clients.html`
- `app/templates/admin/divisions.html`
- `app/templates/admin/mappings.html`
- `app/templates/admin/warehouses.html`
- `app/templates/v2/placeholder.html`
- `tests/test_v2_phase01_foundation.py`
- `docs/WMS_V2_PHASE_01.md`

## FILES MODIFIED

- `app/__init__.py`
- `app/auth.py`
- `app/blueprints/admin.py`
- `app/blueprints/allocation.py`
- `app/blueprints/dashboard.py`
- `app/blueprints/health.py`
- `app/blueprints/inventory.py`
- `app/blueprints/kpi.py`
- `app/blueprints/orders.py`
- `app/blueprints/processing.py`
- `app/blueprints/reports.py`
- `app/config.py`
- `app/constants.py`
- `app/models.py`
- `app/permissions.py`
- `app/schema.py`
- `app/static/css/styles.css`
- `app/templates/admin/_tabs.html`
- `app/templates/dashboard.html`
- `tests/conftest.py`

V1 operational test modules deleted (V2 greenfield suite starts at Phase 1). V1 application on `main` is untouched.

## DATABASE OBJECTS CREATED

- Sequence `client_code_seq`
- Tables: `users`, `permissions`, `user_permissions`, `user_clients`, `audit_events`, `clients`, `divisions`, `warehouses`, `division_warehouses`, plus reserved operational tables (`import_batches`, `inventory_units`, `inventory_transactions`, `orders`, `order_lines`, `allocations`, `pick_tickets`, `pick_ticket_print_events`, `cartons`, `carton_contents`, `documents`, `invoices`)
- Trigger `trg_dw_same_client` / function `enforce_dw_same_client`
- Unique constraints: `client_code`, `sequence_number`, `uq_division_code`, `uq_division_client_op`, `uq_warehouse_client_symbol`, `uq_warehouse_code`, `uq_division_warehouse`, `uq_user_client`, `uq_user_permission`

## IMPLEMENTATION SUMMARY

- `WMS_V2_DATABASE_URL` preferred; production rejects insecure `SECRET_KEY`.
- Client codes assigned by `nextval('client_code_seq')` as `{seq:02d}-{INITIALS}`.
- Division codes `{client_code}-{operation_type}`; warehouse codes `{client_code}-{SYMBOL}`.
- Mapping rejects cross-client pairs in the service layer and database trigger.
- `user_clients` + ADMIN global access; operational IDOR returns 404.
- Admin UI: Users, Permissions, Client Access, Clients, Divisions, Warehouses, Mapping, Audit.
- Operational modules stubbed behind frozen nav until later phases.

## TESTS RUN

`pytest -q tests/test_v2_phase01_foundation.py` against `TEST_DATABASE_URL`.

## TEST RESULTS

- passed: 12 (P1-01 … P1-12)
- failed: 0
- skipped: 0

## INTEGRITY CHECKS

- Concurrent client creates produce distinct monotonic sequence numbers.
- Division/warehouse codes stay on the creating client.
- Same warehouse symbol allowed across clients (`01-CEL-NY`, `02-DIO-NY`).
- Cross-client `division_warehouses` rejected by service and trigger.

## SECURITY CHECKS

- USER with no `user_clients` sees no client data.
- USER with client A cannot GET/POST client B (404).
- Missing `CLIENTS_CREATE` → 403 on create routes.
- `/health` JSON contains no credentials.

## KNOWN ISSUES

None P0/P1. Operational inventory/order/processing routes are placeholders until later phases.

## GIT BRANCH

`cursor/wms-v2-rebuild-6b85`

## HEAD SHA

(recorded at commit time)

## NEXT PHASE

AUTHORIZED — Phase 2 Inventory
