#!/bin/bash
set -u

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$ROOT_DIR/.venv/bin/python"
LOG_DIR="$ROOT_DIR/logs"
RUN_DIR="$ROOT_DIR/.run"

cd "$ROOT_DIR"

if [ ! -x "$PYTHON" ]; then
  echo "Error: virtual environment not found at $PYTHON"
  exit 1
fi

if [ -f "$ROOT_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT_DIR/.env"
  set +a
fi

mkdir -p "$LOG_DIR" "$RUN_DIR"

echo "Stopping old Veritas servers..."
"$ROOT_DIR/stop_all.sh" >/dev/null 2>&1 || true

start_service() {
  name="$1"
  script="$2"
  port="$3"
  log_file="$LOG_DIR/$name.log"
  pid_file="$RUN_DIR/$name.pid"

  echo "Starting $name on port $port..."
  : > "$log_file"
  nohup env PYTHONUNBUFFERED=1 \
    SEARCHER_AGENT_URL="http://127.0.0.1:5001/search" \
    EXTRACTOR_AGENT_URL="http://127.0.0.1:5002/extract" \
    FRIEND_AGENT_URL="http://127.0.0.1:5003" \
    "$PYTHON" "$script" >> "$log_file" 2>&1 < /dev/null &
  echo "$!" > "$pid_file"
}

wait_for_health() {
  name="$1"
  port="$2"
  pid_file="$RUN_DIR/$name.pid"

  attempt=0
  while [ "$attempt" -lt 30 ]; do
    if curl -fsS --max-time 2 "http://127.0.0.1:$port/health" >/dev/null 2>&1; then
      echo "  ✓ $name is healthy"
      return 0
    fi
    if [ -f "$pid_file" ] && ! kill -0 "$(cat "$pid_file")" 2>/dev/null; then
      echo "  ✗ $name exited during startup. See logs/$name.log"
      return 1
    fi
    attempt=$((attempt + 1))
    sleep 1
  done

  echo "  ✗ $name did not become healthy. See logs/$name.log"
  return 1
}

start_service searcher src/veritas_searcher_agent.py 5001
wait_for_health searcher 5001 || exit 1

start_service extractor src/veritas_extractor_agent.py 5002
wait_for_health extractor 5002 || exit 1

start_service judge src/veritas_browser_agent_fixed.py 5003
wait_for_health judge 5003 || exit 1

start_service ui app.py 5000
wait_for_health ui 5000 || exit 1

echo ""
echo "Veritas is running:"
echo "  UI:       http://127.0.0.1:5000"
echo "  Searcher: http://127.0.0.1:5001"
echo "  Extractor:http://127.0.0.1:5002"
echo "  Judge:    http://127.0.0.1:5003"
echo ""
echo "Run ./stop_all.sh to stop all services."

if [ "${OPEN_UI:-0}" = "1" ] && command -v open >/dev/null 2>&1; then
  open http://127.0.0.1:5000
fi

# Useful for IDE tasks, containers, and process supervisors that clean up
# detached child processes as soon as this script exits.
if [ "${FOREGROUND:-0}" = "1" ]; then
  echo "Running in foreground mode. Press Ctrl+C to stop all services."
  trap '"$ROOT_DIR/stop_all.sh"; exit 0' INT TERM
  while true; do
    sleep 3600
  done
fi
