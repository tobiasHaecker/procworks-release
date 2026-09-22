#!/bin/sh
# SPDX-License-Identifier: BUSL-1.1
# ProcWorks -- Selbsttest einer frischen Installation (Docker-Compose-Stack).
#
# Prueft nach `docker compose -f deploy/docker-compose.full.yml up -d`, ob der
# Stack wirklich einsatzbereit ist, und misst die Zeit:
#
#   1. alle Dienste laufen (postgres, api, web, backup)
#   2. die API antwortet (/api/health) und nennt ihre Version
#   3. die Oberflaeche wird ausgeliefert
#   4. das Einmal-Passwort des Administrators steht im Log
#   5. eine Sicherung laesst sich anlegen ...
#   6. ... und ist wiederherstellbar (verify.sh spielt sie in eine
#      Wegwerf-Datenbank ein und prueft sie; die echte Datenbank bleibt unberuehrt)
#
# Nichts davon aendert Daten der Installation -- ausser dass eine zusaetzliche
# Sicherung im Sicherungs-Volume liegt (sie faellt unter die normale
# Aufbewahrung). Ausgabe: je Schritt OK/FEHLER mit Sekunden seit Start,
# Exitcode 0 nur, wenn alles bestanden ist.
#
# Aufruf (aus dem Verzeichnis, in das ProcWorks geklont wurde):
#   sh deploy/selftest.sh
# Unter Windows in WSL oder Git Bash. Optionen per Umgebung:
#   PROCWORKS_URL   Basis-URL der Oberflaeche (Standard http://localhost)
#   COMPOSE_FILE    Compose-Datei (Standard deploy/docker-compose.full.yml)
#   WAIT_SECONDS    wie lange auf die API gewartet wird (Standard 300)
#
# Muss POSIX-sh bleiben (wie deploy/backup/*.sh) -- kein bash vorausgesetzt.

set -u

URL="${PROCWORKS_URL:-http://localhost}"
COMPOSE_FILE="${COMPOSE_FILE:-deploy/docker-compose.full.yml}"
WAIT_SECONDS="${WAIT_SECONDS:-300}"
START=$(date +%s)
FAILED=0

elapsed() { echo $(( $(date +%s) - START )); }
ok()   { printf '  [OK]     %4ss  %s\n' "$(elapsed)" "$1"; }
fail() { printf '  [FEHLER] %4ss  %s\n' "$(elapsed)" "$1"; FAILED=1; }
dc()   { docker compose -f "$COMPOSE_FILE" "$@"; }

command -v docker >/dev/null 2>&1 || { echo "docker nicht gefunden"; exit 2; }
command -v curl >/dev/null 2>&1 || { echo "curl nicht gefunden"; exit 2; }
[ -f "$COMPOSE_FILE" ] || { echo "$COMPOSE_FILE fehlt -- aus dem ProcWorks-Verzeichnis aufrufen"; exit 2; }

echo "ProcWorks-Selbsttest gegen $URL ($(date '+%Y-%m-%d %H:%M:%S'))"

# 1. Dienste
running=$(dc ps --status running --services 2>/dev/null | sort | tr '\n' ' ')
missing=""
for svc in postgres api web backup; do
    case " $running " in *" $svc "*) ;; *) missing="$missing $svc" ;; esac
done
if [ -z "$missing" ]; then ok "Dienste laufen: $running"; else fail "nicht laufend:$missing"; fi

# 2. API (wartet, weil die API beim Start die Datenbank migriert)
health=""
deadline=$(( $(date +%s) + WAIT_SECONDS ))
while [ "$(date +%s)" -lt "$deadline" ]; do
    health=$(curl -fsS -m 5 "$URL/api/health" 2>/dev/null) && break
    health=""
    sleep 3
done
if [ -n "$health" ]; then ok "API antwortet: $health"; else fail "API antwortet nicht unter $URL/api/health"; fi

# 3. Oberflaeche
if curl -fsS -m 10 "$URL/" 2>/dev/null | grep -q "ProcWorks"; then
    ok "Oberflaeche wird ausgeliefert"
else
    fail "Oberflaeche nicht erreichbar unter $URL/"
fi

# 4. Einmal-Passwort (nur Hinweis, wird nicht ausgegeben)
if dc logs api 2>/dev/null | grep -q "Initial admin"; then
    ok "Einmal-Passwort des Administrators steht im API-Log (docker compose ... logs api)"
else
    fail "kein Einmal-Passwort im API-Log (schon geaendert? dann ist das in Ordnung)"
fi

# 5./6. Sicherung anlegen und pruefen
if dc exec -T backup sh /opt/backup/backup-once.sh >/dev/null 2>&1; then
    ok "Sicherung angelegt"
    if dc exec -T backup sh /opt/backup/verify.sh --latest >/dev/null 2>&1; then
        ok "Sicherung ist wiederherstellbar (Probe-Wiederherstellung in Wegwerf-Datenbank)"
    else
        fail "Sicherung liess sich nicht pruefen (docker compose ... exec backup sh /opt/backup/verify.sh --latest)"
    fi
else
    fail "Sicherung konnte nicht angelegt werden"
fi

echo
if [ "$FAILED" -eq 0 ]; then
    echo "Ergebnis: bestanden in $(elapsed) s."
    exit 0
fi
echo "Ergebnis: NICHT bestanden (Details oben)."
exit 1
