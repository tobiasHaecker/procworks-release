#!/bin/sh
# SPDX-License-Identifier: BUSL-1.1
# Lightweight in-container scheduler for the Compose `backup` service.
#
# Runs backup-once.sh on a schedule without pulling in a cron package (the
# postgres:16-alpine image ships none). Also honours an on-demand trigger file
# so an optional "back up now" GUI action (roadmap B6) can request an immediate
# run by touching a marker -- the API never runs pg_dump itself (no coupling,
# no DB credentials in the API container). See concept §5.1/§11.
#
# BACKUP_CRON: a reduced 5-field expression "MIN HOUR * * *".
#   * MIN and HOUR accept an integer or "*" (every minute / every hour).
#   * The day-of-month, month and day-of-week fields must be "*" (documented
#     limitation -- daily/hourly cadence covers the target deployments).
# Default "0 2 * * *" = daily at 02:00 UTC.
set -eu

_dir="$(cd "$(dirname "$0")" && pwd)"
. "${_dir}/lib.sh"

BACKUP_CRON="${BACKUP_CRON:-0 2 * * *}"
_run_now_marker="${BACKUP_DIR}/.run-now"

# Parse the (reduced) cron expression once at startup.
_min="$(printf '%s' "$BACKUP_CRON"  | awk '{print $1}')"
_hour="$(printf '%s' "$BACKUP_CRON" | awk '{print $2}')"
_dom="$(printf '%s' "$BACKUP_CRON"  | awk '{print $3}')"
_mon="$(printf '%s' "$BACKUP_CRON"  | awk '{print $4}')"
_dow="$(printf '%s' "$BACKUP_CRON"  | awk '{print $5}')"

if [ "$_dom" != "*" ] || [ "$_mon" != "*" ] || [ "$_dow" != "*" ]; then
    log warn scheduler "only 'MIN HOUR * * *' is supported; ignoring day/month/dow fields in '$BACKUP_CRON'"
fi

# Prepare the shared control surface (roadmap B6): the API writes .run-now here
# (so the dir must be writable across container uids) and reads backups-index.json.
# It holds only metadata + a trigger marker -- never dumps (concept §9).
if [ -n "$BACKUP_CONTROL_DIR" ]; then
    mkdir -p "$BACKUP_CONTROL_DIR" 2>/dev/null || true
    chmod 0777 "$BACKUP_CONTROL_DIR" 2>/dev/null || true
    publish_index                 # so pre-existing backups show up immediately
fi

log info scheduler "started; schedule='$BACKUP_CRON' dir='$BACKUP_DIR'"

# field_matches PATTERN CURRENT -> true if PATTERN is "*" or equals CURRENT
# (with leading zeros stripped so "02" matches hour 2).
field_matches() {
    [ "$1" = "*" ] && return 0
    _p="$(printf '%s' "$1" | sed 's/^0*//')"; _p="${_p:-0}"
    _c="$(printf '%s' "$2" | sed 's/^0*//')"; _c="${_c:-0}"
    [ "$_p" = "$_c" ]
}

_last_run_slot=""

while true; do
    # On-demand run requested by an operator (marker in the backup dir) or by the
    # admin GUI (marker in the shared control dir): consume it and run now.
    _triggered=""
    [ -f "$_run_now_marker" ] && _triggered="$_run_now_marker"
    if [ -n "$BACKUP_CONTROL_DIR" ] && [ -f "${BACKUP_CONTROL_DIR}/.run-now" ]; then
        _triggered="${BACKUP_CONTROL_DIR}/.run-now"
    fi
    if [ -n "$_triggered" ]; then
        rm -f "$_run_now_marker" "${BACKUP_CONTROL_DIR:-/nonexistent}/.run-now" 2>/dev/null || true
        log info scheduler "on-demand trigger received ($_triggered)"
        "${_dir}/backup-once.sh" || log error scheduler "on-demand backup failed"
    fi

    _now_min="$(date -u +%M)"
    _now_hour="$(date -u +%H)"
    _slot="$(date -u +%Y%m%d%H%M)"

    if [ "$_slot" != "$_last_run_slot" ] \
        && field_matches "$_hour" "$_now_hour" \
        && field_matches "$_min" "$_now_min"; then
        _last_run_slot="$_slot"
        log info scheduler "scheduled run for slot $_slot"
        "${_dir}/backup-once.sh" || log error scheduler "scheduled backup failed"
    fi

    # Check roughly twice per minute so we never miss the matching minute.
    sleep 30
done
