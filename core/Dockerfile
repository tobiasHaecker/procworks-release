# syntax=docker/dockerfile:1

# Container image for the procworks API server (roadmap step 16).
# Build context is the core/ directory:
#   docker build -t procworks-api core/
#
# The image is stateless; point it at PostgreSQL via DATABASE_URL. On start the
# entrypoint applies Alembic migrations (when DATABASE_URL is set) and then
# launches Uvicorn. Without DATABASE_URL the API runs against the in-memory
# stores (demo only, no durability).

FROM python:3.12-slim AS base

# Do not buffer stdout/stderr; no .pyc files in the container layer.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first (better layer caching), then the package itself.
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --upgrade pip \
    && python -m pip install ".[postgres]"

# Migrations + Alembic config are needed at runtime for the entrypoint.
COPY alembic.ini ./
COPY migrations ./migrations
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Run as a non-root user.
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000

# Lightweight liveness probe against the public /health endpoint.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "procworks.api:app", "--host", "0.0.0.0", "--port", "8000"]
