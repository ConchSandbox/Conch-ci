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
    socket="$work_dir/buildkitd.sock"
    address="unix://$socket"
    pid_file="$work_dir/buildkitd.pid"
    log_file="$work_dir/buildkitd.log"
    config_file="$work_dir/buildkitd.toml"
    printf '%s\n' \
      '[registry."localhost:5000"]' \
      '  http = true' \
      > "$config_file"
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
    # Invoked indirectly by the EXIT trap below.
    # shellcheck disable=SC2317,SC2329
    cleanup_failed_start() {
      local status=$?
      trap - EXIT
      set +e
      stop_process "$pid"
      find "$pid_file" "$socket" -maxdepth 0 -delete 2>/dev/null
      exit "$status"
    }
    trap cleanup_failed_start EXIT
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
    [[ ${1:-} == --pid-file && -n ${2:-} ]] || { usage; exit 2; }
    pid_file=$2
    if [[ -f "$pid_file" ]]; then
      pid=$(<"$pid_file")
      stop_process "$pid"
      find "$pid_file" -maxdepth 0 -delete
    fi
    ;;
  *) usage; exit 2 ;;
esac
