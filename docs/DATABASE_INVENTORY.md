# WMS PostgreSQL Database Inventory

Generated: `2026-09-20T06:32:11Z` (UTC)

Inspection mode: **read-only**. No schema changes, no seeds, no resets, no data modification.
Credentials, `DATABASE_URL`, and password hashes are omitted.

**Final status: `DATABASE_SCHEMA_PARTIAL`**

## Executive summary

The Flask WMS schema after `flask --app wsgi init-db` (`db.create_all()` + `seed_permissions()`), plus additive patches from `ensure_processing_columns()` and `ensure_kpi_schema()` on app startup, is **operationally complete** for the current WMS:

- Live database `wms` on PostgreSQL 16, schema `public`.
- **23** SQLAlchemy models / WMS tables, all present in PostgreSQL.
- **0** expected WMS tables missing.
- Live relation count is **26** because **3 leftover Prisma tables** remain from the reverted Next.js experiment (`Item`, `Location`, `_prisma_migrations`).
- Inventory is scoped **Client + Warehouse only**. `inventory_units.order_type_id` exists but is **nullable and deprecated**.
- Processing lock columns, `orders.closed_at`, carton fields, `divisions`, `orders.division_id`, `order_types.channel`, KPI indexes, and RBAC tables all exist.
- Admin `leandro` exists, role `ADMIN`, active, `must_change_password=false`, effective permission count 25 (all catalog codes). Password hash not disclosed.

Why PARTIAL rather than PASS:

1. Leftover unused Prisma tables in this database.
2. `orders.processing_user_id` is present but **lacks a live FOREIGN KEY** to `users.id` (additive ALTER added a bare INTEGER).
3. `boxes.reweigh_required` is nullable in PostgreSQL (DEFAULT false) while the model is NOT NULL.

These do not block current WMS behavior. The lock is still enforced by atomic `UPDATE … WHERE processing_user_id IS NULL`. They are cleanup / hardening items, not missing core schema.

## 1. Database connection status

| Item | Value |
|---|---|
| Connection | **succeeds** |
| Database name | `wms` |
| PostgreSQL version | PostgreSQL 16.15 (Ubuntu 16.15-0ubuntu0.24.04.1) on x86_64-pc-linux-gnu, compiled by gcc (Ubuntu 13.3.0-6ubuntu2~24.04.1) 13.3.0, 64-bit |
| Current schema | `public` |
| Host class | `local` (not a connection URI) |
| Credentials / URI | **omitted** |

`flask --app wsgi init-db` creates tables via SQLAlchemy `create_all()` and seeds the permissions catalog. App startup then applies additive patches (`ensure_processing_columns`, `ensure_kpi_schema`) so existing databases pick up processing-lock, carton, division, and channel columns without dropping data.

## 2–3. Table and column inventory

### WMS tables

### `users`

Authenticated operators; RBAC identity (ADMIN/USER).

- Row count: **2**
- Primary key: `id` (`users_pkey`)
- Foreign keys:
  - `users_created_by_user_id_fkey`: `created_by_user_id` → `users.id`
  - `users_updated_by_user_id_fkey`: `updated_by_user_id` → `users.id`
- Unique constraints / unique indexes:
  - `ix_users_username` unique index (`username`)
- Non-unique indexes: none

| column | type | nullable | default | PK | FK |
|---|---|---|---|---|---|
| `id` | `INTEGER` | NO | nextval('"public".users_id_seq'::regclass) | yes | — |
| `username` | `VARCHAR(64)` | NO | — | no | — |
| `password_hash` | `VARCHAR(255)` | NO | — | no | — |
| `full_name` | `VARCHAR(255)` | YES | — | no | — |
| `email` | `VARCHAR(255)` | YES | — | no | — |
| `role` | `VARCHAR(16)` | NO | — | no | — |
| `active` | `BOOLEAN` | NO | — | no | — |
| `must_change_password` | `BOOLEAN` | YES | — | no | — |
| `failed_login_count` | `INTEGER` | YES | — | no | — |
| `locked_until` | `TIMESTAMP` | YES | — | no | — |
| `last_login_at` | `TIMESTAMP` | YES | — | no | — |
| `created_at` | `TIMESTAMP` | NO | — | no | — |
| `created_by_user_id` | `INTEGER` | YES | — | no | `users.id` |
| `updated_at` | `TIMESTAMP` | YES | — | no | — |
| `updated_by_user_id` | `INTEGER` | YES | — | no | `users.id` |

### `permissions`

Canonical permission catalog (code/module).

- Row count: **25**
- Primary key: `id` (`permissions_pkey`)
- Foreign keys: none
- Unique constraints / unique indexes:
  - `ix_permissions_code` unique index (`code`)
- Non-unique indexes: none

| column | type | nullable | default | PK | FK |
|---|---|---|---|---|---|
| `id` | `INTEGER` | NO | nextval('"public".permissions_id_seq'::regclass) | yes | — |
| `code` | `VARCHAR(64)` | NO | — | no | — |
| `description` | `VARCHAR(255)` | YES | — | no | — |
| `module` | `VARCHAR(64)` | YES | — | no | — |

### `user_permissions`

Per-user granted permissions (ADMIN bypasses this).

- Row count: **9**
- Primary key: `id` (`user_permissions_pkey`)
- Foreign keys:
  - `user_permissions_created_by_user_id_fkey`: `created_by_user_id` → `users.id`
  - `user_permissions_permission_id_fkey`: `permission_id` → `permissions.id`
  - `user_permissions_user_id_fkey`: `user_id` → `users.id`
- Unique constraints / unique indexes:
  - `uq_user_permission` (`user_id, permission_id`)
- Non-unique indexes: none

| column | type | nullable | default | PK | FK |
|---|---|---|---|---|---|
| `id` | `INTEGER` | NO | nextval('"public".user_permissions_id_seq'::regclass) | yes | — |
| `user_id` | `INTEGER` | NO | — | no | `users.id` |
| `permission_id` | `INTEGER` | NO | — | no | `permissions.id` |
| `granted` | `BOOLEAN` | NO | — | no | — |
| `created_at` | `TIMESTAMP` | NO | — | no | — |
| `created_by_user_id` | `INTEGER` | YES | — | no | `users.id` |

### `audit_events`

Append-only human-action audit log.

- Row count: **35**
- Primary key: `id` (`audit_events_pkey`)
- Foreign keys:
  - `audit_events_user_id_fkey`: `user_id` → `users.id`
- Unique constraints: none
- Indexes:
  - `ix_audit_events_created_at` (`created_at`)
  - `ix_audit_events_event_type` (`event_type`)

| column | type | nullable | default | PK | FK |
|---|---|---|---|---|---|
| `id` | `INTEGER` | NO | nextval('"public".audit_events_id_seq'::regclass) | yes | — |
| `user_id` | `INTEGER` | YES | — | no | `users.id` |
| `username` | `VARCHAR(64)` | YES | — | no | — |
| `event_type` | `VARCHAR(48)` | NO | — | no | — |
| `module` | `VARCHAR(48)` | YES | — | no | — |
| `entity_type` | `VARCHAR(48)` | YES | — | no | — |
| `entity_id` | `VARCHAR(64)` | YES | — | no | — |
| `ip_address` | `VARCHAR(64)` | YES | — | no | — |
| `detail` | `TEXT` | YES | — | no | — |
| `created_at` | `TIMESTAMP` | NO | — | no | — |

### `clients`

Commercial clients that own warehouses, divisions, and orders.

- Row count: **2**
- Primary key: `id` (`clients_pkey`)
- Foreign keys: none
- Unique constraints / unique indexes:
  - `ix_clients_code` unique index (`code`)
- Non-unique indexes: none

| column | type | nullable | default | PK | FK |
|---|---|---|---|---|---|
| `id` | `INTEGER` | NO | nextval('"public".clients_id_seq'::regclass) | yes | — |
| `code` | `VARCHAR(32)` | NO | — | no | — |
| `name` | `VARCHAR(255)` | NO | — | no | — |
| `active` | `BOOLEAN` | NO | — | no | — |
| `created_at` | `TIMESTAMP` | NO | — | no | — |

### `warehouses`

Physical warehouses scoped to a client.

- Row count: **3**
- Primary key: `id` (`warehouses_pkey`)
- Foreign keys:
  - `warehouses_client_id_fkey`: `client_id` → `clients.id`
- Unique constraints / unique indexes:
  - `uq_warehouse_client_code` (`client_id, code`)
- Indexes:
  - `ix_warehouses_code` (`code`)

| column | type | nullable | default | PK | FK |
|---|---|---|---|---|---|
| `id` | `INTEGER` | NO | nextval('"public".warehouses_id_seq'::regclass) | yes | — |
| `client_id` | `INTEGER` | NO | — | no | `clients.id` |
| `code` | `VARCHAR(32)` | NO | — | no | — |
| `name` | `VARCHAR(255)` | NO | — | no | — |
| `active` | `BOOLEAN` | NO | — | no | — |
| `created_at` | `TIMESTAMP` | NO | — | no | — |

### `divisions`

Client-scoped commercial division (order attribute, not warehouse).

- Row count: **5**
- Primary key: `id` (`divisions_pkey`)
- Foreign keys:
  - `divisions_client_id_fkey`: `client_id` → `clients.id`
- Unique constraints / unique indexes:
  - `uq_division_client_code` (`client_id, code`)
- Indexes:
  - `ix_divisions_client_id` (`client_id`)
  - `ix_divisions_code` (`code`)

| column | type | nullable | default | PK | FK |
|---|---|---|---|---|---|
| `id` | `INTEGER` | NO | nextval('"public".divisions_id_seq'::regclass) | yes | — |
| `client_id` | `INTEGER` | NO | — | no | `clients.id` |
| `code` | `VARCHAR(32)` | NO | — | no | — |
| `name` | `VARCHAR(255)` | NO | — | no | — |
| `active` | `BOOLEAN` | NO | true | no | — |
| `created_at` | `TIMESTAMP` | NO | now() | no | — |

### `order_types`

Order channel/type per client (ECOMMERCE/RETAIL/WHOLESALE).

- Row count: **7**
- Primary key: `id` (`order_types_pkey`)
- Foreign keys:
  - `order_types_client_id_fkey`: `client_id` → `clients.id`
- Unique constraints / unique indexes:
  - `uq_order_type_client_code` (`client_id, code`)
- Indexes:
  - `ix_order_types_channel` (`channel`)
  - `ix_order_types_code` (`code`)

| column | type | nullable | default | PK | FK |
|---|---|---|---|---|---|
| `id` | `INTEGER` | NO | nextval('"public".order_types_id_seq'::regclass) | yes | — |
| `client_id` | `INTEGER` | NO | — | no | `clients.id` |
| `code` | `VARCHAR(32)` | NO | — | no | — |
| `name` | `VARCHAR(255)` | NO | — | no | — |
| `active` | `BOOLEAN` | NO | — | no | — |
| `created_at` | `TIMESTAMP` | NO | — | no | — |
| `channel` | `VARCHAR(16)` | YES | — | no | — |

### `orders`

Customer orders; uniqueness Client+Warehouse+OrderType+OrderNumber.

- Row count: **13**
- Primary key: `id` (`orders_pkey`)
- Foreign keys:
  - `orders_client_id_fkey`: `client_id` → `clients.id`
  - `orders_closed_by_user_id_fkey`: `closed_by_user_id` → `users.id`
  - `orders_division_id_fkey`: `division_id` → `divisions.id`
  - `orders_import_batch_id_fkey`: `import_batch_id` → `import_batches.id`
  - `orders_order_type_id_fkey`: `order_type_id` → `order_types.id`
  - `orders_validated_by_user_id_fkey`: `validated_by_user_id` → `users.id`
  - `orders_warehouse_id_fkey`: `warehouse_id` → `warehouses.id`
- Unique constraints / unique indexes:
  - `uq_order_scope_number` (`client_id, warehouse_id, order_type_id, order_number`)
- Indexes:
  - `ix_orders_client_id` (`client_id`)
  - `ix_orders_created_at` (`created_at`)
  - `ix_orders_division_id` (`division_id`)
  - `ix_orders_order_number` (`order_number`)
  - `ix_orders_order_type_id` (`order_type_id`)
  - `ix_orders_processing_lock_id` (`processing_lock_id`)
  - `ix_orders_status` (`status`)
  - `ix_orders_warehouse_id` (`warehouse_id`)

| column | type | nullable | default | PK | FK |
|---|---|---|---|---|---|
| `id` | `INTEGER` | NO | nextval('"public".orders_id_seq'::regclass) | yes | — |
| `order_number` | `VARCHAR(64)` | NO | — | no | — |
| `customer` | `VARCHAR(255)` | YES | — | no | — |
| `carrier` | `VARCHAR(128)` | YES | — | no | — |
| `shipping_service` | `VARCHAR(128)` | YES | — | no | — |
| `status` | `VARCHAR(20)` | NO | — | no | — |
| `client_id` | `INTEGER` | NO | — | no | `clients.id` |
| `warehouse_id` | `INTEGER` | NO | — | no | `warehouses.id` |
| `order_type_id` | `INTEGER` | NO | — | no | `order_types.id` |
| `notes` | `TEXT` | YES | — | no | — |
| `import_batch_id` | `INTEGER` | YES | — | no | `import_batches.id` |
| `validated_by_user_id` | `INTEGER` | YES | — | no | `users.id` |
| `validated_by_username` | `VARCHAR(64)` | YES | — | no | — |
| `closed_by_user_id` | `INTEGER` | YES | — | no | `users.id` |
| `closed_by_username` | `VARCHAR(64)` | YES | — | no | — |
| `created_at` | `TIMESTAMP` | NO | — | no | — |
| `updated_at` | `TIMESTAMP` | NO | — | no | — |
| `closed_at` | `TIMESTAMP` | YES | — | no | — |
| `processing_user_id` | `INTEGER` | YES | — | no | — |
| `processing_username` | `VARCHAR(64)` | YES | — | no | — |
| `processing_started_at` | `TIMESTAMP` | YES | — | no | — |
| `division_id` | `INTEGER` | YES | — | no | `divisions.id` |
| `processing_lock_id` | `VARCHAR(64)` | YES | — | no | — |

### `order_lines`

Ordered SKU quantities on an order.

- Row count: **15**
- Primary key: `id` (`order_lines_pkey`)
- Foreign keys:
  - `order_lines_order_id_fkey`: `order_id` → `orders.id`
- Unique constraints: none
- Indexes:
  - `ix_order_lines_order_id` (`order_id`)

| column | type | nullable | default | PK | FK |
|---|---|---|---|---|---|
| `id` | `INTEGER` | NO | nextval('"public".order_lines_id_seq'::regclass) | yes | — |
| `order_id` | `INTEGER` | NO | — | no | `orders.id` |
| `sku` | `VARCHAR(64)` | NO | — | no | — |
| `description` | `VARCHAR(255)` | YES | — | no | — |
| `quantity` | `INTEGER` | NO | — | no | — |

### `inventory_units`

Physical units identified by unique barcode; scoped Client+Warehouse.

- Row count: **15**
- Primary key: `id` (`inventory_units_pkey`)
- Foreign keys:
  - `inventory_units_client_id_fkey`: `client_id` → `clients.id`
  - `inventory_units_import_batch_id_fkey`: `import_batch_id` → `import_batches.id`
  - `inventory_units_order_id_fkey`: `order_id` → `orders.id`
  - `inventory_units_order_type_id_fkey`: `order_type_id` → `order_types.id`
  - `inventory_units_warehouse_id_fkey`: `warehouse_id` → `warehouses.id`
- Unique constraints / unique indexes:
  - `ix_inventory_units_barcode` unique index (`barcode`)
- Indexes:
  - `ix_inventory_units_client_id` (`client_id`)
  - `ix_inventory_units_cwl` (`client_id, warehouse_id, location`)
  - `ix_inventory_units_cws` (`client_id, warehouse_id, sku`)
  - `ix_inventory_units_cwu` (`client_id, warehouse_id, upc`)
  - `ix_inventory_units_sku` (`sku`)
  - `ix_inventory_units_upc` (`upc`)
  - `ix_inventory_units_warehouse_id` (`warehouse_id`)

| column | type | nullable | default | PK | FK |
|---|---|---|---|---|---|
| `id` | `INTEGER` | NO | nextval('"public".inventory_units_id_seq'::regclass) | yes | — |
| `barcode` | `VARCHAR(128)` | NO | — | no | — |
| `upc` | `VARCHAR(64)` | NO | — | no | — |
| `sku` | `VARCHAR(64)` | NO | — | no | — |
| `description` | `VARCHAR(255)` | YES | — | no | — |
| `location` | `VARCHAR(64)` | NO | — | no | — |
| `status` | `VARCHAR(20)` | NO | — | no | — |
| `client_id` | `INTEGER` | NO | — | no | `clients.id` |
| `warehouse_id` | `INTEGER` | NO | — | no | `warehouses.id` |
| `order_type_id` | `INTEGER` | YES | — | no | `order_types.id` |
| `order_id` | `INTEGER` | YES | — | no | `orders.id` |
| `import_batch_id` | `INTEGER` | YES | — | no | `import_batches.id` |
| `created_at` | `TIMESTAMP` | NO | — | no | — |
| `updated_at` | `TIMESTAMP` | NO | — | no | — |

### `allocations`

Barcode-level assignment of an inventory unit to an order.

- Row count: **12**
- Primary key: `id` (`allocations_pkey`)
- Foreign keys:
  - `allocations_inventory_unit_id_fkey`: `inventory_unit_id` → `inventory_units.id`
  - `allocations_order_id_fkey`: `order_id` → `orders.id`
  - `allocations_order_line_id_fkey`: `order_line_id` → `order_lines.id`
- Unique constraints: none
- Indexes:
  - `ix_allocations_barcode` (`barcode`)
  - `ix_allocations_unit_status` (`inventory_unit_id, status`)

| column | type | nullable | default | PK | FK |
|---|---|---|---|---|---|
| `id` | `INTEGER` | NO | nextval('"public".allocations_id_seq'::regclass) | yes | — |
| `order_id` | `INTEGER` | NO | — | no | `orders.id` |
| `order_line_id` | `INTEGER` | YES | — | no | `order_lines.id` |
| `inventory_unit_id` | `INTEGER` | NO | — | no | `inventory_units.id` |
| `barcode` | `VARCHAR(128)` | NO | — | no | — |
| `status` | `VARCHAR(20)` | NO | — | no | — |
| `created_at` | `TIMESTAMP` | NO | — | no | — |

### `pick_tickets`

Permanent pick-ticket identity; one per fully allocated order.

- Row count: **3**
- Primary key: `id` (`pick_tickets_pkey`)
- Foreign keys:
  - `pick_tickets_assigned_by_user_id_fkey`: `assigned_by_user_id` → `users.id`
  - `pick_tickets_document_id_fkey`: `document_id` → `documents.id`
  - `pick_tickets_order_id_fkey`: `order_id` → `orders.id`
- Unique constraints / unique indexes:
  - `pick_tickets_order_id_key` (`order_id`)
  - `ix_pick_tickets_pick_ticket_number` unique index (`pick_ticket_number`)
- Non-unique indexes: none

| column | type | nullable | default | PK | FK |
|---|---|---|---|---|---|
| `id` | `INTEGER` | NO | nextval('"public".pick_tickets_id_seq'::regclass) | yes | — |
| `pick_ticket_number` | `VARCHAR(64)` | NO | — | no | — |
| `order_id` | `INTEGER` | NO | — | no | `orders.id` |
| `status` | `VARCHAR(20)` | NO | — | no | — |
| `assigned_at` | `TIMESTAMP` | NO | — | no | — |
| `assigned_by_user_id` | `INTEGER` | YES | — | no | `users.id` |
| `assigned_by_username` | `VARCHAR(64)` | YES | — | no | — |
| `document_id` | `INTEGER` | YES | — | no | `documents.id` |
| `created_at` | `TIMESTAMP` | NO | — | no | — |
| `updated_at` | `TIMESTAMP` | NO | — | no | — |

### `pick_ticket_print_events`

Print/reprint history for pick tickets.

- Row count: **1**
- Primary key: `id` (`pick_ticket_print_events_pkey`)
- Foreign keys:
  - `pick_ticket_print_events_pick_ticket_id_fkey`: `pick_ticket_id` → `pick_tickets.id`
  - `pick_ticket_print_events_user_id_fkey`: `user_id` → `users.id`
- Unique constraints: none
- Indexes:
  - `ix_pick_ticket_print_events_pick_ticket_id` (`pick_ticket_id`)
  - `ix_pick_ticket_print_events_printed_at` (`printed_at`)

| column | type | nullable | default | PK | FK |
|---|---|---|---|---|---|
| `id` | `INTEGER` | NO | nextval('"public".pick_ticket_print_events_id_seq'::regclass) | yes | — |
| `pick_ticket_id` | `INTEGER` | NO | — | no | `pick_tickets.id` |
| `user_id` | `INTEGER` | YES | — | no | `users.id` |
| `username` | `VARCHAR(64)` | YES | — | no | — |
| `printed_at` | `TIMESTAMP` | NO | — | no | — |
| `source` | `VARCHAR(20)` | NO | — | no | — |
| `ip_address` | `VARCHAR(64)` | YES | — | no | — |

### `boxes`

Cartons packed during order processing.

- Row count: **5**
- Primary key: `id` (`boxes_pkey`)
- Foreign keys:
  - `boxes_closed_by_user_id_fkey`: `closed_by_user_id` → `users.id`
  - `boxes_created_by_user_id_fkey`: `created_by_user_id` → `users.id`
  - `boxes_order_id_fkey`: `order_id` → `orders.id`
- Unique constraints: none
- Non-unique indexes: none

| column | type | nullable | default | PK | FK |
|---|---|---|---|---|---|
| `id` | `INTEGER` | NO | nextval('"public".boxes_id_seq'::regclass) | yes | — |
| `order_id` | `INTEGER` | NO | — | no | `orders.id` |
| `box_number` | `VARCHAR(64)` | NO | — | no | — |
| `length_cm` | `DOUBLE PRECISION` | YES | — | no | — |
| `width_cm` | `DOUBLE PRECISION` | YES | — | no | — |
| `height_cm` | `DOUBLE PRECISION` | YES | — | no | — |
| `weight_kg` | `DOUBLE PRECISION` | YES | — | no | — |
| `status` | `VARCHAR(20)` | NO | — | no | — |
| `created_by_user_id` | `INTEGER` | YES | — | no | `users.id` |
| `created_by_username` | `VARCHAR(64)` | YES | — | no | — |
| `closed_by_user_id` | `INTEGER` | YES | — | no | `users.id` |
| `closed_by_username` | `VARCHAR(64)` | YES | — | no | — |
| `created_at` | `TIMESTAMP` | NO | — | no | — |
| `closed_at` | `TIMESTAMP` | YES | — | no | — |
| `dimension_unit` | `VARCHAR(8)` | YES | 'in'::character varying | no | — |
| `weight_unit` | `VARCHAR(8)` | YES | 'lb'::character varying | no | — |
| `reweigh_required` | `BOOLEAN` | YES | false | no | — |

### `box_contents`

Units packed into a carton (one unit in at most one box).

- Row count: **7**
- Primary key: `id` (`box_contents_pkey`)
- Foreign keys:
  - `box_contents_box_id_fkey`: `box_id` → `boxes.id`
  - `box_contents_inventory_unit_id_fkey`: `inventory_unit_id` → `inventory_units.id`
- Unique constraints / unique indexes:
  - `uq_box_contents_unit` (`inventory_unit_id`)
- Indexes:
  - `ix_box_contents_barcode` (`barcode`)

| column | type | nullable | default | PK | FK |
|---|---|---|---|---|---|
| `id` | `INTEGER` | NO | nextval('"public".box_contents_id_seq'::regclass) | yes | — |
| `box_id` | `INTEGER` | NO | — | no | `boxes.id` |
| `inventory_unit_id` | `INTEGER` | NO | — | no | `inventory_units.id` |
| `barcode` | `VARCHAR(128)` | NO | — | no | — |
| `created_at` | `TIMESTAMP` | NO | — | no | — |

### `documents`

Generated PDFs (pick tickets, packing, closure).

- Row count: **2**
- Primary key: `id` (`documents_pkey`)
- Foreign keys:
  - `documents_box_id_fkey`: `box_id` → `boxes.id`
  - `documents_created_by_user_id_fkey`: `created_by_user_id` → `users.id`
  - `documents_order_id_fkey`: `order_id` → `orders.id`
- Unique constraints: none
- Non-unique indexes: none

| column | type | nullable | default | PK | FK |
|---|---|---|---|---|---|
| `id` | `INTEGER` | NO | nextval('"public".documents_id_seq'::regclass) | yes | — |
| `order_id` | `INTEGER` | YES | — | no | `orders.id` |
| `box_id` | `INTEGER` | YES | — | no | `boxes.id` |
| `type` | `VARCHAR(48)` | NO | — | no | — |
| `filename` | `VARCHAR(255)` | NO | — | no | — |
| `path` | `VARCHAR(512)` | NO | — | no | — |
| `created_by_user_id` | `INTEGER` | YES | — | no | `users.id` |
| `created_by_username` | `VARCHAR(64)` | YES | — | no | — |
| `created_at` | `TIMESTAMP` | NO | — | no | — |

### `import_batches`

Inventory and order import history.

- Row count: **2**
- Primary key: `id` (`import_batches_pkey`)
- Foreign keys:
  - `import_batches_created_by_user_id_fkey`: `created_by_user_id` → `users.id`
- Unique constraints: none
- Non-unique indexes: none

| column | type | nullable | default | PK | FK |
|---|---|---|---|---|---|
| `id` | `INTEGER` | NO | nextval('"public".import_batches_id_seq'::regclass) | yes | — |
| `type` | `VARCHAR(20)` | NO | — | no | — |
| `filename` | `VARCHAR(255)` | NO | — | no | — |
| `row_count` | `INTEGER` | NO | — | no | — |
| `rows_submitted` | `INTEGER` | NO | — | no | — |
| `rows_imported` | `INTEGER` | NO | — | no | — |
| `rows_updated` | `INTEGER` | NO | — | no | — |
| `rows_rejected` | `INTEGER` | NO | — | no | — |
| `warnings_count` | `INTEGER` | NO | — | no | — |
| `clients` | `VARCHAR(512)` | YES | — | no | — |
| `warehouses` | `VARCHAR(512)` | YES | — | no | — |
| `status` | `VARCHAR(20)` | NO | — | no | — |
| `message` | `TEXT` | YES | — | no | — |
| `detail` | `TEXT` | YES | — | no | — |
| `created_by` | `VARCHAR(128)` | YES | — | no | — |
| `created_by_user_id` | `INTEGER` | YES | — | no | `users.id` |
| `created_by_username` | `VARCHAR(64)` | YES | — | no | — |
| `created_at` | `TIMESTAMP` | NO | — | no | — |

### `order_exceptions`

Order-processing exceptions.

- Row count: **0**
- Primary key: `id` (`order_exceptions_pkey`)
- Foreign keys:
  - `order_exceptions_created_by_user_id_fkey`: `created_by_user_id` → `users.id`
  - `order_exceptions_order_id_fkey`: `order_id` → `orders.id`
- Unique constraints: none
- Non-unique indexes: none

| column | type | nullable | default | PK | FK |
|---|---|---|---|---|---|
| `id` | `INTEGER` | NO | nextval('"public".order_exceptions_id_seq'::regclass) | yes | — |
| `order_id` | `INTEGER` | YES | — | no | `orders.id` |
| `barcode` | `VARCHAR(128)` | YES | — | no | — |
| `type` | `VARCHAR(48)` | NO | — | no | — |
| `message` | `TEXT` | YES | — | no | — |
| `resolved` | `BOOLEAN` | NO | — | no | — |
| `created_by_user_id` | `INTEGER` | YES | — | no | `users.id` |
| `created_by_username` | `VARCHAR(64)` | YES | — | no | — |
| `created_at` | `TIMESTAMP` | NO | — | no | — |

### `inventory_exceptions`

Inventory-import/control exceptions (Client+Warehouse).

- Row count: **1**
- Primary key: `id` (`inventory_exceptions_pkey`)
- Foreign keys:
  - `inventory_exceptions_client_id_fkey`: `client_id` → `clients.id`
  - `inventory_exceptions_warehouse_id_fkey`: `warehouse_id` → `warehouses.id`
- Unique constraints: none
- Non-unique indexes: none

| column | type | nullable | default | PK | FK |
|---|---|---|---|---|---|
| `id` | `INTEGER` | NO | nextval('"public".inventory_exceptions_id_seq'::regclass) | yes | — |
| `type` | `VARCHAR(48)` | NO | — | no | — |
| `barcode` | `VARCHAR(128)` | YES | — | no | — |
| `upc` | `VARCHAR(64)` | YES | — | no | — |
| `client_id` | `INTEGER` | YES | — | no | `clients.id` |
| `warehouse_id` | `INTEGER` | YES | — | no | `warehouses.id` |
| `client_code` | `VARCHAR(32)` | YES | — | no | — |
| `warehouse_code` | `VARCHAR(32)` | YES | — | no | — |
| `details` | `TEXT` | YES | — | no | — |
| `status` | `VARCHAR(20)` | NO | — | no | — |
| `created_at` | `TIMESTAMP` | NO | — | no | — |

### `invoices`

One invoice per closed order.

- Row count: **1**
- Primary key: `id` (`invoices_pkey`)
- Foreign keys:
  - `invoices_client_id_fkey`: `client_id` → `clients.id`
  - `invoices_created_by_user_id_fkey`: `created_by_user_id` → `users.id`
  - `invoices_order_id_fkey`: `order_id` → `orders.id`
  - `invoices_order_type_id_fkey`: `order_type_id` → `order_types.id`
  - `invoices_warehouse_id_fkey`: `warehouse_id` → `warehouses.id`
- Unique constraints / unique indexes:
  - `uq_invoice_order` (`order_id`)
  - `ix_invoices_invoice_number` unique index (`invoice_number`)
- Non-unique indexes: none

| column | type | nullable | default | PK | FK |
|---|---|---|---|---|---|
| `id` | `INTEGER` | NO | nextval('"public".invoices_id_seq'::regclass) | yes | — |
| `invoice_number` | `VARCHAR(64)` | NO | — | no | — |
| `order_id` | `INTEGER` | NO | — | no | `orders.id` |
| `client_id` | `INTEGER` | NO | — | no | `clients.id` |
| `warehouse_id` | `INTEGER` | NO | — | no | `warehouses.id` |
| `order_type_id` | `INTEGER` | NO | — | no | `order_types.id` |
| `customer` | `VARCHAR(255)` | YES | — | no | — |
| `carrier` | `VARCHAR(128)` | YES | — | no | — |
| `shipping_service` | `VARCHAR(128)` | YES | — | no | — |
| `total_units` | `INTEGER` | NO | — | no | — |
| `total_boxes` | `INTEGER` | NO | — | no | — |
| `total_weight` | `DOUBLE PRECISION` | NO | — | no | — |
| `status` | `VARCHAR(20)` | NO | — | no | — |
| `created_at` | `TIMESTAMP` | NO | — | no | — |
| `created_by` | `VARCHAR(128)` | YES | — | no | — |
| `created_by_user_id` | `INTEGER` | YES | — | no | `users.id` |
| `created_by_username` | `VARCHAR(64)` | YES | — | no | — |

### `transactions`

Append-only operational event ledger.

- Row count: **77**
- Primary key: `id` (`transactions_pkey`)
- Foreign keys:
  - `transactions_inventory_unit_id_fkey`: `inventory_unit_id` → `inventory_units.id`
  - `transactions_order_id_fkey`: `order_id` → `orders.id`
  - `transactions_user_id_fkey`: `user_id` → `users.id`
- Unique constraints: none
- Non-unique indexes: none

| column | type | nullable | default | PK | FK |
|---|---|---|---|---|---|
| `id` | `INTEGER` | NO | nextval('"public".transactions_id_seq'::regclass) | yes | — |
| `type` | `VARCHAR(48)` | NO | — | no | — |
| `order_id` | `INTEGER` | YES | — | no | `orders.id` |
| `inventory_unit_id` | `INTEGER` | YES | — | no | `inventory_units.id` |
| `barcode` | `VARCHAR(128)` | YES | — | no | — |
| `quantity` | `INTEGER` | YES | — | no | — |
| `detail` | `TEXT` | YES | — | no | — |
| `user_id` | `INTEGER` | YES | — | no | `users.id` |
| `username` | `VARCHAR(64)` | YES | — | no | — |
| `created_at` | `TIMESTAMP` | NO | — | no | — |

### `inventory_movements`

Inventory status/location movement history.

- Row count: **35**
- Primary key: `id` (`inventory_movements_pkey`)
- Foreign keys:
  - `inventory_movements_client_id_fkey`: `client_id` → `clients.id`
  - `inventory_movements_created_by_user_id_fkey`: `created_by_user_id` → `users.id`
  - `inventory_movements_inventory_unit_id_fkey`: `inventory_unit_id` → `inventory_units.id`
  - `inventory_movements_order_id_fkey`: `order_id` → `orders.id`
  - `inventory_movements_warehouse_id_fkey`: `warehouse_id` → `warehouses.id`
- Unique constraints: none
- Indexes:
  - `ix_inventory_movements_barcode` (`barcode`)
  - `ix_inventory_movements_client_id` (`client_id`)
  - `ix_inventory_movements_warehouse_id` (`warehouse_id`)

| column | type | nullable | default | PK | FK |
|---|---|---|---|---|---|
| `id` | `INTEGER` | NO | nextval('"public".inventory_movements_id_seq'::regclass) | yes | — |
| `inventory_unit_id` | `INTEGER` | NO | — | no | `inventory_units.id` |
| `barcode` | `VARCHAR(128)` | YES | — | no | — |
| `upc` | `VARCHAR(64)` | YES | — | no | — |
| `client_id` | `INTEGER` | YES | — | no | `clients.id` |
| `warehouse_id` | `INTEGER` | YES | — | no | `warehouses.id` |
| `movement_type` | `VARCHAR(48)` | YES | — | no | — |
| `from_status` | `VARCHAR(20)` | YES | — | no | — |
| `to_status` | `VARCHAR(20)` | YES | — | no | — |
| `from_location` | `VARCHAR(64)` | YES | — | no | — |
| `to_location` | `VARCHAR(64)` | YES | — | no | — |
| `order_id` | `INTEGER` | YES | — | no | `orders.id` |
| `actor` | `VARCHAR(128)` | YES | — | no | — |
| `created_by_user_id` | `INTEGER` | YES | — | no | `users.id` |
| `created_by_username` | `VARCHAR(64)` | YES | — | no | — |
| `reason` | `VARCHAR(255)` | YES | — | no | — |
| `created_at` | `TIMESTAMP` | NO | — | no | — |

### Leftover non-WMS tables (present in DB, not in models)

### `Item`

**NOT a WMS table.** Leftover from the reverted Next.js/Prisma experiment. The Flask WMS does not read or write this table.

- Row count: **4**
- Primary key: `id` (`Item_pkey`)
- Foreign keys:
  - `Item_locationId_fkey`: `locationId` → `Location.id`
- Unique constraints / unique indexes:
  - `Item_sku_key` unique index (`sku`)
- Non-unique indexes: none

| column | type | nullable | default | PK | FK |
|---|---|---|---|---|---|
| `id` | `TEXT` | NO | — | yes | — |
| `sku` | `TEXT` | NO | — | no | — |
| `name` | `TEXT` | NO | — | no | — |
| `description` | `TEXT` | YES | — | no | — |
| `quantity` | `INTEGER` | NO | 0 | no | — |
| `locationId` | `TEXT` | YES | — | no | `Location.id` |
| `createdAt` | `TIMESTAMP` | NO | CURRENT_TIMESTAMP | no | — |
| `updatedAt` | `TIMESTAMP` | NO | — | no | — |

### `Location`

**NOT a WMS table.** Leftover from the reverted Next.js/Prisma experiment. The Flask WMS does not read or write this table.

- Row count: **2**
- Primary key: `id` (`Location_pkey`)
- Foreign keys: none
- Unique constraints / unique indexes:
  - `Location_code_key` unique index (`code`)
- Non-unique indexes: none

| column | type | nullable | default | PK | FK |
|---|---|---|---|---|---|
| `id` | `TEXT` | NO | — | yes | — |
| `code` | `TEXT` | NO | — | no | — |
| `name` | `TEXT` | NO | — | no | — |
| `createdAt` | `TIMESTAMP` | NO | CURRENT_TIMESTAMP | no | — |
| `updatedAt` | `TIMESTAMP` | NO | — | no | — |

### `_prisma_migrations`

**NOT a WMS table.** Leftover from the reverted Next.js/Prisma experiment. The Flask WMS does not read or write this table.

- Row count: **1**
- Primary key: `id` (`_prisma_migrations_pkey`)
- Foreign keys: none
- Unique constraints: none
- Non-unique indexes: none

| column | type | nullable | default | PK | FK |
|---|---|---|---|---|---|
| `id` | `VARCHAR(36)` | NO | — | yes | — |
| `checksum` | `VARCHAR(64)` | NO | — | no | — |
| `finished_at` | `TIMESTAMP` | YES | — | no | — |
| `migration_name` | `VARCHAR(255)` | NO | — | no | — |
| `logs` | `TEXT` | YES | — | no | — |
| `rolled_back_at` | `TIMESTAMP` | YES | — | no | — |
| `started_at` | `TIMESTAMP` | NO | now() | no | — |
| `applied_steps_count` | `INTEGER` | NO | 0 | no | — |

## 4. Relationship map

RBAC: ADMIN users bypass `user_permissions` and receive every code in `permissions`. USER grants are rows in `user_permissions` (`granted=true`).

```
users 1──* user_permissions *──1 permissions
users 1──* audit_events
users 1──* (attribution columns on orders, boxes, documents, invoices, import_batches, …)

clients 1──* warehouses
clients 1──* divisions
clients 1──* order_types
clients 1──* orders
clients 1──* inventory_units
clients 1──* invoices
clients 1──* inventory_exceptions
clients 1──* inventory_movements

warehouses 1──* orders
warehouses 1──* inventory_units

divisions 1──* orders          (orders.division_id, nullable)

order_types 1──* orders        (channel is an ORDER attribute)
order_types 0──* inventory_units.order_type_id   (DEPRECATED, nullable; unused)

orders 1──* order_lines
orders 1──* allocations
orders 1──0..1 pick_tickets
orders 1──* boxes
orders 1──0..1 invoices
orders 1──* documents
orders 1──* order_exceptions
orders 1──* transactions
orders.processing_user_id → users.id   (model FK; live column exists, FK constraint missing)

order_lines 1──* allocations

inventory_units 1──* allocations
inventory_units 1──* box_contents
inventory_units 1──* inventory_movements
inventory_units 1──* transactions
UNIQUE barcode globally

allocations  (barcode-level; unit → order)

pick_tickets 1──* pick_ticket_print_events
pick_tickets.document_id → documents.id

boxes 1──* box_contents
boxes 1──* documents

import_batches 1──* orders.import_batch_id
import_batches 1──* inventory_units.import_batch_id
```


### Expected-table presence

| Expected entity | Table | Status |
|---|---|---|
| users | `users` | present |
| permissions | `permissions` | present |
| user_permissions / role permission structures | `user_permissions` | present |
| clients | `clients` | present |
| warehouses | `warehouses` | present |
| divisions | `divisions` | present |
| order_types | `order_types` | present |
| orders | `orders` | present |
| order_lines | `order_lines` | present |
| inventory units | `inventory_units` | present |
| allocations | `allocations` | present |
| pick tickets | `pick_tickets` | present |
| pick ticket print events | `pick_ticket_print_events` | present |
| cartons / boxes | `boxes` | present |
| carton units / packed units | `box_contents` | present |
| audit events | `audit_events` | present |
| reports/documents | `documents` | present |
| imports / import history | `import_batches` | present |
| order exceptions | `order_exceptions` | present |
| inventory exceptions | `inventory_exceptions` | present |
| invoices | `invoices` | present |
| transactions (ops ledger) | `transactions` | present |
| inventory movements | `inventory_movements` | present |
| Prisma `Item` / `Location` / `_prisma_migrations` | leftover | **present but unused (not WMS)** |

No expected WMS table is MISSING.

## 5. Current data counts

Safe summary counts from the inspected database. Password hashes are not selected or printed.

| Table / metric | Count |
|---|---|
| users | 2 |
| admins (role=ADMIN) | 1 |
| permissions | 25 |
| user_permissions | 9 |
| clients | 2 |
| warehouses | 3 |
| divisions | 5 |
| order types | 7 |
| orders | 13 |
| order lines | 15 |
| inventory units | 15 |
| allocations | 12 |
| pick tickets | 3 |
| pick ticket print events | 1 |
| cartons (boxes) | 5 |
| box contents (packed units) | 7 |
| audit events | 35 |
| documents | 2 |
| import batches | 2 |
| invoices | 1 |
| transactions | 77 |
| inventory movements | 35 |
| order exceptions | 0 |
| inventory exceptions | 1 |
| leftover `Item` (non-WMS) | 4 |
| leftover `Location` (non-WMS) | 2 |
| leftover `_prisma_migrations` (non-WMS) | 1 |

## 6. Admin verification

| Check | Result |
|---|---|
| username `leandro` exists | **True** |
| role | `ADMIN` |
| active | **True** |
| must_change_password | **False** |
| permission count | **25** (ADMIN ⇒ full `permissions` catalog) |
| password hash | **omitted** |

## 7. Schema vs models

| Check | Result |
|---|---|
| Tables in models missing in DB | **none** |
| Tables in DB not in models | `Item`, `Location`, `_prisma_migrations` (leftover Prisma) |
| Columns missing in DB | **none** (all model columns exist) |
| Extra DB columns on WMS tables | **none** |
| Nullable mismatches | `boxes.reweigh_required`: model NOT NULL, DB nullable DEFAULT false |
| Missing foreign keys | `orders.processing_user_id` → `users.id` (column exists, constraint missing) |
| Extra foreign keys | none |
| Missing unique constraints | **none** |
| Missing indexes | **none** for declared WMS indexes (KPI + lock indexes present) |

PostgreSQL implements UNIQUE constraints as unique indexes (`uq_*`). Those are not missing indexes.

SQLAlchemy Python-side defaults without a PostgreSQL `DEFAULT`: **53** columns (typically `created_at` / status literals). `create_all()` does not emit server defaults for Python callables. Inserts from the app still populate them. Not a missing-column defect.

## 8. Critical WMS checks

| Check | Result |
|---|---|
| Inventory scoped Client + Warehouse | **PASS** (`inventory_units.client_id` and `warehouse_id` NOT NULL + FKs) |
| InventoryUnit does NOT require OrderType | **PASS** (`order_type_id` nullable, deprecated, unused by inventory services) |
| `processing_user_id` | **present** |
| `processing_username` | **present** |
| `processing_started_at` | **present** |
| `processing_lock_id` | **present** |
| `orders.closed_at` | **present** |
| `boxes.dimension_unit` | **present** |
| `boxes.weight_unit` | **present** |
| `boxes.reweigh_required` | **present** (nullable mismatch noted above) |
| `divisions` table | **present** |
| `orders.division_id` | **present** |
| `order_types.channel` | **present** |
| KPI indexes (`ix_orders_created_at`, `ix_orders_division_id`, `ix_order_lines_order_id`, `ix_order_types_channel`) | **present** |
| RBAC `users` / `permissions` / `user_permissions` / `audit_events` | **present** |

## Missing items

- **MISSING WMS tables: none.**
- Missing live FK: `orders.processing_user_id` → `users.id`.
- Leftover unused tables: `Item`, `Location`, `_prisma_migrations`.

## Deployment risks

1. **Leftover Prisma objects** in this database can confuse future migrations or `init-db` comparisons. They are unused. Do not drop them in this audit mission; schedule a dedicated, reviewed cleanup if this database is promoted.
2. **`processing_user_id` without FK** cannot enforce referential integrity at the database level if a user row is deleted. Application code still writes the lock atomically.
3. **`reweigh_required` nullable** allows NULL if a raw SQL insert omits the column; Flask-SQLAlchemy inserts send `false` from the model default.
4. **`inventory_units.order_type_id`** remains as a deprecated nullable column. Inventory code must continue to ignore it until a later migration drops it.
5. **`orders.division_id` is nullable** in both model and DB. KPI inner-joins Division; `ensure_kpi_schema` backfills a `MAIN` division so existing orders are counted. New imports assign a default division.
6. Python-only defaults mean a SQL client inserting rows without `created_at` will fail NOT NULL checks. That is acceptable: the app is the writer.

## Recommended next actions

These are **not** part of this audit and must not be applied here:

1. In a future schema-hardening mission: `ALTER TABLE orders ADD CONSTRAINT … FOREIGN KEY (processing_user_id) REFERENCES users(id)` after validating no orphans.
2. In a future schema-hardening mission: `ALTER TABLE boxes ALTER COLUMN reweigh_required SET NOT NULL` after `UPDATE … SET reweigh_required = FALSE WHERE reweigh_required IS NULL`.
3. Separately review leftover Prisma tables for drop **only** after confirming they are unused in every environment.
4. Keep `init-db` + startup patches as the supported way to bring a fresh PostgreSQL up to this schema. Do not merge this branch to `main` as part of this inventory.

## Final status

`DATABASE_SCHEMA_PARTIAL`
