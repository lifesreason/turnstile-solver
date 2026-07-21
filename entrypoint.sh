#!/usr/bin/env bash
set -euo pipefail
cd /app

HOST="${TURNSTILE_HOST:-0.0.0.0}"
PORT="${TURNSTILE_PORT:-5072}"
THREAD="${TURNSTILE_THREAD:-1}"
INSTANCES="${TURNSTILE_BROWSER_INSTANCES:-1}"
BROWSER_TYPE="${TURNSTILE_BROWSER_TYPE:-camoufox}"
KEEP_ALIVE="${TURNSTILE_KEEP_BROWSER_ALIVE:-0}"
DEBUG_FLAG=()
if [[ "${TURNSTILE_DEBUG:-0}" == "1" || "${TURNSTILE_DEBUG:-false}" == "true" ]]; then
  DEBUG_FLAG=(--debug)
fi

PROXY_FLAG=()
if [[ "${TURNSTILE_PROXY:-0}" == "1" || "${TURNSTILE_PROXY:-false}" == "true" ]]; then
  PROXY_FLAG=(--proxy)
fi

mkdir -p /app/logs /app/keys

if [[ "${BROWSER_TYPE}" == "camoufox" ]]; then
  CAMOUFOX_DIR="${HOME:-/root}/.local/share/camoufox"
  if [[ ! -d "${CAMOUFOX_DIR}" ]] || [[ -z "$(find "${CAMOUFOX_DIR}" -mindepth 1 -maxdepth 1 2>/dev/null | head -1)" ]]; then
    echo "[turnstile-solver] Camoufox browser assets missing; downloading to ${CAMOUFOX_DIR}"
    python -m camoufox fetch
  fi
fi

echo "[turnstile-solver] browser=${BROWSER_TYPE} concurrency_slots=${THREAD} browser_instances=${INSTANCES} keep_alive=${KEEP_ALIVE} ${HOST}:${PORT} lazy=${TURNSTILE_LAZY:-1}"
exec python api_solver.py \
  --browser_type "${BROWSER_TYPE}" \
  --thread "${THREAD}" \
  --host "${HOST}" \
  --port "${PORT}" \
  "${DEBUG_FLAG[@]}" \
  "${PROXY_FLAG[@]}"
