#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: buildkit.sh start --bin-dir DIR --work-dir DIR | stop --pid-file FILE" >&2
}

process_is_alive() {
  sudo -n kill -0 "$1" 2>/dev/null
}

stop_process() {
  local process_pid=$1
  if [[ "$process_pid" =~ ^[0-9]+$ ]] && process_is_alive "$process_pid"; then
    sudo -n kill "$process_pid" 2>/dev/null || true
    for _ in $(seq 1 30); do
      process_is_alive "$process_pid" || break
      sleep 1
    done
    if process_is_alive "$process_pid"; then
      sudo -n kill -9 "$process_pid" 2>/dev/null || true
    fi
  fi
  wait "$process_pid" 2>/dev/null || true
}

cleanup_socket_dir() {
  local socket_dir=$1
  [[ "$socket_dir" =~ ^/tmp/conch-ci-buildkit\.[A-Za-z0-9]{8}$ && ! -L "$socket_dir" ]] || {
    echo "invalid BuildKit socket directory: $socket_dir" >&2
    return 2
  }
  [[ -d "$socket_dir" ]] || return 0
  rm -f -- "$socket_dir/buildkitd.pid" "$socket_dir/buildkitd.sock"
  rmdir -- "$socket_dir"
}

mode=${1:-}
shift || true
case "$mode" in
  start)
    bin_dir=
    work_dir=
    while (($#)); do
      case "$1" in
        --bin-dir) bin_dir=${2:?}; shift 2 ;;
        --work-dir) work_dir=${2:?}; shift 2 ;;
        *) usage; exit 2 ;;
      esac
    done
    [[ -x "$bin_dir/buildctl" && -x "$bin_dir/buildkitd" && -x "$bin_dir/buildkit-runc" ]]
    [[ -n "$work_dir" && "$work_dir" != / ]]
    mkdir -p "$work_dir"
    log_file="$work_dir/buildkitd.log"
    config_file="$work_dir/buildkitd.toml"
    printf '%s\n' \
      '[registry."localhost:5001"]' \
      '  http = true' \
      > "$config_file"
    # Keep the Unix socket short regardless of the runner's workspace path.
    # The returned pid-file also identifies this private directory for stop.
    socket_dir=$(mktemp -d /tmp/conch-ci-buildkit.XXXXXXXX)
    socket="$socket_dir/buildkitd.sock"
    address="unix://$socket"
    pid_file="$socket_dir/buildkitd.pid"
    pid=
    # Invoked indirectly by the EXIT trap below.
    # shellcheck disable=SC2317,SC2329
    cleanup_failed_start() {
      local status=$?
      trap - EXIT
      set +e
      [[ -z "$pid" ]] || stop_process "$pid"
      cleanup_socket_dir "$socket_dir"
      exit "$status"
    }
    trap cleanup_failed_start EXIT
    # The runner shell intentionally owns this job-local log file.
    # shellcheck disable=SC2024
    sudo -n env "PATH=$bin_dir:$PATH" \
      "$bin_dir/buildkitd" \
      --addr "$address" \
      --config "$config_file" \
      --root "$work_dir/root" \
      --oci-worker-binary "$bin_dir/buildkit-runc" \
      >"$log_file" 2>&1 &
    pid=$!
    printf '%s\n' "$pid" > "$pid_file"
    for _ in $(seq 1 120); do
      if sudo -n "$bin_dir/buildctl" --addr "$address" debug workers >/dev/null 2>&1; then
        if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
          {
            printf 'address=%s\n' "$address"
            printf 'pid-file=%s\n' "$pid_file"
            printf 'log-file=%s\n' "$log_file"
          } >> "$GITHUB_OUTPUT"
        fi
        printf 'BuildKit ready at %s\n' "$address"
        trap - EXIT
        exit 0
      fi
      if ! process_is_alive "$pid"; then
        sed -n '1,300p' "$log_file" >&2 || true
        exit 1
      fi
      sleep 1
    done
    sed -n '1,300p' "$log_file" >&2 || true
    exit 1
    ;;
  stop)
    [[ $# == 2 && ${1:-} == --pid-file && -n ${2:-} ]] || { usage; exit 2; }
    pid_file=$2
    [[ "$pid_file" =~ ^/tmp/conch-ci-buildkit\.[A-Za-z0-9]{8}/buildkitd\.pid$ ]] || {
      echo "invalid BuildKit pid file: $pid_file" >&2
      exit 2
    }
    [[ ! -L "${pid_file%/*}" && ! -L "$pid_file" ]] || {
      echo "BuildKit pid path must not be a symbolic link: $pid_file" >&2
      exit 2
    }
    if [[ -f "$pid_file" ]]; then
      pid=$(<"$pid_file")
      [[ "$pid" =~ ^[1-9][0-9]*$ ]] || {
        echo "invalid BuildKit process ID in $pid_file" >&2
        exit 2
      }
      stop_process "$pid"
    fi
    cleanup_socket_dir "${pid_file%/*}"
    ;;
  *) usage; exit 2 ;;
esac
