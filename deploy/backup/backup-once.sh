#!/bin/sh
# SPDX-License-Identifier: BUSL-1.1
# Produce ONE consistent ProcWorks backup, write its manifest, optionally
# encrypt and sync it off-site, then apply GFS retention.
#
# Consistency comes from the method, not from a follow-up check: pg_dump reads
# the whole database in a single MVCC snapshot (--serializable-deferrable), so
# every table is captured at the same logical instant even while the API keeps
# writing. See docs/Backup-und-Restore-Konzept.md §3.1.
#
# Usage: backup-once.sh          (invoked by run-scheduler.sh or manually)
# Env:   see lib.sh for all knobs (BACKUP_DIR, BACKUP_KEEP_*, BACKUP_PASSPHRASE,
#        BACKUP_SYNC_CMD, BACKUP_ALERT_WEBHOOK, PROCWORKS_VERSION) and libpq
#        variables (PGHOST/PGUSER/PGPASSWORD/PGDATABASE).
set -eu

_dir="$(cd "$(dirname "$0")" && pwd)"
. "${_dir}/lib.sh"

# Backups contain sensitive business data -> owner-only files/dirs (§9/§14).
umask 077

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR" 2>/dev/null || true

# Timestamped, minute-precision name matching the documented pattern
# (procworks-YYYY-MM-DDTHH-MM.dump). UTC keeps names monotonic across DST.
_ts="$(date -u +%Y-%m-%dT%H-%M)"
_dump="${BACKUP_DIR}/${BACKUP_PREFIX}-${_ts}.dump"

log info dump "starting consistent dump -> $(basename "$_dump")"

# -Fc: custom format (compressed, selectively restorable).
# --serializable-deferrable: wait for a snapshot that cannot see a serialization
# anomaly, without blocking concurrent writers (maximum-strictness online dump).
if ! pg_dump -Fc --serializable-deferrable --file "$_dump"; then
    rm -f "$_dump"
    log error dump "pg_dump FAILED"
    alert error "pg_dump failed"
    exit 1
fi
log info dump "dump complete ($(wc -c < "$_dump" | tr -d ' ') bytes)"

# Optional symmetric encryption at rest. Manifest records the true final path.
_encrypted=false
if [ -n "$BACKUP_PASSPHRASE" ]; then
    log info encrypt "encrypting dump at rest"
    _dump="$(encrypt_file "$_dump")"
    _encrypted=true
fi

# Manifest (checksum, versions, Alembic head, rough row counts) for later verify.
write_manifest "$_dump" "$_encrypted"

# Off-site copy (optional) and retention. A prune failure must not mask a
# successful, verified dump -- log it but still record success for the dump.
sync_offsite || true
prune || log warn prune "retention pass reported an error"

mark_success
publish_index                 # refresh the API-readable metadata index (§B6)
alert ok "backup ${_ts} succeeded"
log info done "backup ${_ts} finished successfully"
