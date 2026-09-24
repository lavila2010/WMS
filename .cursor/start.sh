#!/usr/bin/env bash
# Per-boot startup: bring up the local PostgreSQL cluster used for local
# development/testing. The Flask app itself runs as a named terminal
# (see environment.json) and connects to DATABASE_URL (Aiven in production).
set -euo pipefail

PG_VERSION=16

echo "==> Starting local PostgreSQL cluster"
sudo pg_ctlcluster "${PG_VERSION}" main start 2>/dev/null || true

for _ in $(seq 1 30); do
  if sudo -u postgres pg_isready -q; then
    echo "==> PostgreSQL is ready"
    exit 0
  fi
  sleep 1
done

echo "PostgreSQL did not become ready in time" >&2
exit 1
