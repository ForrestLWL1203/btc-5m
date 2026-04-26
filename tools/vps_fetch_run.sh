#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
usage: tools/vps_fetch_run.sh --host <host> [--user root] [--run-id ID|latest] [--dest DIR]
EOF
}

HOST=""
USER_NAME="${POLYBOT_VPS_USER:-root}"
RUN_ID="latest"
DEST_ROOT="${POLYBOT_VPS_FETCH_DIR:-remote_runs}"

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
    --run-id)
      RUN_ID="$2"
      shift 2
      ;;
    --dest)
      DEST_ROOT="$2"
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
REMOTE_DIR="$(ssh "$REMOTE" "if [ '${RUN_ID}' = 'latest' ]; then readlink -f /opt/polybot/log/runs/latest; else echo /opt/polybot/log/runs/'${RUN_ID}'; fi")"

if [ -z "$REMOTE_DIR" ]; then
  echo "remote run dir not found" >&2
  exit 1
fi

RUN_BASENAME="$(basename "$REMOTE_DIR")"
HOST_SAFE="${HOST//./_}"
DEST_DIR="${DEST_ROOT}/${HOST_SAFE}"
mkdir -p "$DEST_DIR"

scp -r "${REMOTE}:${REMOTE_DIR}" "${DEST_DIR}/"
echo "fetched=${DEST_DIR}/${RUN_BASENAME}"
