#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

CONFIG_PATH="${1:-paired_window_early_entry_dry.yaml}"
ROUNDS="${2:-108}"
CHECK_INTERVAL_SEC="${CHECK_INTERVAL_SEC:-3600}"
BOOTSTRAP_WAIT_SEC="${BOOTSTRAP_WAIT_SEC:-30}"
BOOTSTRAP_POLL_SEC="${BOOTSTRAP_POLL_SEC:-5}"

STAMP="$(date '+%Y%m%d_%H%M%S')"
SUPERVISOR_LOG="log/live_supervisor_${STAMP}.log"
BOT_STDOUT_LOG="log/live_run_${STAMP}.log"
PID_FILE="log/live_run_${STAMP}.pid"

mkdir -p log

{
  echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] supervisor_start config=${CONFIG_PATH} rounds=${ROUNDS} check_interval=${CHECK_INTERVAL_SEC}s"
  echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] bot_stdout_log=${BOT_STDOUT_LOG}"
} >> "$SUPERVISOR_LOG"

nohup caffeinate -dimsu stdbuf -oL -eL python3.11 run.py --config "$CONFIG_PATH" --rounds "$ROUNDS" >> "$BOT_STDOUT_LOG" 2>&1 &
BOT_PID=$!
echo "$BOT_PID" > "$PID_FILE"

echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] bot_started pid=${BOT_PID}" >> "$SUPERVISOR_LOG"

elapsed=0
while [ "$elapsed" -lt "$BOOTSTRAP_WAIT_SEC" ]; do
  if ! kill -0 "$BOT_PID" 2>/dev/null; then
    wait "$BOT_PID" || true
    echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] bot_exited_during_bootstrap" >> "$SUPERVISOR_LOG"
    tail -n 40 "$BOT_STDOUT_LOG" >> "$SUPERVISOR_LOG" 2>/dev/null || true
    exit 0
  fi
  if grep -q 'RUN_START:' "$BOT_STDOUT_LOG"; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] bootstrap_ok" >> "$SUPERVISOR_LOG"
    break
  fi
  sleep "$BOOTSTRAP_POLL_SEC"
  elapsed=$((elapsed + BOOTSTRAP_POLL_SEC))
done

while true; do
  if ! kill -0 "$BOT_PID" 2>/dev/null; then
    wait "$BOT_PID" || true
    echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] bot_exited" >> "$SUPERVISOR_LOG"
    exit 0
  fi

  if grep -qE 'STOP_INSUFFICIENT_FUNDS|INSUFFICIENT_FUNDS' "$BOT_STDOUT_LOG"; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] insufficient_funds_detected stopping pid=${BOT_PID}" >> "$SUPERVISOR_LOG"
    kill "$BOT_PID" 2>/dev/null || true
    wait "$BOT_PID" || true
    exit 0
  fi

  {
    echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] hourly_heartbeat pid=${BOT_PID}"
    tail -n 20 "$BOT_STDOUT_LOG" | sed 's/^/  /'
  } >> "$SUPERVISOR_LOG"

  sleep "$CHECK_INTERVAL_SEC"
done
