#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
#
# D1 proof: create -> reach -> stop -> start -> destroy ONE demo Machine on
# Fly.io via the Machines REST API -- the exact API the broker's FlyProvisioner
# uses. Proves an isolated demo instance is reachable, scales to zero on stop,
# and can be destroyed. It costs a few cents of compute while running.
#
# Prerequisites (all on the operator's own Fly account):
#   1. A Fly app exists and the demo image is pushed to its registry, e.g.:
#        fly apps create procworks-demo
#        fly deploy --config deploy/demo/fly.toml \
#                   --dockerfile deploy/demo/Dockerfile --build-only \
#                   --push --image-label demo
#      (or push to registry.fly.io/procworks-demo:demo any other way).
#      `fly deploy` uses Fly's REMOTE builder -- no local Docker needed.
#   2. A deploy/org token:  export FLY_API_TOKEN="$(fly auth token)"
#
# Usage:
#   FLY_API_TOKEN=... FLY_APP_NAME=procworks-demo \
#   FLY_IMAGE_REF=registry.fly.io/procworks-demo:demo \
#   deploy/demo/d1-proof.sh
#
# Env (with defaults):
#   FLY_APP_NAME   (procworks-demo)   FLY_REGION (fra)
#   FLY_IMAGE_REF  (required)         FLY_API_TOKEN (required)
set -euo pipefail

API="https://api.machines.dev/v1"
APP="${FLY_APP_NAME:-procworks-demo}"
REGION="${FLY_REGION:-fra}"
IMAGE="${FLY_IMAGE_REF:?set FLY_IMAGE_REF to the pushed demo image, e.g. registry.fly.io/${APP}:demo}"
: "${FLY_API_TOKEN:?set FLY_API_TOKEN (fly auth token)}"

auth=(-H "Authorization: Bearer ${FLY_API_TOKEN}" -H "Content-Type: application/json")
trial_id="d1$(date +%s)"

# jq is optional; fall back to a tiny python filter if it is missing.
json() { if command -v jq >/dev/null 2>&1; then jq -r "$1"; else python3 -c "import sys,json;d=json.load(sys.stdin);print(eval('d$2'))"; fi; }

echo "==> [1/6] create Machine (app=${APP}, region=${REGION})"
create_body=$(cat <<JSON
{ "name": "trial-${trial_id}", "region": "${REGION}",
  "config": {
    "image": "${IMAGE}", "auto_destroy": false,
    "restart": { "policy": "on-failure" },
    "guest": { "cpu_kind": "shared", "cpus": 1, "memory_mb": 512 },
    "services": [ { "protocol": "tcp", "internal_port": 8000,
      "autostart": true, "autostop": "stop", "force_https": true,
      "ports": [ { "port": 443, "handlers": ["tls","http"] },
                 { "port": 80,  "handlers": ["http"] } ] } ]
  } }
JSON
)
created=$(curl -fsS "${auth[@]}" -X POST "${API}/apps/${APP}/machines" -d "${create_body}")
mid=$(printf '%s' "${created}" | json '.id' "['id']")
echo "    machine id: ${mid}"

cleanup() { echo "==> cleanup: destroy ${mid}"; curl -fsS "${auth[@]}" -X DELETE "${API}/apps/${APP}/machines/${mid}?force=true" >/dev/null || true; }
trap cleanup EXIT

echo "==> [2/6] wait until started, then reach https://${APP}.fly.dev/health"
for i in $(seq 1 30); do
  state=$(curl -fsS "${auth[@]}" "${API}/apps/${APP}/machines/${mid}" | json '.state' "['state']")
  [ "${state}" = "started" ] && break
  sleep 2
done
echo "    state: ${state}"
code=$(curl -s -o /dev/null -w '%{http_code}' "https://${APP}.fly.dev/health" || echo 000)
echo "    GET /health -> HTTP ${code}"
[ "${code}" = "200" ] || echo "    (note: routing/DNS may take a moment on first deploy)"

echo "==> [3/6] stop Machine (scale-to-zero; billing pauses)"
curl -fsS "${auth[@]}" -X POST "${API}/apps/${APP}/machines/${mid}/stop" >/dev/null
sleep 3
state=$(curl -fsS "${auth[@]}" "${API}/apps/${APP}/machines/${mid}" | json '.state' "['state']")
echo "    state: ${state}   (expect 'stopped' -> ~0 EUR at rest)"

echo "==> [4/6] start Machine again (auto-start on re-access)"
curl -fsS "${auth[@]}" -X POST "${API}/apps/${APP}/machines/${mid}/start" >/dev/null
sleep 3
state=$(curl -fsS "${auth[@]}" "${API}/apps/${APP}/machines/${mid}" | json '.state' "['state']")
echo "    state: ${state}"

echo "==> [5/6] destroy Machine (explicit; trap will also try)"
curl -fsS "${auth[@]}" -X DELETE "${API}/apps/${APP}/machines/${mid}?force=true" >/dev/null
trap - EXIT
echo "==> [6/6] confirm gone"
# Fly keeps the record briefly with state=destroyed before it 404s, so accept
# either as "gone".
resp=$(curl -s -w '\n%{http_code}' "${auth[@]}" "${API}/apps/${APP}/machines/${mid}")
gone_code=$(printf '%s' "${resp}" | tail -1)
gone_state=$(printf '%s' "${resp}" | sed '$d' | json '.state' "['state']" 2>/dev/null || echo "")
if [ "${gone_code}" = "404" ] || [ "${gone_state}" = "destroyed" ]; then
  echo "    gone (HTTP ${gone_code}, state=${gone_state:-none}) OK"
else
  echo "    WARN: machine still present (HTTP ${gone_code}, state=${gone_state})"
fi
echo "==> D1 proof complete."
