# syntax=docker/dockerfile:1
# SPDX-License-Identifier: BUSL-1.1
#
# Combined throw-away DEMO image (Variante A): the API *and* the static web
# client in a single container, running fully in-memory and seeded at boot.
# One container = whole application = one URL -- ideal for a per-visitor,
# scale-to-zero cloud demo.
#
# Build from the REPOSITORY ROOT so both core/ and web/ are in the build
# context:
#   docker build -f deploy/demo/Dockerfile -t procworks-demo .
#
# This is NOT the production self-hosting path. A durable install uses the
# regular API image (core/Dockerfile) with DATABASE_URL; see the repo README.

FROM python:3.12-slim

# Do not buffer stdout/stderr; no .pyc files in the container layer.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install the API package (base deps only -- the demo has no database, so the
# [postgres] extra is deliberately omitted). Dependencies are layered first for
# better caching.
COPY core/pyproject.toml core/README.md ./
COPY core/src ./src
RUN python -m pip install --upgrade pip \
    && python -m pip install .

# Static single-page app, served from the same process via PROCWORKS_WEB_DIR.
COPY web ./web

# --- Demo runtime configuration -------------------------------------------
# In-memory (NO DATABASE_URL -> no migration, no persistence, wipes on restart).
# Boot-seeded and single-URL: the two additive boot switches do the work.
ENV PROCWORKS_LOAD_DEMO=1 \
    PROCWORKS_DEMO_MODE=1 \
    PROCWORKS_WEB_DIR=/app/web \
    PROCWORKS_AUTH=password \
    PROCWORKS_SESSION_TTL_MINUTES=120 \
    PROCWORKS_EGRESS_DENY=1 \
    PROCWORKS_DEMO_FEEDBACK_URL=https://broker.procworks.de/feedback
# PROCWORKS_DEMO_MODE surfaces the seeded demo logins on /auth/config so the SPA
# auto-logs the visitor in as the modeler (no credential guessing). It is a
# throw-away-demo-only switch and MUST stay unset in any real deployment.
# PROCWORKS_EGRESS_DENY hard-refuses every webhook/push target at the app layer
# (outbox.assert_url_allowed), so a visitor (a modeler here) cannot register a
# webhook that turns the demo into an egress beacon / exfil channel -- the SSRF
# guard alone only blocks *internal* targets, not arbitrary public ones.
# IMPORTANT invariants for a public throw-away demo (leave these UNSET):
#   * PROCWORKS_LICENSE_PUBKEY -> licensing stays dormant. The demo seeds five
#     agents; with the free quota of three, enforcement would 402 the demo.
#   * PROCWORKS_SMTP_*         -> mail falls back to the no-op NullMailSender.
#   * PROCWORKS_WEBHOOK_ALLOWLIST / PROCWORKS_CONNECTIONS / PROCWORKS_PUSH_ENDPOINTS
#     -> no outbound targets configured. DAL connectors carry no URL in the model
#     (register_connector stores metadata only), so with PROCWORKS_CONNECTIONS
#     unset the data layer has nothing to dial. Egress-deny above closes the one
#     visitor-reachable path (webhooks); a platform egress policy is defence in
#     depth on top.

# Run as a non-root user.
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000

# Liveness probe against the public /health endpoint.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"

# In-memory demo -> no Alembic entrypoint needed; launch Uvicorn directly.
CMD ["uvicorn", "procworks.api:app", "--host", "0.0.0.0", "--port", "8000"]
