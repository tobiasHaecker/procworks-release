# syntax=docker/dockerfile:1

# Container image for the web GUI + reverse proxy (roadmap step 16).
# It bundles the static SPA assets into a Caddy image that also terminates TLS
# and reverse-proxies /api/* to the API server (see deploy/Caddyfile).
#
# Build context is the repository root so both web/ and deploy/ are available:
#   docker build -f web/Dockerfile -t procworks-web .

FROM caddy:2-alpine

# Apply available OS security patches on top of the pinned base image
# (e.g. OpenSSL CVE fixes that ship ahead of a new caddy image).
RUN apk --no-cache -U upgrade

# Static single-page app assets.
COPY web/ /srv/

# Reverse-proxy + TLS + static-serving configuration.
COPY deploy/Caddyfile /etc/caddy/Caddyfile

EXPOSE 80 443
