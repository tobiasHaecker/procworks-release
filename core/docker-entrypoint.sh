#!/bin/sh
# SPDX-License-Identifier: BUSL-1.1
# Entrypoint for the procworks API container.
#
# When DATABASE_URL is set, apply Alembic migrations before serving so the
# schema, instance and audit_event tables exist (durable persistence). Without
# DATABASE_URL the API uses the in-memory stores and no migration is needed.
set -e

if [ -n "${DATABASE_URL}" ]; then
    echo "DATABASE_URL is set - applying Alembic migrations..."
    alembic upgrade head
else
    echo "DATABASE_URL is not set - using in-memory stores (no durability)."
fi

exec "$@"
