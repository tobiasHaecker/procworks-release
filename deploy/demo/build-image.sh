#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
#
# Build the demo image from the CURRENT state of the PUBLIC release repo, so
# every NEW demo instance runs exactly the released, customer-facing code --
# never internal-only working state from the private repo. Clones the release
# repo shallow and builds via Fly's REMOTE builder (no local Docker), pushing
# registry.fly.io/<app>:<label>. New Fly Machines pull that tag on create, so
# after this runs every fresh trial reflects the release repo's HEAD.
#
# Run this after each release (or on a schedule -- see
# .github/workflows/demo-image.yml). Prereqs: flyctl on PATH + FLY_API_TOKEN.
#
# Env (with defaults):
#   RELEASE_REPO  (https://github.com/tobiasHaecker/procworks-release.git)
#   FLY_APP_NAME  (procworks-demo)   IMAGE_LABEL (demo)   FLY_API_TOKEN (required)
set -euo pipefail

RELEASE_REPO="${RELEASE_REPO:-https://github.com/tobiasHaecker/procworks-release.git}"
APP="${FLY_APP_NAME:-procworks-demo}"
LABEL="${IMAGE_LABEL:-demo}"
: "${FLY_API_TOKEN:?set FLY_API_TOKEN (fly auth token)}"

work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT

echo "==> cloning ${RELEASE_REPO} (shallow)"
git clone --depth 1 "${RELEASE_REPO}" "${work}/release"
cd "${work}/release"
rev="$(git rev-parse --short HEAD)"

if [ ! -f deploy/demo/Dockerfile ]; then
  echo "ERROR: the release repo has no deploy/demo/ yet." >&2
  echo "       It is mirrored there only on a release/tag (or manual sync);" >&2
  echo "       cut a release first, then re-run this script." >&2
  exit 1
fi

echo "==> building + pushing registry.fly.io/${APP}:${LABEL} from release@${rev}"
# fly resolves [build].dockerfile in fly.toml relative to the config dir; the
# build context is the cwd (the release repo root, which has core/ + web/).
fly deploy --config deploy/demo/fly.toml --build-only --push --image-label "${LABEL}" -a "${APP}"

echo "==> done: registry.fly.io/${APP}:${LABEL} now reflects release@${rev}"
echo "    New trials created after this point run the released code."
