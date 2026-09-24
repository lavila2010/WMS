# WMS V2 Render Deployment Checklist

Status: **GATE_12_READY_FOR_HUMAN_DEPLOYMENT**

This agent does **not** have Render or Aiven V2 credentials. Do **not** treat this document as a live deploy. Gate 12 stays **blocked** until a human creates the Aiven V2 database, attaches it to a **separate** Render service named `wms-v2`, and a follow-up agent or operator proves the live URL.

Do **not** merge to `main`.
Do **not** delete, reset, or repoint V1 `wms`.
Do **not** run `init-db` as the Gunicorn start command.

Source of truth for this deploy:

- Branch: `cursor/wms-v2-rebuild-6b85`
- Blueprint: `render.yaml` services `wms-v2` (web) and `wms-v2-import-worker` (V1 `wms` remains listed and untouched)
- Web entry: `wsgi:app`
- Worker entry: `python -m app.workers.import_worker` (inventory + orders; `inventory_import_worker` remains an alias)

---

## 1. Aiven V2 database

Create a **new** Aiven PostgreSQL service for V2. Do not reuse the V1 production database.

Required:

1. New Aiven service (example name: `wms-v2-pg`).
2. New database inside that service (example name: `wms_v2`).
3. Application user with DDL rights for the one-time `init-db` bootstrap, then DML for runtime.
4. SSL required (`sslmode=require`).
5. Copy the Aiven URI into Render as `WMS_V2_DATABASE_URL` only.

Forbidden:

- Pointing `WMS_V2_DATABASE_URL` at `localhost` / `127.0.0.1`.
- Reusing V1 `DATABASE_URL`.
- Using `AIVEN_AUDIT_DATABASE_URL` (read-only V1 audit) as the V2 write target.
- Pasting the URI into chat, tickets, or logs.

Production `Config` rejects a missing `WMS_V2_DATABASE_URL`, a local host, and an insecure `SECRET_KEY`. It does not silently fall back to local PostgreSQL.

---

## 2. Exact environment variable names

| Variable | Required | Production value |
|---|---|---|
| `WMS_V2_DATABASE_URL` | **yes** | Aiven V2 URI, SSL. Preferred and required in production. |
| `SECRET_KEY` | **yes** | Unique random value. Empty / `changeme` / `secret` / `dev-insecure-secret-key` rejected. |
| `SESSION_COOKIE_SECURE` | **yes** | `true` (defaults to true when `WMS_ENV=production`) |
| `WMS_TIMEZONE` | **yes** | `America/New_York` |
| `PYTHON_VERSION` | **yes** | `3.12.6` |
| `WMS_ENV` | **yes** | `production` |
| `DATABASE_URL` | no | Do not rely on this in production. Render may still set it; V2 ignores it when `WMS_ENV=production`. |
| `DOCUMENT_STORE` | recommended | `local` until object-storage credentials exist |
| `DOCUMENTS_DIR` | optional | default `instance/documents` |
| `MAX_UPLOAD_MB` | optional | `50` (upload size is not the large-import bottleneck) |
| `WTF_CSRF_TIME_LIMIT` | optional | seconds; default `28800` |
| `S3_BUCKET` | optional | only if `DOCUMENT_STORE=s3` |
| `PORT` | Render-provided | do not hardcode |
| `WMS_UAT_ADMIN_PASSWORD` | UAT only | never commit |
| `WMS_UAT_USER_PASSWORD` | UAT only | never commit |
| `AIVEN_AUDIT_DATABASE_URL` | V1 audit only | never a V2 reset/write target |

Production also enables CSRF (`WTF_CSRF_ENABLED=true`) and HTTPS cookie CSRF strictness when `SESSION_COOKIE_SECURE=true`.

---

## 3. Exact Render branch

`cursor/wms-v2-rebuild-6b85`

Do not deploy V2 from `main` until Gates 12–14 pass on the live V2 URL.

---

## 4. Exact Build Command

```text
pip install -r requirements.txt
```

---

## 5. Exact Start Command

```text
gunicorn wsgi:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120
```

Do **not** put `flask --app wsgi init-db` or `create-admin` in the start command.

Unified import worker (`wms-v2-import-worker`), same Aiven `WMS_V2_DATABASE_URL`:

```text
python -m app.workers.import_worker
```

Do **not** create a second database for the worker.

---

## 6. Health endpoint

- `GET /health` — JSON `{status, database, application_version}`; `200` when DB answers, `503` if degraded
- `GET /health/db` — JSON DB ping

Neither endpoint returns secrets, URIs, or password hashes.

After deploy, replace `HOST` with the live `wms-v2` hostname:

```text
curl -fsS https://HOST/health
curl -fsS https://HOST/health/db
```

---

## 7. init-db command

One-time, from a Render shell on `wms-v2` (or an equivalent one-off job against the same env):

```text
flask --app wsgi init-db
```

Behavior:

- Creates the V2 schema from scratch (`db.create_all` + `client_code_seq` + same-client mapping trigger)
- Seeds the permission catalog
- Idempotent: safe to run again; does not drop data
- Prints `V2 tables created and permissions seeded.`
- Does not print the database URI or any password

No V1 data is required or imported.

---

## 8. create-admin command

```text
flask --app wsgi create-admin --username leandro
```

The command prompts for the password twice (`hide_input`). It:

- hashes the password with Werkzeug
- does not print the password
- does not overwrite an existing admin (`User 'leandro' already exists.`)
- seeds permissions if needed
- initializes `client_code_seq` if needed

---

## 9. Live smoke-test commands / URLs

Replace `HOST` with the Render `wms-v2` URL. Do not run these against V1.

```text
curl -fsS https://HOST/health
curl -fsS https://HOST/health/db
curl -fsSI https://HOST/login
```

Browser smoke (two sessions):

1. Open `https://HOST/login` — CSRF hidden field present.
2. Sign in as the admin created in step 8.
3. Create Client / Division / Warehouse / mapping.
4. Upload one inventory file (preview then confirm). Confirm returns immediately and the page shows Inventory Import Processing; it must reach COMPLETED without keeping the browser request open. See `docs/WMS_V2_LARGE_IMPORT.md`.
5. Upload one order file (preview then confirm).
6. Allocate, generate pick ticket, process one UPC scan, close if in scope.
7. Confirm a second browser cannot close/scan the locked order (`Order is currently being processed by another user.`).
8. Restart the Render service; previously created client `01-…` still exists.
9. Confirm V1 `wms` URL still serves the old application.

A POST without a CSRF token must return `400` and must not mutate data.

---

## 10. Gate 13 certification command / prompt

After the live `wms-v2` URL is up and the smoke above is recorded, send a follow-up agent:

```text
CONTINUE MASTER MISSION. GATE 12 live URL is https://HOST
HEAD on cursor/wms-v2-rebuild-6b85 must match the deployed commit.
Run Gate 13 against that URL only. Do not merge to main. Do not touch V1.
Prove: health, login CSRF, admin bootstrap, two-browser lock, persist-after-restart.
If any live check fails: GATE 12 = FAIL and stop.
```

Do **not** mark Gate 12 PASS until that live URL has been tested.

---

## Render dashboard mapping

Create a **new** Web Service:

- Name: `wms-v2`
- Repo: `lavila2010/WMS`
- Branch: `cursor/wms-v2-rebuild-6b85`
- Runtime: Python
- Instance: start with the same plan family as V1; do not convert the existing `wms` service
- Build Command: see §4
- Start Command: see §5
- Env vars: see §2 (`SECRET_KEY` generate-value is acceptable if unique)

`render.yaml` already declares this second service next to V1 `wms`. Applying the blueprint must not delete `wms`.
