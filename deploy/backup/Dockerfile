# SPDX-License-Identifier: BUSL-1.1
# Backup runner image: the standard PostgreSQL client tools (pg_dump/pg_restore/
# psql from the base image) plus GnuPG for optional symmetric encryption at rest
# (BACKUP_PASSPHRASE, concept §9 / roadmap B5). Kept intentionally tiny -- it is
# only the base image plus gnupg.
#
# Build (from the repository root):
#   docker build -t procworks-backup deploy/backup
# The bundled Compose stack builds it automatically for the `backup` service.
FROM postgres:16-alpine

# gnupg provides `gpg` for unattended symmetric encryption (--passphrase-fd 0).
RUN apk add --no-cache gnupg

# The scripts are mounted (Compose) or injected via ConfigMap (Helm) at runtime,
# so no COPY is needed here; the image only supplies the tools.
