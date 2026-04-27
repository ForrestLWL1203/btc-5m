#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

DEFAULT_REPO="https://github.com/ForrestLWL1203/btc-5m.git"
DEFAULT_BRANCH="main"
DEFAULT_USER="root"
DEFAULT_DEST_ROOT="remote_runs"
DEFAULT_PROFILE_HOME="${HOME}/.polybot"

usage() {
  cat <<'EOF'
usage:
  tools/vpsctl.sh bootstrap --host <ip> [--user root]
                          [--repo URL] [--branch main] [--account-profile NAME|PATH]
  tools/vpsctl.sh run      --host <ip> [--user root] [--preset enhanced] [--rounds 6] [--dry] [--label LABEL]
  tools/vpsctl.sh stop     --host <ip> [--user root] [--run-id latest]
  tools/vpsctl.sh fetch    --host <ip> [--user root] [--run-id latest] [--dest remote_runs]
  tools/vpsctl.sh status   --host <ip> [--user root] [--run-id latest]
  tools/vpsctl.sh collect  --host <ip> [--user root] [--windows 96] [--label LABEL] [collector args...]
  tools/vpsctl.sh probe    --host <ip> [--user root] --token-id <TOKEN> [extra probe args...]

profile options:
  --vps-profile NAME|PATH
  --account-profile NAME|PATH

default profile locations:
  ~/.polybot/vps/<name>.env
  ~/.polybot/accounts/<name>.json

password source:
  Put `PASSWORD=...` or `PASSWORD_ENV_VAR=...` inside the VPS profile.
EOF
}

die() {
  echo "$*" >&2
  exit 1
}

require_file() {
  [ -f "$1" ] || die "missing file: $1"
}

PASSWORD="${POLYBOT_VPS_PASSWORD:-}"
HOST=""
USER_NAME="${POLYBOT_VPS_USER:-$DEFAULT_USER}"
REPO_URL="${POLYBOT_VPS_REPO:-$DEFAULT_REPO}"
BRANCH="${POLYBOT_VPS_BRANCH:-$DEFAULT_BRANCH}"
DEST_ROOT="${POLYBOT_VPS_FETCH_DIR:-$DEFAULT_DEST_ROOT}"
RUN_ID="latest"
PRESET="${POLYBOT_VPS_PRESET:-enhanced}"
ROUNDS="${POLYBOT_VPS_ROUNDS:-1}"
MODE="live"
LABEL=""
TOKEN_ID=""
VPS_PROFILE=""
ACCOUNT_PROFILE=""
RUN_EXTRA_ARGS=()
COLLECT_ARGS=()
PROFILE_HOME="${POLYBOT_PROFILE_HOME:-$DEFAULT_PROFILE_HOME}"

EXPECT_TIMEOUT="${POLYBOT_EXPECT_TIMEOUT:-120}"
REMAINING_ARGS=()

resolve_vps_profile_path() {
  local value="$1"
  if [[ "$value" == */* ]] || [[ "$value" == *.env ]]; then
    printf '%s\n' "$value"
  else
    printf '%s\n' "${PROFILE_HOME}/vps/${value}.env"
  fi
}

resolve_account_profile_path() {
  local value="$1"
  if [[ "$value" == */* ]] || [[ "$value" == *.json ]]; then
    printf '%s\n' "$value"
  else
    printf '%s\n' "${PROFILE_HOME}/accounts/${value}.json"
  fi
}

load_vps_profile() {
  [ -n "$VPS_PROFILE" ] || return 0
  local profile_path
  profile_path="$(resolve_vps_profile_path "$VPS_PROFILE")"
  require_file "$profile_path"

  local old_host="$HOST"
  local old_user="$USER_NAME"
  local old_password="$PASSWORD"
  local old_repo="$REPO_URL"
  local old_branch="$BRANCH"
  local old_password_env="${PASSWORD_ENV_VAR:-}"

  # shellcheck disable=SC1090
  source "$profile_path"

  HOST="${HOST:-$old_host}"
  USER_NAME="${USER_NAME:-$old_user}"
  PASSWORD="${PASSWORD:-$old_password}"
  REPO_URL="${REPO_URL:-$old_repo}"
  BRANCH="${BRANCH:-$old_branch}"
  PASSWORD_ENV_VAR="${PASSWORD_ENV_VAR:-$old_password_env}"
}

resolve_local_account_cfg() {
  if [ -n "$ACCOUNT_PROFILE" ]; then
    resolve_account_profile_path "$ACCOUNT_PROFILE"
  else
    printf '%s\n' "${HOME}/.config/polymarket/config.json"
  fi
}

read_password() {
  if [ -n "$PASSWORD" ]; then
    return
  fi
  if [ -n "${PASSWORD_ENV_VAR:-}" ]; then
    PASSWORD="${!PASSWORD_ENV_VAR:-}"
  fi
  [ -n "$PASSWORD" ] || die "password required: set PASSWORD or PASSWORD_ENV_VAR in the VPS profile (or POLYBOT_VPS_PASSWORD in the environment)"
}

expect_ssh() {
  local remote_cmd="$1"
  read_password
  SSHPASS="$PASSWORD" SSHUSER="$USER_NAME" SSHHOST="$HOST" SSHCMD="$remote_cmd" SSHTIMEOUT="$EXPECT_TIMEOUT" \
    /usr/bin/expect <<'EOF'
set timeout $env(SSHTIMEOUT)
set pass $env(SSHPASS)
set user $env(SSHUSER)
set host $env(SSHHOST)
set cmd $env(SSHCMD)
spawn ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null $user@$host $cmd
expect {
  -re ".*assword:.*" { send "$pass\r"; exp_continue }
  eof
}
catch wait result
set code [lindex $result 3]
exit $code
EOF
}

expect_scp_to_remote() {
  local src1="$1"
  local src2="$2"
  local remote_dir="$3"
  read_password
  SSHPASS="$PASSWORD" SSHUSER="$USER_NAME" SSHHOST="$HOST" SRC1="$src1" SRC2="$src2" REMOTEDIR="$remote_dir" SSHTIMEOUT="$EXPECT_TIMEOUT" \
    /usr/bin/expect <<'EOF'
set timeout $env(SSHTIMEOUT)
set pass $env(SSHPASS)
set user $env(SSHUSER)
set host $env(SSHHOST)
set src1 $env(SRC1)
set src2 $env(SRC2)
set remote_dir $env(REMOTEDIR)
spawn scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null $src1 $src2 ${user}@${host}:${remote_dir}
expect {
  -re ".*assword:.*" { send "$pass\r"; exp_continue }
  eof
}
catch wait result
set code [lindex $result 3]
exit $code
EOF
}

expect_scp_from_remote() {
  local remote_src="$1"
  local local_dest="$2"
  read_password
  SSHPASS="$PASSWORD" SSHUSER="$USER_NAME" SSHHOST="$HOST" REMOTESRC="$remote_src" LOCALDEST="$local_dest" SSHTIMEOUT="$EXPECT_TIMEOUT" \
    /usr/bin/expect <<'EOF'
set timeout $env(SSHTIMEOUT)
set pass $env(SSHPASS)
set user $env(SSHUSER)
set host $env(SSHHOST)
set remote_src $env(REMOTESRC)
set local_dest $env(LOCALDEST)
spawn scp -r -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null ${user}@${host}:${remote_src} $local_dest
expect {
  -re ".*assword:.*" { send "$pass\r"; exp_continue }
  eof
}
catch wait result
set code [lindex $result 3]
exit $code
EOF
}

parse_common_args() {
  REMAINING_ARGS=()
  while [ $# -gt 0 ]; do
    case "$1" in
      --vps-profile)
        VPS_PROFILE="$2"
        shift 2
        ;;
      --account-profile)
        ACCOUNT_PROFILE="$2"
        shift 2
        ;;
      --host)
        HOST="$2"
        shift 2
        ;;
      --user)
        USER_NAME="$2"
        shift 2
        ;;
      *)
        REMAINING_ARGS+=("$@")
        return 0
        ;;
    esac
  done
}

subcommand="${1:-}"
[ -n "$subcommand" ] || {
  usage >&2
  exit 1
}
shift

parse_common_args "$@"
if [ "${#REMAINING_ARGS[@]}" -gt 0 ]; then
  set -- "${REMAINING_ARGS[@]}"
else
  set --
fi

load_vps_profile
[ -n "$HOST" ] || die "--host is required"

LOCAL_REMOTE_START="${ROOT_DIR}/tools/remote_start_run.sh"

bootstrap_remote() {
  local local_poly_cfg
  local tmp_dir
  local staged_cfg
  local_poly_cfg="$(resolve_local_account_cfg)"
  require_file "$local_poly_cfg"
  require_file "$LOCAL_REMOTE_START"

  tmp_dir="$(mktemp -d)"
  staged_cfg="${tmp_dir}/config.json"
  cp "$local_poly_cfg" "$staged_cfg"
  trap 'rm -rf "$tmp_dir"' RETURN

  expect_scp_to_remote "$staged_cfg" "$LOCAL_REMOTE_START" "/tmp/"

  local remote_script
  remote_script=$(cat <<EOF
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git python3 python3-pip python3-venv
mkdir -p /opt/polybot/shared /opt/polybot/log/runs /root/.config/polymarket
cp /tmp/config.json /opt/polybot/shared/polymarket_config.json
cp /tmp/config.json /root/.config/polymarket/config.json
if [ ! -d /opt/polybot/current/.git ]; then
  rm -rf /opt/polybot/current
  git clone ${REPO_URL} /opt/polybot/current
fi
git -C /opt/polybot/current remote set-url origin ${REPO_URL}
git -C /opt/polybot/current fetch origin ${BRANCH}
git -C /opt/polybot/current checkout ${BRANCH}
git -C /opt/polybot/current pull --ff-only origin ${BRANCH}
if [ ! -x /opt/polybot/venv/bin/python ]; then
  python3 -m venv /opt/polybot/venv
fi
. /opt/polybot/venv/bin/activate
pip install -q --upgrade pip
pip install -q -r /opt/polybot/current/requirements.txt
install -m 755 /opt/polybot/current/tools/remote_start_run.sh /usr/local/bin/polybot-remote-start
cat >/usr/local/bin/polybot-update <<'SH'
#!/usr/bin/env bash
set -euo pipefail
cd /opt/polybot/current
git fetch origin ${BRANCH}
git checkout ${BRANCH}
git pull --ff-only origin ${BRANCH}
. /opt/polybot/venv/bin/activate
pip install -q -r requirements.txt
mkdir -p /root/.config/polymarket
cp /opt/polybot/shared/polymarket_config.json /root/.config/polymarket/config.json
install -m 755 /opt/polybot/current/tools/remote_start_run.sh /usr/local/bin/polybot-remote-start
echo "updated \$(git rev-parse --short HEAD)"
SH
chmod +x /usr/local/bin/polybot-update
cat >/usr/local/bin/polybot-run <<'SH'
#!/usr/bin/env bash
set -euo pipefail
cd /opt/polybot/current
exec env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy \\
  PYTHONPATH=/opt/polybot/current \\
  /opt/polybot/venv/bin/python run.py "\$@"
SH
chmod +x /usr/local/bin/polybot-run
cat >/usr/local/bin/polybot-probe <<'SH'
#!/usr/bin/env bash
set -euo pipefail
cd /opt/polybot/current
exec env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy \\
  PYTHONPATH=/opt/polybot/current \\
  /opt/polybot/venv/bin/python tools/probe_post_order_latency.py "\$@"
SH
chmod +x /usr/local/bin/polybot-probe
bash -n /usr/local/bin/polybot-remote-start /usr/local/bin/polybot-update /usr/local/bin/polybot-run /usr/local/bin/polybot-probe
echo "bootstrap_ok host=${HOST} branch=${BRANCH} head=\$(git -C /opt/polybot/current rev-parse --short HEAD)"
EOF
)
  expect_ssh "$remote_script"
}

run_remote() {
  local extra_args_text=""
  local arg quoted_arg
  for arg in "${RUN_EXTRA_ARGS[@]}"; do
    printf -v quoted_arg '%q' "$arg"
    extra_args_text+=" ${quoted_arg}"
  done
  local remote_cmd="polybot-update && /usr/local/bin/polybot-remote-start '${PRESET}' '${ROUNDS}' '${MODE}' '${LABEL}'${extra_args_text}"
  expect_ssh "$remote_cmd"
}

collect_remote() {
  local collect_args_text=""
  local arg quoted_arg
  for arg in "${COLLECT_ARGS[@]}"; do
    printf -v quoted_arg '%q' "$arg"
    collect_args_text+=" ${quoted_arg}"
  done
  local label_arg="${LABEL:-collect}"
  local remote_cmd
  remote_cmd=$(cat <<EOF
polybot-update && bash -lc '
set -euo pipefail
ROOT_DIR=/opt/polybot/current
RUNS_DIR=/opt/polybot/log/collect_runs
STAMP=\$(date -u "+%Y%m%dT%H%M%SZ")
RUN_ID="\${STAMP}_${label_arg}"
RUN_DIR="\${RUNS_DIR}/\${RUN_ID}"
mkdir -p "\${RUN_DIR}"
cd "\${RUN_DIR}"
GIT_HEAD=\$(git -C "\${ROOT_DIR}" rev-parse --short HEAD)
STARTED_AT=\$(date -u "+%Y-%m-%dT%H:%M:%SZ")
cat > "\${RUN_DIR}/meta.env" <<META
RUN_ID=\${RUN_ID}
RUN_DIR=\${RUN_DIR}
WINDOWS=${ROUNDS}
GIT_HEAD=\${GIT_HEAD}
STARTED_AT=\${STARTED_AT}
ROOT_DIR=\${ROOT_DIR}
EXTRA_ARGS=${collect_args_text}
META
cat > "\${RUN_DIR}/run.sh" <<RUNSH
#!/usr/bin/env bash
set -euo pipefail
cd "\${RUN_DIR}"
mkdir -p data
exec env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy \\
  PYTHONPATH="\${ROOT_DIR}" \\
  /opt/polybot/venv/bin/python "\${ROOT_DIR}/tools/collect_data.py" --market btc-updown-5m --windows ${ROUNDS}${collect_args_text}
RUNSH
chmod +x "\${RUN_DIR}/run.sh"
nohup setsid bash -lc "
  set -euo pipefail
  RC=0
  echo \"\$\$\" > '\${RUN_DIR}/pgid'
  if ! '\${RUN_DIR}/run.sh' >'\${RUN_DIR}/stdout.log' 2>&1; then
    RC=\$?
  fi
  printf '%s\n' \"\$RC\" > '\${RUN_DIR}/exit_code'
  date -u '+%Y-%m-%dT%H:%M:%SZ' > '\${RUN_DIR}/finished_at'
" </dev/null >/dev/null 2>&1 &
RUN_PID=\$!
echo "\${RUN_PID}" > "\${RUN_DIR}/pid"
echo "PID=\${RUN_PID}" >> "\${RUN_DIR}/meta.env"
echo "PGID=\${RUN_PID}" >> "\${RUN_DIR}/meta.env"
ln -sfn "\${RUN_DIR}" "\${RUNS_DIR}/latest"
sleep 3
STATUS=running
EXIT_CODE_VALUE=""
if [ -f "\${RUN_DIR}/exit_code" ]; then
  STATUS=failed
  EXIT_CODE_VALUE=\$(cat "\${RUN_DIR}/exit_code" 2>/dev/null || true)
elif ! kill -0 "\${RUN_PID}" 2>/dev/null; then
  STATUS=exited
fi
printf "RUN_ID=%s\\nRUN_DIR=%s\\nPID=%s\\nGIT_HEAD=%s\\nSTATUS=%s\\n" "\${RUN_ID}" "\${RUN_DIR}" "\${RUN_PID}" "\${GIT_HEAD}" "\${STATUS}"
if [ -n "\${EXIT_CODE_VALUE}" ]; then
  printf "EXIT_CODE=%s\\n" "\${EXIT_CODE_VALUE}"
fi
if [ -f "\${RUN_DIR}/stdout.log" ]; then
  echo "STDOUT_TAIL_BEGIN"
  tail -n 20 "\${RUN_DIR}/stdout.log" || true
  echo "STDOUT_TAIL_END"
fi
'
EOF
)
  expect_ssh "$remote_cmd"
}

stop_remote() {
  local remote_cmd="RUN_DIR=\$(if [ '${RUN_ID}' = 'latest' ]; then if [ -L /opt/polybot/log/runs/latest ]; then readlink -f /opt/polybot/log/runs/latest; else readlink -f /opt/polybot/log/collect_runs/latest; fi; elif [ -d /opt/polybot/log/runs/'${RUN_ID}' ]; then echo /opt/polybot/log/runs/'${RUN_ID}'; else echo /opt/polybot/log/collect_runs/'${RUN_ID}'; fi); echo RUN_DIR=\$RUN_DIR; if [ ! -d \"\$RUN_DIR\" ]; then echo STATUS=no_run_dir; exit 0; fi; PID=\$(cat \"\$RUN_DIR/pid\" 2>/dev/null || true); PGID=\$(cat \"\$RUN_DIR/pgid\" 2>/dev/null || printf '%s' \"\$PID\"); [ -n \"\$PGID\" ] && kill -TERM -- -\"\$PGID\" 2>/dev/null || true; [ -n \"\$PID\" ] && kill -TERM \"\$PID\" 2>/dev/null || true; pkill -TERM -f '/opt/polybot/venv/bin/python run.py' 2>/dev/null || true; pkill -TERM -f '/opt/polybot/venv/bin/python.*/tools/collect_data.py' 2>/dev/null || true; sleep 2; [ -n \"\$PGID\" ] && kill -KILL -- -\"\$PGID\" 2>/dev/null || true; pkill -KILL -f '/opt/polybot/venv/bin/python run.py' 2>/dev/null || true; pkill -KILL -f '/opt/polybot/venv/bin/python.*/tools/collect_data.py' 2>/dev/null || true; sleep 1; REMAINING=\$(ps -eo pid,ppid,cmd | grep -E 'python.*run\\.py|python.*collect_data\\.py|polybot-run|remote_start_run|polybot-remote-start' | grep -v grep || true); printf '143\n' > \"\$RUN_DIR/exit_code\"; date -u '+%Y-%m-%dT%H:%M:%SZ' > \"\$RUN_DIR/finished_at\"; date -u '+%Y-%m-%dT%H:%M:%SZ' > \"\$RUN_DIR/stopped_at\"; if [ -n \"\$REMAINING\" ]; then echo STATUS=still_running; printf '%s\n' \"\$REMAINING\"; else echo STATUS=stopped; fi; echo PID=\$PID; echo PGID=\$PGID"
  expect_ssh "$remote_cmd"
}

fetch_remote() {
  local remote_dir
  remote_dir="$(expect_ssh "if [ '${RUN_ID}' = 'latest' ]; then if [ -L /opt/polybot/log/runs/latest ]; then readlink -f /opt/polybot/log/runs/latest; else readlink -f /opt/polybot/log/collect_runs/latest; fi; elif [ -d /opt/polybot/log/runs/'${RUN_ID}' ]; then echo /opt/polybot/log/runs/'${RUN_ID}'; else echo /opt/polybot/log/collect_runs/'${RUN_ID}'; fi")"
  remote_dir="$(printf '%s\n' "$remote_dir" | tail -n 1 | tr -d '\r')"
  [ -n "$remote_dir" ] || die "remote run dir not found"
  local host_safe="${HOST//./_}"
  local dest_dir="${DEST_ROOT}/${host_safe}"
  mkdir -p "$dest_dir"
  expect_scp_from_remote "$remote_dir" "$dest_dir/"
  echo "fetched=${dest_dir}/$(basename "$remote_dir")"
}

status_remote() {
  local remote_cmd="RUN_DIR=\$(if [ '${RUN_ID}' = 'latest' ]; then if [ -L /opt/polybot/log/runs/latest ]; then readlink -f /opt/polybot/log/runs/latest; else readlink -f /opt/polybot/log/collect_runs/latest; fi; elif [ -d /opt/polybot/log/runs/'${RUN_ID}' ]; then echo /opt/polybot/log/runs/'${RUN_ID}'; else echo /opt/polybot/log/collect_runs/'${RUN_ID}'; fi); echo RUN_DIR=\$RUN_DIR; if [ -f \"\$RUN_DIR/meta.env\" ]; then cat \"\$RUN_DIR/meta.env\"; fi; PID=\$(cat \"\$RUN_DIR/pid\" 2>/dev/null || true); LIVE=\"\"; if [ -n \"\$PID\" ]; then LIVE=\$(ps -p \"\$PID\" -o pid=,ppid=,cmd= 2>/dev/null || true); fi; if [ -z \"\$LIVE\" ]; then LIVE=\$(ps -eo pid,ppid,cmd | grep -E 'python.*run\\.py|python.*collect_data\\.py|polybot-run|remote_start_run|polybot-remote-start' | grep -v grep || true); fi; if [ -n \"\$LIVE\" ]; then echo STATUS=running; printf '%s\n' \"\$LIVE\"; elif [ -f \"\$RUN_DIR/exit_code\" ]; then CODE=\$(cat \"\$RUN_DIR/exit_code\"); if [ \"\$CODE\" = '143' ] && [ -f \"\$RUN_DIR/stopped_at\" ]; then echo STATUS=stopped; else echo STATUS=done; fi; echo EXIT_CODE=\$CODE; elif [ -f \"\$RUN_DIR/pid\" ]; then echo STATUS=stopped_no_exit; echo PID=\$(cat \"\$RUN_DIR/pid\"); else echo STATUS=unknown; fi"
  expect_ssh "$remote_cmd"
}

probe_remote() {
  [ -n "$TOKEN_ID" ] || die "--token-id is required"
  local remote_cmd="polybot-update && polybot-probe --token-id '${TOKEN_ID}' $*"
  expect_ssh "$remote_cmd"
}

case "$subcommand" in
  bootstrap)
    while [ $# -gt 0 ]; do
      case "$1" in
        --account-profile)
          ACCOUNT_PROFILE="$2"
          shift 2
          ;;
        --repo)
          REPO_URL="$2"
          shift 2
          ;;
        --branch)
          BRANCH="$2"
          shift 2
          ;;
        -h|--help)
          usage
          exit 0
          ;;
        *)
          die "unknown bootstrap arg: $1"
          ;;
      esac
    done
    bootstrap_remote
    ;;
  run)
    while [ $# -gt 0 ]; do
      case "$1" in
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
        --)
          shift
          RUN_EXTRA_ARGS+=("$@")
          break
          ;;
        -h|--help)
          usage
          exit 0
          ;;
        *)
          die "unknown run arg: $1"
          ;;
      esac
    done
    run_remote
    ;;
  stop)
    while [ $# -gt 0 ]; do
      case "$1" in
        --run-id)
          RUN_ID="$2"
          shift 2
          ;;
        -h|--help)
          usage
          exit 0
          ;;
        *)
          die "unknown stop arg: $1"
          ;;
      esac
    done
    stop_remote
    ;;
  fetch)
    while [ $# -gt 0 ]; do
      case "$1" in
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
          die "unknown fetch arg: $1"
          ;;
      esac
    done
    fetch_remote
    ;;
  status)
    while [ $# -gt 0 ]; do
      case "$1" in
        --run-id)
          RUN_ID="$2"
          shift 2
          ;;
        -h|--help)
          usage
          exit 0
          ;;
        *)
          die "unknown status arg: $1"
          ;;
      esac
    done
    status_remote
    ;;
  collect)
    COLLECT_ARGS=(--slim --no-snap --poly-min-interval-ms 100)
    while [ $# -gt 0 ]; do
      case "$1" in
        --windows)
          ROUNDS="$2"
          shift 2
          ;;
        --label)
          LABEL="$2"
          shift 2
          ;;
        --)
          shift
          COLLECT_ARGS=("$@")
          break
          ;;
        -h|--help)
          usage
          exit 0
          ;;
        *)
          COLLECT_ARGS+=("$1")
          shift
          ;;
      esac
    done
    collect_remote
    ;;
  probe)
    probe_args=()
    while [ $# -gt 0 ]; do
      case "$1" in
        --token-id)
          TOKEN_ID="$2"
          shift 2
          ;;
        -h|--help)
          usage
          exit 0
          ;;
        *)
          probe_args+=("$1")
          shift
          ;;
      esac
    done
    probe_remote "${probe_args[@]}"
    ;;
  *)
    usage >&2
    exit 1
    ;;
esac
