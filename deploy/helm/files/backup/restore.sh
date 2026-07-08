#!/bin/sh
# SPDX-License-Identifier: BUSL-1.1
# Guided, atomic restore of a ProcWorks backup into PostgreSQL.
#
# This script performs the DATABASE-level part of the restore (concept §6.2
# steps 2-4): verify -> replace-in-one-transaction. It runs inside the backup
# container and therefore CANNOT stop/start the API container itself. Instead it
# HARD-GUARDS the "no writers during restore" invariant (§3.2): if any other
# session is connected to the database it refuses (unless --force), telling the
# operator to stop the API first.
#
# Documented operator flow (Compose):
#   docker compose -f deploy/docker-compose.full.yml stop api
#   docker compose -f deploy/docker-compose.full.yml run --rm backup \
#       /opt/backup/restore.sh --latest --yes
#   docker compose -f deploy/docker-compose.full.yml up -d api   # auto-migrates
#
# The final `up -d api` re-applies Alembic migrations on start (see the API's
# docker-entrypoint.sh), bringing an older restored schema forward to head.
#
# Options:
#   --latest              restore the newest backup in BACKUP_DIR
#   --file NAME           restore a specific backup (basename or full name)
#   --yes                 confirm the (destructive) restore non-interactively
#   --force               override the non-empty-DB and connected-clients guards
#   --pre-check-only      run all guards/verification, then stop (no changes)
set -eu

_dir="$(cd "$(dirname "$0")" && pwd)"
. "${_dir}/lib.sh"

_select=""
_file=""
_yes=0
_force=0
_precheck=0

while [ $# -gt 0 ]; do
    case "$1" in
        --latest)         _select="latest" ;;
        --file)           _select="file"; _file="${2:-}"; shift ;;
        --file=*)         _select="file"; _file="${1#--file=}" ;;
        --yes)            _yes=1 ;;
        --force)          _force=1 ;;
        --pre-check-only) _precheck=1 ;;
        -h|--help)
            grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
    shift
done

# -- Resolve the backup file ------------------------------------------------
case "$_select" in
    latest)
        _base="$(list_dumps | head -n1)"
        [ -n "$_base" ] || die "no backups found in $BACKUP_DIR"
        ;;
    file)
        [ -n "$_file" ] || die "--file requires a name"
        _base="$(basename "$_file")"
        ;;
    *)
        die "specify --latest or --file NAME (see --help)"
        ;;
esac

# The manifest and (possibly encrypted) dump share the timestamp stem.
_stem="$(printf '%s' "$_base" | sed 's/\.dump.*$//')"
_manifest="${BACKUP_DIR}/${_stem}.manifest.json"

# Locate the actual on-disk dump (plain or .gpg-encrypted).
_dump=""
for _cand in \
    "${BACKUP_DIR}/${_base}" \
    "${BACKUP_DIR}/${_stem}.dump" \
    "${BACKUP_DIR}/${_stem}.dump.gpg"; do
    if [ -f "$_cand" ]; then _dump="$_cand"; break; fi
done
[ -n "$_dump" ] || die "backup file for '$_base' not found in $BACKUP_DIR"
[ -f "$_manifest" ] || die "manifest not found: $(basename "$_manifest")"

log info restore "selected $(basename "$_dump")"

# -- Step 2a: integrity (checksum matches the manifest) ---------------------
_want_sha="$(manifest_value "$_manifest" sha256)"
_have_sha="$(checksum "$_dump")"
[ -n "$_want_sha" ] || die "manifest has no sha256"
[ "$_want_sha" = "$_have_sha" ] \
    || die "checksum mismatch (manifest=$_want_sha file=$_have_sha) -- refusing to restore a corrupt/tampered dump"
log info restore "checksum OK"

# -- Step 2b: version compatibility guards (§6.3) ---------------------------
# PostgreSQL major version: restoring into an OLDER major is unsupported.
_dump_pg_major="$(manifest_value "$_manifest" pg_major_version)"
_cur_pg_major="$(db_major_version)"
if [ -n "$_dump_pg_major" ] && [ "$_dump_pg_major" -gt "$_cur_pg_major" ] 2>/dev/null; then
    if [ "$_force" -eq 1 ]; then
        log warn restore "backup was taken on PostgreSQL $_dump_pg_major, target is $_cur_pg_major (proceeding due to --force)"
    else
        die "backup PostgreSQL major ($_dump_pg_major) is newer than target ($_cur_pg_major) -- restore into an older major is unsupported (use --force to override)"
    fi
fi

# Alembic head: only ever migrate FORWARD. The current DB reflects the running
# binary's head (the API migrates on start), so a dump whose head is NEWER than
# the current DB came from a newer ProcWorks version and cannot be resolved.
# Revisions are numerically prefixed (0001_..0007_), giving a total order.
_dump_head="$(manifest_value "$_manifest" alembic_head)"
_cur_head="$(db_alembic_head)"
_dump_head_num="$(printf '%s' "$_dump_head" | sed -n 's/^0*\([0-9]\{1,\}\).*/\1/p')"
_cur_head_num="$(printf '%s' "$_cur_head" | sed -n 's/^0*\([0-9]\{1,\}\).*/\1/p')"
if [ -n "$_dump_head_num" ] && [ -n "$_cur_head_num" ]; then
    if [ "$_dump_head_num" -gt "$_cur_head_num" ]; then
        die "backup Alembic head ($_dump_head) is newer than this deployment ($_cur_head) -- deploy the matching ProcWorks version first (cannot migrate backward)"
    fi
    log info restore "Alembic head OK (backup=$_dump_head <= current=$_cur_head)"
else
    log warn restore "non-numeric Alembic revisions ($_dump_head / $_cur_head) -- skipping forward-only check"
fi

# -- Step 1 (enforced here): no other writers may be connected --------------
_others="$(psql_scalar 'SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid();')"
_others="${_others:-0}"
if [ "$_others" -gt 0 ]; then
    if [ "$_force" -eq 1 ]; then
        log warn restore "$_others other DB connection(s) present (proceeding due to --force)"
    else
        die "$_others other connection(s) to the database -- stop the API first (e.g. 'docker compose stop api'); refusing to restore while writers may be active"
    fi
fi

# -- Confirmation guards (§6.3) ---------------------------------------------
# A non-empty target database additionally requires --force.
_live="$(psql_scalar 'SELECT coalesce(sum(n_live_tup),0) FROM pg_stat_user_tables;')"
_live="${_live:-0}"
if [ "$_live" -gt 0 ] && [ "$_force" -eq 0 ]; then
    die "target database is not empty (~$_live rows) -- pass --force to overwrite it"
fi
if [ "$_yes" -eq 0 ]; then
    die "restore is destructive -- re-run with --yes to confirm replacing the database"
fi

if [ "$_precheck" -eq 1 ]; then
    log info restore "pre-check passed; --pre-check-only set, no changes made"
    exit 0
fi

# -- Decrypt if necessary (into a temp plaintext for pg_restore) ------------
_plain="$_dump"
_tmp_plain=""
case "$_dump" in
    *.gpg)
        _tmp_plain="$(mktemp)"
        log info restore "decrypting dump"
        decrypt_file "$_dump" "$_tmp_plain"
        _plain="$_tmp_plain"
        ;;
esac

# -- Step 3: atomic replace (all-or-nothing) --------------------------------
# --single-transaction: on ANY error the whole restore rolls back and the
# database is left UNCHANGED (no half state). --clean --if-exists drops existing
# objects first so the restore is a true replace, not a merge.
log info restore "restoring in a single transaction (this replaces the database)"
if pg_restore --clean --if-exists --single-transaction \
        --dbname "${PGDATABASE:-procworks}" "$_plain"; then
    log info restore "restore committed"
else
    [ -n "$_tmp_plain" ] && rm -f "$_tmp_plain"
    die "pg_restore FAILED -- transaction rolled back, database is unchanged"
fi
[ -n "$_tmp_plain" ] && rm -f "$_tmp_plain"

log info restore "database restored to backup $(basename "$_dump")"
log info restore "next: start the API so it applies Alembic migrations, e.g. 'docker compose up -d api'"
