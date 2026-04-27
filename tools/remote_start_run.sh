#!/usr/bin/env bash
set -euo pipefail

PRESET="${1:-enhanced}"
ROUNDS="${2:-1}"
MODE="${3:-live}"
RUN_LABEL="${4:-}"
if [ "$#" -gt 4 ]; then
  EXTRA_ARGS=("${@:5}")
else
  EXTRA_ARGS=()
fi

ROOT_DIR="/opt/polybot/current"
RUNS_DIR="/opt/polybot/log/runs"
SHARED_CFG="/opt/polybot/shared/polymarket_config.json"
LOCAL_CFG="/root/.config/polymarket/config.json"

mkdir -p "$RUNS_DIR" /root/.config/polymarket

if [ -f "$SHARED_CFG" ]; then
  cp "$SHARED_CFG" "$LOCAL_CFG"
fi

STAMP="$(date -u '+%Y%m%dT%H%M%SZ')"
if [ -n "$RUN_LABEL" ]; then
  RUN_ID="${STAMP}_${RUN_LABEL}"
else
  RUN_ID="${STAMP}_${PRESET}_${MODE}_${ROUNDS}r"
fi
RUN_DIR="${RUNS_DIR}/${RUN_ID}"
mkdir -p "$RUN_DIR"

cd "$ROOT_DIR"

GIT_HEAD="$(git rev-parse --short HEAD)"
STARTED_AT="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
EXTRA_ARGS_TEXT=""
for ARG in "${EXTRA_ARGS[@]}"; do
  printf -v QUOTED_ARG '%q' "$ARG"
  EXTRA_ARGS_TEXT+=" ${QUOTED_ARG}"
done

cat > "${RUN_DIR}/meta.env" <<EOF
RUN_ID=${RUN_ID}
RUN_DIR=${RUN_DIR}
PRESET=${PRESET}
ROUNDS=${ROUNDS}
MODE=${MODE}
GIT_HEAD=${GIT_HEAD}
STARTED_AT=${STARTED_AT}
ROOT_DIR=${ROOT_DIR}
EXTRA_ARGS=${EXTRA_ARGS_TEXT}
EOF

cat > "${RUN_DIR}/run.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd "${ROOT_DIR}"
mkdir -p log
find log -maxdepth 1 -type f \( -name '*_trade.log*' -o -name '*_trade.jsonl*' \) -delete
if [ "${MODE}" = "dry" ]; then
  polybot-run --preset "${PRESET}" --rounds "${ROUNDS}" --dry${EXTRA_ARGS_TEXT}
else
  polybot-run --preset "${PRESET}" --rounds "${ROUNDS}"${EXTRA_ARGS_TEXT}
fi
EOF
chmod +x "${RUN_DIR}/run.sh"

nohup setsid bash -lc "
  set -euo pipefail
  RC=0
  echo \"\$\$\" > '${RUN_DIR}/pgid'
  if ! '${RUN_DIR}/run.sh' >'${RUN_DIR}/stdout.log' 2>&1; then
    RC=\$?
  fi
  printf '%s\n' \"\${RC}\" > '${RUN_DIR}/exit_code'
  date -u '+%Y-%m-%dT%H:%M:%SZ' > '${RUN_DIR}/finished_at'
  find '${ROOT_DIR}/log' -maxdepth 1 -type f \\( -name '*_trade.log*' -o -name '*_trade.jsonl*' \\) -exec cp {} '${RUN_DIR}/' \\;
" </dev/null >/dev/null 2>&1 &
RUN_PID=$!

echo "${RUN_PID}" > "${RUN_DIR}/pid"
echo "PID=${RUN_PID}" >> "${RUN_DIR}/meta.env"
echo "PGID=${RUN_PID}" >> "${RUN_DIR}/meta.env"

ln -sfn "${RUN_DIR}" "${RUNS_DIR}/latest"

sleep 3

STATUS="running"
EXIT_CODE_VALUE=""
if [ -f "${RUN_DIR}/exit_code" ]; then
  STATUS="failed"
  EXIT_CODE_VALUE="$(cat "${RUN_DIR}/exit_code" 2>/dev/null || true)"
elif ! kill -0 "${RUN_PID}" 2>/dev/null; then
  STATUS="exited"
fi

printf 'RUN_ID=%s\nRUN_DIR=%s\nPID=%s\nGIT_HEAD=%s\nSTATUS=%s\n' \
  "${RUN_ID}" "${RUN_DIR}" "${RUN_PID}" "${GIT_HEAD}" "${STATUS}"

if [ -n "${EXIT_CODE_VALUE}" ]; then
  printf 'EXIT_CODE=%s\n' "${EXIT_CODE_VALUE}"
fi

if [ -f "${RUN_DIR}/stdout.log" ]; then
  echo "STDOUT_TAIL_BEGIN"
  tail -n 20 "${RUN_DIR}/stdout.log" || true
  echo "STDOUT_TAIL_END"
fi
