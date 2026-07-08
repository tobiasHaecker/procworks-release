#!/bin/sh
# SPDX-License-Identifier: BUSL-1.1
# Self-test a backup by restoring it into a THROWAWAY database and running a few
# consistency checks, then dropping the throwaway database (concept §7).
#
# "A backup you cannot restore is worthless." This proves a dump is actually
# restorable and internally consistent, without touching the live database.
#
# Note on Alembic: the backup container has no Alembic binary, and it does not
# need one -- a dump is self-consistent at its OWN head (the schema and the
# alembic_version row travel together). The real forward-migration is exercised
# during an actual restore when the API restarts. verify.sh therefore checks the
# restored dump as-is; it does not run `alembic upgrade head`.
#
# Options:
#   --latest      verify the newest backup (default)
#   --file NAME   verify a specific backup
set -eu

_dir="$(cd "$(dirname "$0")" && pwd)"
. "${_dir}/lib.sh"

_select="latest"
_file=""
while [ $# -gt 0 ]; do
    case "$1" in
        --latest)  _select="latest" ;;
        --file)    _select="file"; _file="${2:-}"; shift ;;
        --file=*)  _select="file"; _file="${1#--file=}" ;;
        -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
    shift
done

if [ "$_select" = "latest" ]; then
    _base="$(list_dumps | head -n1)"
    [ -n "$_base" ] || die "no backups found in $BACKUP_DIR"
else
    [ -n "$_file" ] || die "--file requires a name"
    _base="$(basename "$_file")"
fi

_stem="$(printf '%s' "$_base" | sed 's/\.dump.*$//')"
_manifest="${BACKUP_DIR}/${_stem}.manifest.json"

_dump=""
for _cand in \
    "${BACKUP_DIR}/${_base}" \
    "${BACKUP_DIR}/${_stem}.dump" \
    "${BACKUP_DIR}/${_stem}.dump.gpg"; do
    if [ -f "$_cand" ]; then _dump="$_cand"; break; fi
done
[ -n "$_dump" ] || die "backup file for '$_base' not found"
[ -f "$_manifest" ] || die "manifest not found: $(basename "$_manifest")"

# Integrity first (cheap, catches corruption before we spin up a database).
_want_sha="$(manifest_value "$_manifest" sha256)"
[ "$_want_sha" = "$(checksum "$_dump")" ] || die "checksum mismatch for $(basename "$_dump")"
log info verify "checksum OK for $(basename "$_dump")"

# Throwaway database name, unique per run.
_vdb="procworks_verify_$$"

# cleanup -> always drop the throwaway DB and temp plaintext, even on failure.
cleanup() {
    [ -n "${_tmp_plain:-}" ] && rm -f "$_tmp_plain"
    psql -d postgres -c "DROP DATABASE IF EXISTS \"$_vdb\";" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

_tmp_plain=""
case "$_dump" in
    *.gpg)
        _tmp_plain="$(mktemp)"
        decrypt_file "$_dump" "$_tmp_plain"
        _restore_src="$_tmp_plain"
        ;;
    *) _restore_src="$_dump" ;;
esac

log info verify "creating throwaway database $_vdb"
psql -d postgres -c "DROP DATABASE IF EXISTS \"$_vdb\";" >/dev/null 2>&1 || true
psql -d postgres -c "CREATE DATABASE \"$_vdb\";" >/dev/null \
    || die "could not create throwaway database"

log info verify "restoring dump into $_vdb"
# Not --single-transaction here: verify is a best-effort probe, and we want to
# see as many objects as possible even if a non-fatal notice occurs.
pg_restore --clean --if-exists --dbname "$_vdb" "$_restore_src" \
    || die "pg_restore into throwaway DB failed -- backup is NOT restorable"

# --- Consistency checks on the restored throwaway database -----------------
# Run each check against $_vdb via `psql -d $_vdb`.
_fail=0

# 1) Every instance references an existing schema id.
_orphans="$(psql -d "$_vdb" -Atqc \
    'SELECT count(*) FROM process_instance i LEFT JOIN process_schema s ON i.schema_id = s.id WHERE s.id IS NULL;' 2>/dev/null)"
_orphans="${_orphans:-0}"
if [ "$_orphans" -ne 0 ]; then
    log error verify "consistency: $_orphans instance(s) reference a missing schema"
    _fail=1
else
    log info verify "consistency: all instances reference an existing schema"
fi

# 2) audit_event.seq strictly unique (no duplicates). seq is a monotonic,
#    database-assigned key; it may have gaps (rolled-back txns) but must be unique.
_dupes="$(psql -d "$_vdb" -Atqc \
    'SELECT count(*) - count(DISTINCT seq) FROM audit_event;' 2>/dev/null)"
_dupes="${_dupes:-0}"
if [ "$_dupes" -ne 0 ]; then
    log error verify "consistency: audit_event.seq has $_dupes duplicate(s)"
    _fail=1
else
    log info verify "consistency: audit_event.seq is unique"
fi

# 3) Exactly one Alembic version row (schema/version travel with the data).
_avr="$(psql -d "$_vdb" -Atqc 'SELECT count(*) FROM alembic_version;' 2>/dev/null)"
_avr="${_avr:-0}"
if [ "$_avr" -ne 1 ]; then
    log error verify "consistency: alembic_version has $_avr rows (expected 1)"
    _fail=1
else
    log info verify "consistency: single alembic_version row present"
fi

if [ "$_fail" -ne 0 ]; then
    die "verify FAILED for $(basename "$_dump")"
fi

# Record a verify success marker for monitoring (§10).
date -u +%Y-%m-%dT%H:%M:%SZ > "${BACKUP_DIR}/.last-verify-success"
publish_index                 # surface the new verify timestamp to the admin view
log info verify "OK -- $(basename "$_dump") is restorable and consistent"
