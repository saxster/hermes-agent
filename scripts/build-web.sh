#!/usr/bin/env bash
# build-web.sh — build the Hermes web dashboard SPA.
#
# Populates hermes_cli/web_dist/ from web/ via Vite. Invoked by
# `hermes web --build-frontend` and by release/CI pipelines. Runnable
# from any cwd — the script re-anchors relative to its own location.
#
# Package-manager selection matches the lockfile present in web/:
#   - pnpm-lock.yaml     → pnpm install --frozen-lockfile
#   - package-lock.json  → npm ci
#   - (no lockfile)      → prefer pnpm install, else npm install
# Exits non-zero with a clear message if no compatible tool is found.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WEB_DIR="${SCRIPT_DIR}/../web"

if [[ ! -d "${WEB_DIR}" ]]; then
  echo "build-web.sh: frontend sources not found at ${WEB_DIR}" >&2
  exit 1
fi

cd "${WEB_DIR}"

has_pnpm() { command -v pnpm >/dev/null 2>&1; }
has_npm()  { command -v npm  >/dev/null 2>&1; }

if [[ -f "pnpm-lock.yaml" ]] && has_pnpm; then
  echo "build-web.sh: using pnpm (pnpm-lock.yaml present)"
  pnpm install --frozen-lockfile
  pnpm run build
elif [[ -f "package-lock.json" ]] && has_npm; then
  echo "build-web.sh: using npm (package-lock.json present)"
  npm ci
  npm run build
elif has_pnpm; then
  echo "build-web.sh: no lockfile matched — using pnpm install"
  pnpm install
  pnpm run build
elif has_npm; then
  echo "build-web.sh: no lockfile matched — using npm install"
  npm install
  npm run build
else
  echo "build-web.sh: neither pnpm nor npm found on PATH." >&2
  echo "  Install pnpm (https://pnpm.io) or npm, then retry." >&2
  exit 1
fi

echo "build-web.sh: frontend built to hermes_cli/web_dist/"
