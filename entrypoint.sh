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
  CAMOUFOX_DIR="${HOME:-/root}/.cache/camoufox"
  CAMOUFOX_MIN_CACHE_MB="${CAMOUFOX_MIN_CACHE_MB:-500}"
  CAMOUFOX_CACHE_MB=0
  if [[ -d "${CAMOUFOX_DIR}" ]]; then
    CAMOUFOX_CACHE_MB="$(du -sm "${CAMOUFOX_DIR}" 2>/dev/null | awk '{print $1}')"
  fi
  if [[ "${CAMOUFOX_CACHE_MB:-0}" -lt "${CAMOUFOX_MIN_CACHE_MB}" ]]; then
    echo "[turnstile-solver] ERROR: Camoufox browser assets are missing from ${CAMOUFOX_DIR}" >&2
    echo "[turnstile-solver] Rebuild the image so Camoufox is bundled into it." >&2
    exit 1
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
