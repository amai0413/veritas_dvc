#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

export PYTHONUNBUFFERED=1
export SEARCHER_AGENT_URL="http://127.0.0.1:5001/search"
export EXTRACTOR_AGENT_URL="http://127.0.0.1:5002/extract"
export FRIEND_AGENT_URL="http://127.0.0.1:5003"

PIDS=()

cleanup() {
  trap - INT TERM EXIT
  if [ "${#PIDS[@]}" -gt 0 ]; then
    kill "${PIDS[@]}" 2>/dev/null || true
    wait "${PIDS[@]}" 2>/dev/null || true
  fi
}
trap cleanup INT TERM EXIT

start_internal_service() {
  local name="$1"
  local module="$2"
  local port="$3"

  echo "Starting ${name} on internal port ${port}..."
  gunicorn \
    --bind "127.0.0.1:${port}" \
    --workers 1 \
    --threads 4 \
    --timeout 120 \
    --access-logfile - \
    --error-logfile - \
    "${module}" &
  PIDS+=("$!")
}

wait_for_health() {
  local name="$1"
  local port="$2"

  for _ in $(seq 1 60); do
    if python - "$port" <<'PY' >/dev/null 2>&1
import sys
import urllib.request

with urllib.request.urlopen(
    f"http://127.0.0.1:{sys.argv[1]}/health", timeout=2
) as response:
    raise SystemExit(0 if response.status == 200 else 1)
PY
    then
      echo "${name} is healthy."
      return 0
    fi
    sleep 1
  done

  echo "${name} failed its health check." >&2
  return 1
}

start_internal_service "Searcher" "src.veritas_searcher_agent:app" 5001
wait_for_health "Searcher" 5001

start_internal_service "Extractor" "src.veritas_extractor_agent:app" 5002
wait_for_health "Extractor" 5002

start_internal_service "Judge" "src.veritas_browser_agent_fixed:app" 5003
wait_for_health "Judge" 5003

PUBLIC_PORT="${PORT:-5000}"
echo "Starting public UI on 0.0.0.0:${PUBLIC_PORT}..."
gunicorn \
  --bind "0.0.0.0:${PUBLIC_PORT}" \
  --workers 1 \
  --threads 8 \
  --timeout 180 \
  --access-logfile - \
  --error-logfile - \
  "app:app" &
UI_PID="$!"
PIDS+=("$UI_PID")

wait "$UI_PID"
