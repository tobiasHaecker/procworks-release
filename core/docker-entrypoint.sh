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

# The client address for the login lock: ProcWorks reads X-Forwarded-For
# itself, and believes it only from the proxies named in
# PROCWORKS_TRUSTED_PROXIES. uvicorn would evaluate the header first, for every
# local peer (FORWARDED_ALLOW_IPS), and replace the client address with it --
# its setting, not PROCWORKS_TRUSTED_PROXIES, would then decide. So uvicorn's
# own evaluation is off unless the operator sets UVICORN_PROXY_HEADERS (or
# passes --proxy-headers, which wins over the variable).
: "${UVICORN_PROXY_HEADERS:=false}"
export UVICORN_PROXY_HEADERS

exec "$@"
