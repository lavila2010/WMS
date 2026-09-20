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
3. **Orders** — browse/import `Orders.xlsx`, validate orders.
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

## Database tables

`inventory_units`, `orders`, `order_lines`, `allocations`, `boxes`,
`box_contents`, `transactions`, `inventory_movements`, `order_exceptions`,
`documents`, `import_batches`.

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
