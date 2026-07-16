# syntax=docker/dockerfile:1
# SPDX-License-Identifier: BUSL-1.1
#
# Image for the demo BROKER (the scale-to-zero service behind the landing page's
# "Start test version" button). Separate from the demo instances it provisions.
# Build context is THIS directory (deploy/demo/broker); deploy from here:
#   cd deploy/demo/broker && fly deploy -a procworks-demo-broker
#
# The broker needs a Fly token to create/destroy per-visitor apps; provide it as
# a Fly *secret* (never bake it in):  fly secrets set FLY_API_TOKEN=... -a <app>

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --upgrade pip && python -m pip install -r requirements.txt

# Only the runtime modules (not the tests).
COPY app.py provision.py ./

RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health').status==200 else 1)"

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
