# SPDX-License-Identifier: BUSL-1.1
# Shared helper library for the ProcWorks backup/restore scripts.
#
# Sourced by backup-once.sh, run-scheduler.sh, restore.sh and verify.sh. All
# functions are written for POSIX /bin/sh (busybox ash in postgres:16-alpine) --
# no bashisms (no arrays, no [[ ]]). Database access uses libpq environment
# variables (PGHOST/PGUSER/PGPASSWORD/PGDATABASE) that the container provides.
#
# Design notes:
#   * The backup container only ever talks to PostgreSQL over the network. It
#     never controls other containers and never needs Alembic -- the API
#     container applies migrations on its own start (see docker-entrypoint.sh).
#   * Consistency is a property of the *method*: pg_dump reads one MVCC snapshot,
#     pg_restore replays in one transaction. See docs/Backup-und-Restore-Konzept.md.

# --------------------------------------------------------------------------
# Configuration with sensible, out-of-the-box defaults (all overridable via env).
# --------------------------------------------------------------------------
BACKUP_DIR="${BACKUP_DIR:-/backups}"
BACKUP_PREFIX="${BACKUP_PREFIX:-procworks}"
BACKUP_KEEP_DAILY="${BACKUP_KEEP_DAILY:-14}"
BACKUP_KEEP_WEEKLY="${BACKUP_KEEP_WEEKLY:-8}"
BACKUP_KEEP_MONTHLY="${BACKUP_KEEP_MONTHLY:-6}"
# Optional: symmetric encryption at rest (gpg preferred, then age). When a
# passphrase is set but no tool is available, backup-once.sh fails hard rather
# than silently writing an unencrypted dump.
BACKUP_PASSPHRASE="${BACKUP_PASSPHRASE:-}"
# Optional: off-site copy hook and operational alert webhook.
BACKUP_SYNC_CMD="${BACKUP_SYNC_CMD:-}"
BACKUP_ALERT_WEBHOOK="${BACKUP_ALERT_WEBHOOK:-}"
# Optional: shared "control" directory the API may read (roadmap B6). The
# scheduler publishes a metadata-only index here and watches it for the
# .run-now trigger. It NEVER contains dumps -- the API must not access the dump
# volume (concept §9). Empty = the read-only admin view is not wired.
BACKUP_CONTROL_DIR="${BACKUP_CONTROL_DIR:-}"
# Optional: best-effort application version recorded in the manifest. The real
# compatibility gate is the Alembic head (read from the database itself).
PROCWORKS_VERSION="${PROCWORKS_VERSION:-unknown}"

# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

# log LEVEL PHASE MESSAGE...
# Emit one structured, greppable line to stdout, e.g.
#   ts=2026-07-08T02:00:01Z level=info phase=dump msg="starting"
# Structured logging keeps `docker compose logs backup` machine-readable (§10).
log() {
    _level="$1"; _phase="$2"; shift 2
    printf 'ts=%s level=%s phase=%s msg="%s"\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$_level" "$_phase" "$*"
}

# die MESSAGE...
# Log an error and abort the current script with a non-zero exit code.
die() {
    log error fatal "$*"
    exit 1
}

# --------------------------------------------------------------------------
# Checksums
# --------------------------------------------------------------------------

# checksum FILE -> prints the lowercase sha256 hex digest of FILE.
# Uses busybox `sha256sum` (present in the postgres:16-alpine image).
checksum() {
    sha256sum "$1" | cut -d' ' -f1
}

# --------------------------------------------------------------------------
# Connection: derive libpq env from a SQLAlchemy-style DATABASE_URL
# --------------------------------------------------------------------------

# db_env_from_url URL -> export PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE parsed
# from a URL like "postgresql+psycopg://user:pass@host:5432/dbname?opts".
# The SQLAlchemy driver suffix ("+psycopg") sits in the scheme and is simply
# discarded here (pg_dump/psql speak libpq, not SQLAlchemy). Kubernetes provides
# only DATABASE_URL (one secret, single source of truth); Docker Compose instead
# sets the PG* variables directly, in which case this is never called.
# Note: userinfo is used verbatim (no percent-decoding) -- fine for typical
# credentials; URL-encoded special characters would need decoding.
db_env_from_url() {
    _rest="${1#*://}"                                  # drop scheme
    case "$_rest" in
        *@*) _userinfo="${_rest%%@*}"; _rest="${_rest#*@}" ;;
        *)   _userinfo="" ;;
    esac
    _hostport="${_rest%%/*}"
    case "$_rest" in */*) _dbpart="${_rest#*/}" ;; *) _dbpart="" ;; esac
    _db="${_dbpart%%\?*}"                               # strip ?query
    case "$_hostport" in
        *:*) _h="${_hostport%%:*}"; _p="${_hostport##*:}" ;;
        *)   _h="$_hostport"; _p="5432" ;;
    esac
    case "$_userinfo" in
        *:*) _user="${_userinfo%%:*}"; _pass="${_userinfo#*:}" ;;
        "")  _user=""; _pass="" ;;
        *)   _user="$_userinfo"; _pass="" ;;
    esac
    [ -n "$_h" ]    && export PGHOST="$_h"
    [ -n "$_p" ]    && export PGPORT="$_p"
    [ -n "$_user" ] && export PGUSER="$_user"
    [ -n "$_pass" ] && export PGPASSWORD="$_pass"
    [ -n "$_db" ]   && export PGDATABASE="$_db"
}

# Auto-derive PG* from DATABASE_URL when no explicit PGHOST is provided.
if [ -z "${PGHOST:-}" ] && [ -n "${DATABASE_URL:-}" ]; then
    db_env_from_url "$DATABASE_URL"
fi

# --------------------------------------------------------------------------
# Database introspection (no Alembic binary required)
# --------------------------------------------------------------------------

# psql_scalar SQL -> prints a single scalar result (empty on error/no rows).
# -A unaligned, -t tuples-only, -q quiet: yields a bare value with no headers.
psql_scalar() {
    psql -Atq -c "$1" 2>/dev/null
}

# db_alembic_head -> current migration revision stored in the database.
# This value travels *inside* every dump, so a restored dump always carries the
# schema version it was taken at (see concept §2.1/§6).
db_alembic_head() {
    _head="$(psql_scalar 'SELECT version_num FROM alembic_version LIMIT 1;')"
    [ -n "$_head" ] && printf '%s' "$_head" || printf 'unknown'
}

# db_server_version_num -> numeric PostgreSQL server version (e.g. 160004).
db_server_version_num() {
    _v="$(psql_scalar 'SHOW server_version_num;')"
    [ -n "$_v" ] && printf '%s' "$_v" || printf '0'
}

# db_major_version -> PostgreSQL major version (e.g. 16), derived from the
# numeric server version. Used by the restore major-version guard (§6.3).
db_major_version() {
    _num="$(db_server_version_num)"
    printf '%s' "$(( _num / 10000 ))"
}

# db_table_rows -> newline-separated "relname n_live_tup" pairs for user tables.
# Uses the planner's live-tuple estimate (pg_stat_user_tables): cheap and exactly
# the "rough plausibility" the manifest promises (§7) -- not an exact count.
db_table_rows() {
    psql_scalar "SELECT relname || ' ' || n_live_tup FROM pg_stat_user_tables ORDER BY relname;"
}

# --------------------------------------------------------------------------
# Encryption at rest (optional, §9 / roadmap B5)
# --------------------------------------------------------------------------
#
# We use GnuPG symmetric encryption (AES-256). It is the right tool for an
# *unattended* backup: the passphrase is fed on file descriptor 0
# (--passphrase-fd 0), so no terminal is required. (age's passphrase mode, by
# contrast, insists on a TTY and cannot be scripted, so it is not used here.)
# The image built from deploy/backup/Dockerfile ships gpg; the plain
# postgres:16-alpine base does not -- hence encryption fails hard rather than
# silently writing a plaintext dump when a passphrase was requested.

# gpg_home -> a private, throwaway GNUPGHOME so gpg never complains about home
# permissions and leaves no state behind (symmetric encryption needs no keyring).
gpg_home() {
    _gh="$(mktemp -d)"
    printf '%s' "$_gh"
}

# encrypt_file PLAINFILE -> encrypts in place, prints the resulting path (.gpg).
# Only called when BACKUP_PASSPHRASE is set. Removes the plaintext on success so
# no unencrypted copy lingers. Fails hard if gpg is unavailable (never falls back
# to storing the requested-encrypted dump in the clear).
encrypt_file() {
    _plain="$1"
    command -v gpg >/dev/null 2>&1 \
        || die "BACKUP_PASSPHRASE is set but gpg is not installed -- use the image built from deploy/backup/Dockerfile; refusing to write an unencrypted backup"
    _enc="${_plain}.gpg"
    _gh="$(gpg_home)"
    printf '%s' "$BACKUP_PASSPHRASE" | gpg --homedir "$_gh" --batch --yes --quiet \
        --passphrase-fd 0 --pinentry-mode loopback \
        --symmetric --cipher-algo AES256 --output "$_enc" "$_plain" \
        || { rm -rf "$_gh"; die "gpg encryption failed"; }
    rm -rf "$_gh"
    rm -f "$_plain"
    printf '%s' "$_enc"
}

# decrypt_file ENCFILE PLAINOUT -> decrypts ENCFILE to PLAINOUT for restore/verify.
decrypt_file() {
    _enc="$1"; _out="$2"
    case "$_enc" in
        *.gpg)
            command -v gpg >/dev/null 2>&1 || die "encrypted dump ($_enc) but gpg not installed"
            _gh="$(gpg_home)"
            printf '%s' "$BACKUP_PASSPHRASE" | gpg --homedir "$_gh" --batch --yes --quiet \
                --passphrase-fd 0 --pinentry-mode loopback \
                --decrypt --output "$_out" "$_enc" \
                || { rm -rf "$_gh"; die "gpg decryption failed (wrong BACKUP_PASSPHRASE?)"; }
            rm -rf "$_gh"
            ;;
        *)
            die "decrypt_file: unsupported encrypted format ($_enc) -- expected a .gpg dump"
            ;;
    esac
}

# --------------------------------------------------------------------------
# Date arithmetic for GFS retention (no external `date -d` parsing needed)
# --------------------------------------------------------------------------

# epoch_day YEAR MONTH DAY -> integer days since 1970-01-01 (may be negative).
# Howard Hinnant's branch-free days_from_civil algorithm, in pure POSIX integer
# arithmetic so it is identical on busybox ash and on the host (unit-testable).
# We derive daily/weekly/monthly retention buckets from the dump's own timestamp
# rather than from file mtimes, so retention is deterministic and tool-agnostic.
epoch_day() {
    # Strip leading zeros first: "08"/"09" would otherwise be read as invalid
    # octal inside $(( )) (true on busybox ash too). The sed keeps one digit.
    _y="$(printf '%s' "$1" | sed 's/^0*\([0-9]\)/\1/')"
    _m="$(printf '%s' "$2" | sed 's/^0*\([0-9]\)/\1/')"
    _d="$(printf '%s' "$3" | sed 's/^0*\([0-9]\)/\1/')"
    # Shift so that March is month 0 (leap day lands at the end of the year).
    if [ "$_m" -le 2 ]; then _y=$(( _y - 1 )); fi
    _era=$(( ( _y >= 0 ? _y : _y - 399 ) / 400 ))
    _yoe=$(( _y - _era * 400 ))                        # [0, 399]
    if [ "$_m" -gt 2 ]; then _mp=$(( _m - 3 )); else _mp=$(( _m + 9 )); fi
    _doy=$(( (153 * _mp + 2) / 5 + _d - 1 ))           # [0, 365]
    _doe=$(( _yoe * 365 + _yoe / 4 - _yoe / 100 + _doy ))
    printf '%s' "$(( _era * 146097 + _doe - 719468 ))"
}

# --------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------

# write_manifest DUMPFILE ENCRYPTED -> writes DUMPFILE-with-.manifest.json sidecar.
# ENCRYPTED is "true"/"false". The manifest is the audit/verification record for
# a backup (§5/§7): timestamp, versions, Alembic head, size, sha256 and a rough
# per-table row estimate. It is consumed by restore.sh (integrity + version
# guards) and by the optional read-only GET /admin/backups view.
write_manifest() {
    _dump="$1"; _encrypted="$2"
    _manifest="${BACKUP_DIR}/$(basename "$_dump" | sed 's/\.dump.*$//').manifest.json"
    _size="$(wc -c < "$_dump" | tr -d ' ')"
    _sha="$(checksum "$_dump")"

    # Build the per-table row-count object from "relname count" pairs.
    _rows_json=""
    _first=1
    _tmp_rows="$(db_table_rows)"
    if [ -n "$_tmp_rows" ]; then
        # POSIX-safe line iteration.
        printf '%s\n' "$_tmp_rows" | while IFS=' ' read -r _rel _cnt; do
            [ -n "$_rel" ] || continue
            if [ "$_first" -eq 1 ]; then _first=0; else printf ','; fi
            printf '"%s":%s' "$_rel" "$_cnt"
        done > "${_manifest}.rows.tmp"
        _rows_json="$(cat "${_manifest}.rows.tmp")"
        rm -f "${_manifest}.rows.tmp"
    fi

    cat > "$_manifest" <<EOF
{
  "file": "$(basename "$_dump")",
  "created_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "app_version": "${PROCWORKS_VERSION}",
  "alembic_head": "$(db_alembic_head)",
  "pg_server_version_num": $(db_server_version_num),
  "pg_major_version": $(db_major_version),
  "size_bytes": ${_size},
  "sha256": "${_sha}",
  "encrypted": ${_encrypted},
  "table_rows": { ${_rows_json} }
}
EOF
    log info manifest "wrote $(basename "$_manifest")"
}

# manifest_value MANIFEST KEY -> extract a scalar string/number value for KEY.
# A tiny grep/sed reader so we do not depend on jq being present in the image.
manifest_value() {
    grep -o "\"$2\"[[:space:]]*:[[:space:]]*\"\{0,1\}[^\",}]*" "$1" \
        | head -n1 | sed 's/.*:[[:space:]]*"\{0,1\}//'
}

# --------------------------------------------------------------------------
# Retention (Grandfather-Father-Son)
# --------------------------------------------------------------------------

# list_dumps -> newline-separated dump basenames, NEWEST FIRST.
# ISO-8601 timestamps in the names sort lexically == chronologically.
list_dumps() {
    ls -1 "$BACKUP_DIR" 2>/dev/null \
        | grep -E "^${BACKUP_PREFIX}-[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}-[0-9]{2}\.dump" \
        | sort -r
}

# dump_epoch_day BASENAME -> epoch day derived from the timestamp in the name.
# Extracts year/month/day separately and passes them as explicit arguments, so
# it does not rely on word-splitting behaviour (which differs across shells).
dump_epoch_day() {
    _yy="$(printf '%s' "$1" | sed -E "s/^${BACKUP_PREFIX}-([0-9]{4})-[0-9]{2}-[0-9]{2}T.*/\1/")"
    _mm="$(printf '%s' "$1" | sed -E "s/^${BACKUP_PREFIX}-[0-9]{4}-([0-9]{2})-[0-9]{2}T.*/\1/")"
    _dd="$(printf '%s' "$1" | sed -E "s/^${BACKUP_PREFIX}-[0-9]{4}-[0-9]{2}-([0-9]{2})T.*/\1/")"
    epoch_day "$_yy" "$_mm" "$_dd"
}

# dump_month_key BASENAME -> YYYYMM integer (monthly bucket).
dump_month_key() {
    printf '%s' "$1" | sed -E "s/^${BACKUP_PREFIX}-([0-9]{4})-([0-9]{2})-.*/\1\2/"
}

# prune -> apply GFS retention, deleting dumps (and their manifests) that no
# rule keeps. A dump is KEPT if any of the following holds:
#   * it is among the newest BACKUP_KEEP_DAILY dumps overall, or
#   * it is the newest dump in its ISO week, among the newest KEEP_WEEKLY weeks, or
#   * it is the newest dump in its calendar month, among the newest KEEP_MONTHLY months.
# Idempotent: running it repeatedly on an unchanged set deletes nothing (§8).
prune() {
    _all="$(list_dumps)"
    [ -n "$_all" ] || { log info prune "no dumps to prune"; return 0; }

    _keep_file="$(mktemp)"    # set of basenames to retain

    # -- Daily: newest N by count --------------------------------------------
    printf '%s\n' "$_all" | head -n "$BACKUP_KEEP_DAILY" >> "$_keep_file"

    # -- Weekly: newest dump per 7-day bucket, newest KEEP_WEEKLY buckets ------
    _seen_weeks="$(mktemp)"
    _week_count=0
    printf '%s\n' "$_all" | while IFS= read -r _b; do
        [ -n "$_b" ] || continue
        _ed="$(dump_epoch_day "$_b")"
        _wk=$(( (_ed + 3) / 7 ))          # +3 aligns bucket boundaries to Monday
        if ! grep -qx "$_wk" "$_seen_weeks"; then
            _week_count=$(( _week_count + 1 ))
            [ "$_week_count" -le "$BACKUP_KEEP_WEEKLY" ] || continue
            printf '%s\n' "$_wk" >> "$_seen_weeks"
            printf '%s\n' "$_b" >> "$_keep_file"
        fi
    done
    rm -f "$_seen_weeks"

    # -- Monthly: newest dump per calendar month, newest KEEP_MONTHLY months ---
    _seen_months="$(mktemp)"
    _month_count=0
    printf '%s\n' "$_all" | while IFS= read -r _b; do
        [ -n "$_b" ] || continue
        _mk="$(dump_month_key "$_b")"
        if ! grep -qx "$_mk" "$_seen_months"; then
            _month_count=$(( _month_count + 1 ))
            [ "$_month_count" -le "$BACKUP_KEEP_MONTHLY" ] || continue
            printf '%s\n' "$_mk" >> "$_seen_months"
            printf '%s\n' "$_b" >> "$_keep_file"
        fi
    done
    rm -f "$_seen_months"

    # -- Delete everything not in the keep set --------------------------------
    _deleted=0
    printf '%s\n' "$_all" | while IFS= read -r _b; do
        [ -n "$_b" ] || continue
        if ! grep -qx "$_b" "$_keep_file"; then
            _stem="$(printf '%s' "$_b" | sed 's/\.dump.*$//')"
            rm -f "${BACKUP_DIR}/${_b}" "${BACKUP_DIR}/${_stem}.manifest.json"
            log info prune "deleted $_b"
            _deleted=$(( _deleted + 1 ))
        fi
    done
    rm -f "$_keep_file"
    log info prune "retention applied (daily=$BACKUP_KEEP_DAILY weekly=$BACKUP_KEEP_WEEKLY monthly=$BACKUP_KEEP_MONTHLY)"
}

# --------------------------------------------------------------------------
# Off-site sync + alerting hooks (optional, §8/§10)
# --------------------------------------------------------------------------

# sync_offsite -> run the operator-provided off-site copy command, if any.
# A local backup does not survive a total loss of the server (§8).
sync_offsite() {
    [ -n "$BACKUP_SYNC_CMD" ] || return 0
    log info sync "running off-site sync hook"
    if sh -c "$BACKUP_SYNC_CMD"; then
        log info sync "off-site sync succeeded"
    else
        log error sync "off-site sync FAILED"
        alert error "off-site sync failed"
        return 1
    fi
}

# alert STATUS MESSAGE -> POST a small JSON payload to BACKUP_ALERT_WEBHOOK.
# Operational (not the app's own webhook/outbox) to avoid coupling (§10).
alert() {
    [ -n "$BACKUP_ALERT_WEBHOOK" ] || return 0
    command -v curl >/dev/null 2>&1 || { log warn alert "curl missing, cannot alert"; return 0; }
    curl -fsS -m 10 -X POST -H 'Content-Type: application/json' \
        -d "{\"status\":\"$1\",\"message\":\"$2\",\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" \
        "$BACKUP_ALERT_WEBHOOK" >/dev/null 2>&1 || log warn alert "alert POST failed"
}

# mark_success -> update the last-success marker used for trivial monitoring (§10).
mark_success() {
    date -u +%Y-%m-%dT%H:%M:%SZ > "${BACKUP_DIR}/.last-success"
}

# --------------------------------------------------------------------------
# Control surface for the read-only admin view (roadmap B6)
# --------------------------------------------------------------------------

# publish_index -> (re)write BACKUP_CONTROL_DIR/backups-index.json from the
# manifests, so the API can show the backup state WITHOUT any access to the
# dumps (concept §9). The index is metadata only: it embeds each manifest object
# plus the last-success / last-verify timestamps. Written atomically (temp file
# + mv) so the API never reads a half-written index. No-op when unconfigured.
publish_index() {
    [ -n "$BACKUP_CONTROL_DIR" ] || return 0
    mkdir -p "$BACKUP_CONTROL_DIR" 2>/dev/null || {
        log warn control "cannot create control dir $BACKUP_CONTROL_DIR"; return 0; }

    _ls=""; [ -f "${BACKUP_DIR}/.last-success" ] && _ls="$(cat "${BACKUP_DIR}/.last-success")"
    _lv=""; [ -f "${BACKUP_DIR}/.last-verify-success" ] && _lv="$(cat "${BACKUP_DIR}/.last-verify-success")"

    _idx_tmp="$(mktemp)"
    {
        printf '{\n'
        printf '  "generated_at": "%s",\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        printf '  "last_success": "%s",\n' "$_ls"
        printf '  "last_verify_success": "%s",\n' "$_lv"
        printf '  "backups": ['
        _first=1
        for _m in $(ls -1 "$BACKUP_DIR"/*.manifest.json 2>/dev/null | sort -r); do
            [ -f "$_m" ] || continue
            if [ "$_first" -eq 1 ]; then _first=0; else printf ','; fi
            printf '\n'
            cat "$_m"
        done
        printf '\n  ]\n}\n'
    } > "$_idx_tmp"

    mv "$_idx_tmp" "${BACKUP_CONTROL_DIR}/backups-index.json"
    chmod 0644 "${BACKUP_CONTROL_DIR}/backups-index.json" 2>/dev/null || true
    log info control "published backups-index.json"
}
