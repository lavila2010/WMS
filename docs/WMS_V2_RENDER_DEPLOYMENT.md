# WMS V2 Render Deployment

V2 must be a **separate** Render web service (`wms-v2`). Do not replace V1 `wms`.

## Service

- Name: `wms-v2`
- Runtime: Python 3.12
- Build: `pip install -r requirements.txt`
- Start: `gunicorn wsgi:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120`
- Bootstrap (once, never on start): `flask --app wsgi init-db` then `flask --app wsgi create-admin`

## Environment

| Variable | Required |
|---|---|
| `WMS_V2_DATABASE_URL` | Aiven V2 PostgreSQL (preferred) |
| `DATABASE_URL` | fallback only |
| `SECRET_KEY` | unique; production rejects insecure defaults |
| `WMS_ENV` | `production` |
| `SESSION_COOKIE_SECURE` | `true` |
| `PYTHON_VERSION` | `3.12.6` |
| `WMS_TIMEZONE` | `America/New_York` |
| `DOCUMENT_STORE` | `local` until object-storage credentials exist |
| `S3_BUCKET` | optional durable store |

`AIVEN_AUDIT_DATABASE_URL` is V1 read-only. Never point reset scripts at it.

## This environment

This Cloud Agent run has no Render API token and no Aiven V2 URI injected. GATE 12 cannot be executed here. Create `wms-v2` in the Render dashboard against a new Aiven database, then rerun smoke:

1. Build succeeds
2. Gunicorn starts
3. `GET /health` = 200
4. Login + admin bootstrap
5. Two browser sessions
6. Data persists across restart
