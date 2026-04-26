#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
usage: tools/vps_start_run.sh --host <host> [--user root] [--preset enhanced] [--rounds 1] [--dry] [--label LABEL]
EOF
}

HOST=""
USER_NAME="${POLYBOT_VPS_USER:-root}"
PRESET="${POLYBOT_VPS_PRESET:-enhanced}"
ROUNDS="${POLYBOT_VPS_ROUNDS:-1}"
MODE="live"
LABEL=""

while [ $# -gt 0 ]; do
  case "$1" in
    --host)
      HOST="$2"
      shift 2
      ;;
    --user)
      USER_NAME="$2"
      shift 2
      ;;
    --preset)
      PRESET="$2"
      shift 2
      ;;
    --rounds)
      ROUNDS="$2"
      shift 2
      ;;
    --dry)
      MODE="dry"
      shift
      ;;
    --label)
      LABEL="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown arg: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [ -z "$HOST" ]; then
  echo "--host is required" >&2
  usage >&2
  exit 1
fi

REMOTE="${USER_NAME}@${HOST}"
REMOTE_CMD="polybot-update && /usr/local/bin/polybot-remote-start '${PRESET}' '${ROUNDS}' '${MODE}' '${LABEL}'"

echo "remote=${REMOTE}"
echo "preset=${PRESET} rounds=${ROUNDS} mode=${MODE}"
ssh "$REMOTE" "$REMOTE_CMD"
