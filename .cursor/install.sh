#!/usr/bin/env bash
# Idempotent repository bootstrap for the WMS Cloud Agent environment.
# Installs PostgreSQL if missing, ensures the dev role/database exist,
# installs Node dependencies, generates the Prisma client, applies
# migrations, and seeds sample data.
set -euo pipefail

PG_VERSION=16

echo "==> Ensuring PostgreSQL ${PG_VERSION} is installed"
if ! command -v pg_ctlcluster >/dev/null 2>&1; then
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    postgresql postgresql-contrib
fi

echo "==> Starting PostgreSQL cluster"
sudo pg_ctlcluster "${PG_VERSION}" main start 2>/dev/null || true

echo "==> Waiting for PostgreSQL to accept connections"
for _ in $(seq 1 30); do
  if sudo -u postgres pg_isready -q; then break; fi
  sleep 1
done

echo "==> Ensuring 'wms' role and database exist"
sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='wms'" | grep -q 1 \
  || sudo -u postgres psql -c "CREATE ROLE wms LOGIN PASSWORD 'wms' CREATEDB;"
sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='wms'" | grep -q 1 \
  || sudo -u postgres createdb -O wms wms

echo "==> Ensuring .env exists"
[ -f .env ] || cp .env.example .env

echo "==> Installing Node dependencies"
pnpm install --frozen-lockfile

echo "==> Generating Prisma client and applying migrations"
pnpm prisma:generate
pnpm prisma:migrate

echo "==> Seeding sample data (idempotent)"
pnpm db:seed

echo "==> Install complete"
