#!/usr/bin/env bash
# Per-boot startup for the WMS Cloud Agent environment.
# Starts the PostgreSQL cluster and waits until it is ready. The Next.js
# dev server itself runs as a named terminal (see environment.json).
set -euo pipefail

PG_VERSION=16

echo "==> Starting PostgreSQL cluster"
sudo pg_ctlcluster "${PG_VERSION}" main start 2>/dev/null || true

echo "==> Waiting for PostgreSQL to accept connections"
for _ in $(seq 1 30); do
  if sudo -u postgres pg_isready -q; then
    echo "==> PostgreSQL is ready"
    exit 0
  fi
  sleep 1
done

echo "PostgreSQL did not become ready in time" >&2
exit 1
