#!/usr/bin/env bash
# Idempotent bootstrap for the WMS (Flask + PostgreSQL) Cloud Agent environment.
#
# Production storage is Aiven PostgreSQL, reached via the DATABASE_URL secret.
# For local development and the automated test suite we also provision a LOCAL
# PostgreSQL instance (this is NOT used as the production database).
set -euo pipefail

PG_VERSION=16
LOCAL_DB_URL="postgresql://wms:wms@127.0.0.1:5432/wms"

echo "==> Ensuring system packages (local PostgreSQL + python venv) are installed"
if ! command -v pg_ctlcluster >/dev/null 2>&1 || ! python3 -m venv --help >/dev/null 2>&1; then
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    postgresql postgresql-contrib python3-venv
fi

echo "==> Starting local PostgreSQL cluster"
sudo pg_ctlcluster "${PG_VERSION}" main start 2>/dev/null || true
for _ in $(seq 1 30); do
  if sudo -u postgres pg_isready -q; then break; fi
  sleep 1
done

echo "==> Ensuring dev role and dev/test databases exist"
sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='wms'" | grep -q 1 \
  || sudo -u postgres psql -c "CREATE ROLE wms LOGIN PASSWORD 'wms' CREATEDB;"
for dbname in wms wms_test; do
  sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='${dbname}'" | grep -q 1 \
    || sudo -u postgres createdb -O wms "${dbname}"
done

echo "==> Creating Python virtualenv and installing dependencies"
python3 -m venv .venv
./.venv/bin/pip install --upgrade pip >/dev/null
./.venv/bin/pip install -r requirements-dev.txt

echo "==> Ensuring .env exists"
[ -f .env ] || cp .env.example .env

echo "==> Creating database tables"
export DATABASE_URL="${DATABASE_URL:-${LOCAL_DB_URL}}"
./.venv/bin/flask --app wsgi init-db \
  || echo "WARN: init-db skipped (database unreachable during install)"

echo "==> Install complete"
