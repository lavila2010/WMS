# WMS — Warehouse Management System

A barcode-driven Warehouse Management System built with **Flask**, **SQLAlchemy**,
and **PostgreSQL** (Aiven in production), rendered with **Jinja templates**.

## Stack

- Python 3, Flask, Flask-SQLAlchemy, SQLAlchemy
- PostgreSQL (Aiven, external `DATABASE_URL`, SSL required) via `psycopg2-binary`
- `pandas` + `openpyxl` for Excel import (Inventory.xlsx, Orders.xlsx only)
- `reportlab` for PDF documents
- `gunicorn` for serving; deployed on **Render**
- Frontend: Flask Jinja templates + HTML/CSS (no JS framework)

## Modules

1. **Dashboard** — inventory + order-status overview and recent exceptions.
2. **Inventory** — browse units, import `Inventory.xlsx`.
3. **Orders** — Order Management, two-step `Orders.xlsx` upload, Pick Tickets, Allocation Report.
4. **Allocation** — barcode-level allocation (scan or manual).
5. **Order Processing** — packing workflow (boxes, scanning, weights, reconciliation).
6. **Order Reports** — Pick Ticket, Packing Report, Order Closure, Box Detail PDFs.

## Order workflow

```
NEW → VALIDATED → ALLOCATING → ALLOCATED → READY_TO_PICK
    → PROCESSING → PROCESSED → READY_TO_CLOSE → CLOSED
```

## Barcode rules

- Every physical unit has a unique barcode.
- Allocation is barcode-level; scan or manual entry use the same path.
- A duplicate scan is rejected.
- A barcode must belong to the selected order (SKU must have remaining demand).
- A barcode cannot belong to two active orders.
- A barcode cannot be in two boxes.

## Packing workflow

Select order → create/select box → scan units into box → close box → enter box
weight → repeat until all units packed → close order after quantity reconciliation.

## Scope model

- **Physical inventory** is partitioned by **Client + Warehouse only**. Order
  Type is not an inventory dimension — an ECOM, RETAIL, or WHOLESALE order for
  the same client/warehouse draws from the same physical pool. (`InventoryUnit`
  keeps a deprecated, nullable `order_type_id` compatibility column that is no
  longer read/written/filtered; a later migration will drop it.)
- **Orders** remain scoped by **Client + Warehouse + Order Type + Order
  Number**. Master tables `clients`, `warehouses`, `order_types` keep clients
  independent. Barcodes are globally unique.

## Orders module

Tabs: **Order Management**, **Upload Orders**, **Pick Tickets**, **Allocation Report**.
A client must be selected before any order data is shown. Upload is a two-step
preview → confirm flow. Allocation matches inventory by **Client + Warehouse +
SKU + AVAILABLE** (not Order Type). Pick Ticket numbers are permanent per order.

## Inventory Control module

Tabbed operational UI (Overview, Upload Inventory, Inventory Search,
Transactions, Import History, Exceptions) with Client/Warehouse selectors.
Inventory upload is a two-step **preview → confirm** flow: preview validates and
summarizes with no DB writes; only explicit confirmation imports atomically.
Inventory Excel columns: **Client, Warehouse, UPC, SKU, Description, Barcode,
Location**.

## Invoicing

Closing an order is atomic: validate quantities → validate all boxes closed →
close order → create exactly one invoice → mark units shipped → record
transactions. If invoice creation fails, the whole close is rolled back.
Invoice numbering (`INV-YYYY-000001`) is isolated in `app/services/invoices.py`
so client-specific schemes can be added later.

## Authentication & RBAC

- Flask-Login session auth. Credentials are entered once at `/login`; the
  authenticated session then persists across all modules until logout,
  expiration, or the account being disabled. Public endpoints: `/login`,
  `/health`, `/health/db`.
- Roles: `ADMIN` (all permissions) and `USER` (granular permissions).
- Pick Ticket permissions: `PICK_TICKET_VIEW`, `PICK_TICKET_GENERATE`,
  `PICK_TICKET_PRINT`. A Pick Ticket Number (`PT-YYYY-000001`) is assigned
  once when an order becomes fully allocated and never changes on reprint.
- Every route is protected server-side with `@permission_required("...")`;
  navigation and action buttons are also permission-driven. Unauthorized
  authenticated actions return HTTP 403 with an Access Denied page.
- Administration module (`/admin/users`) — Users, Permissions, Audit tabs;
  create/edit/enable/disable users, reset passwords, assign permissions.
- Every human action is attributed to the authenticated user (transactions,
  inventory movements, import batches, invoices, boxes, documents, order
  close/validate, exceptions) and recorded in `audit_events`.

Bootstrap the first admin (no hard-coded credentials):

```bash
flask --app wsgi create-admin
```

## Database tables

`users`, `permissions`, `user_permissions`, `audit_events`, `clients`,
`warehouses`, `order_types`, `inventory_units`, `orders`, `order_lines`,
`allocations`, `boxes`, `box_contents`, `invoices`, `transactions`,
`inventory_movements`, `order_exceptions`, `inventory_exceptions`,
`documents`, `import_batches`, `pick_tickets`, `pick_ticket_print_events`.

## Local development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # adjust DATABASE_URL as needed
flask --app wsgi init-db        # create tables
flask --app wsgi run --port 5000
```

The app reads `DATABASE_URL` from the environment. For production set it to the
Aiven PostgreSQL connection string (SSL is enforced automatically for non-local
hosts). For local development it can point at a local PostgreSQL instance.

## Tests

```bash
TEST_DATABASE_URL="postgresql://wms:wms@127.0.0.1:5432/wms_test" \
  .venv/bin/pytest -q
```

## Deployment (Render)

`render.yaml` defines a Python web service that runs
`gunicorn wsgi:app`. Set the `DATABASE_URL` environment variable to the Aiven
connection string in the Render dashboard.
