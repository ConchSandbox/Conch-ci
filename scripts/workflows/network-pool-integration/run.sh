#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: run.sh --conchd PATH --cni-bin-dir DIR --control-plugin PATH --work-dir DIR" >&2
}

conchd=
cni_bin_dir=
control_plugin=
work_dir=
while (($#)); do
  case "$1" in
    --conchd) conchd=${2:?}; shift 2 ;;
    --cni-bin-dir) cni_bin_dir=${2:?}; shift 2 ;;
    --control-plugin) control_plugin=${2:?}; shift 2 ;;
    --work-dir) work_dir=${2:?}; shift 2 ;;
    *) usage; exit 2 ;;
  esac
done

# These paths cross the sudo and namespace boundary, so keep them absolute and
# reject known unsafe work roots before any cleanup can run.
for path in "$conchd" "$cni_bin_dir" "$control_plugin" "$work_dir"; do
  [[ "$path" == /* ]] || {
    echo "network integration path must be absolute: $path" >&2
    exit 2
  }
done
[[ -x "$conchd" ]]
[[ -x "$control_plugin" ]]
for plugin in bridge host-local loopback; do
  [[ -x "$cni_bin_dir/$plugin" ]]
done
[[ "$work_dir" != / && "$work_dir" != /run && "$work_dir" != /var ]]

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
eventctl="$script_dir/eventctl.py"
[[ -x "$eventctl" ]]

results_dir="$work_dir/results"
scenarios_dir="$work_dir/scenarios"
global_events="$results_dir/cni-events.jsonl"
mkdir -p "$results_dir" "$scenarios_dir"

# The EXIT trap uses this state to stop only the daemon and mounts created by
# the currently active scenario runner.
run_conch_mounted=false
cni_state_mounted=false
active_pid=
active_name=

process_is_running() {
  [[ -n "$1" ]] && kill -0 "$1" 2>/dev/null
}

terminate_active_process() {
  set +e
  if process_is_running "$active_pid"; then
    kill -TERM "$active_pid" 2>/dev/null
    for ((attempt = 0; attempt < 100; attempt++)); do
      process_is_running "$active_pid" || break
      sleep 0.1
    done
    process_is_running "$active_pid" && kill -KILL "$active_pid" 2>/dev/null
  fi
  [[ -z "$active_pid" ]] || wait "$active_pid" 2>/dev/null
  active_pid=
  active_name=
}

dump_diagnostics() {
  for log in "$results_dir"/*/conchd.log; do
    [[ -f "$log" ]] || continue
    echo "===== $log =====" >&2
    tail -n 300 "$log" >&2
  done
}

cleanup() {
  status=$?
  trap - EXIT
  set +e
  if ((status != 0)); then
    dump_diagnostics
  fi
  terminate_active_process
  chmod -R a+rX "$results_dir" 2>/dev/null
  [[ "$cni_state_mounted" != true ]] || umount -R -l /var/lib/cni
  [[ "$run_conch_mounted" != true ]] || umount -R -l /run/conch
  exit "$status"
}
trap cleanup EXIT

# The workflow starts this script in private mount, network, and PID namespaces.
# Make propagation private before hiding host CNI state behind job-local tmpfs.
mount --make-rprivate /
mkdir -p /run/conch /var/lib/cni
mount -t tmpfs -o mode=0700,nosuid,nodev tmpfs /run/conch
run_conch_mounted=true
mount -t tmpfs -o mode=0700,nosuid,nodev tmpfs /var/lib/cni
cni_state_mounted=true
ip link set lo up

prepare_scenario() {
  local name=$1
  local warm_pool_size=$2
  local bridge=$3
  local subnet=$4
  local plan=$5
  local runtime_dir="$scenarios_dir/$name"
  local result_dir="$results_dir/$name"
  local control_dir="$result_dir/control"
  local conf_dir="$runtime_dir/cni/net.d"

  # Runtime state is disposable, while result_dir is retained for diagnostics.
  # plan.json tells the control CNI which ADD/DEL calls to pass, fail, or block.
  mkdir -p \
    "$runtime_dir/home" \
    "$runtime_dir/tmp" \
    "$runtime_dir/work" \
    "$conf_dir" \
    "$control_dir"
  printf '%s\n' "$plan" > "$control_dir/plan.json"

  cat > "$conf_dir/10-conch-ci.conf" <<JSON
{
  "cniVersion": "1.0.0",
  "name": "conch-ci-$name",
  "type": "conch-ci-control",
  "bridge": "$bridge",
  "isGateway": true,
  "ipMasq": true,
  "ipam": {
    "type": "host-local",
    "subnet": "$subnet",
    "routes": [{"dst": "0.0.0.0/0"}]
  },
  "conchCIControlDir": "$control_dir"
}
JSON

  cat > "$result_dir/config.yaml" <<YAML
app:
  name: conch-ci-$name
log:
  level: info
  output: stdout
server:
  unix_socket: "/run/conch/conch-ci-$name.sock"
  pid_file: "$runtime_dir/conchd-service.pid"
  work_dir: "$runtime_dir/work"
state:
  path: "$runtime_dir/state.db"
network:
  warm_pool_size: $warm_pool_size
  tap_ip: 192.168.100.2
  tap_mask: 24
  cni:
    plugin_bin_dirs:
      - "$(dirname "$control_plugin")"
      - "$cni_bin_dir"
    plugin_conf_dir: "$conf_dir"
    if_name: eth0
containerd:
  root_dir: "$runtime_dir/containerd-root"
  state_dir: "$runtime_dir/containerd-state"
sandbox:
  vsock_signal_retry: 10ms
  vsock_signal_timeout: 5s
  request_timeout: 10s
  default_vmm_name: cloud-hypervisor
YAML
  chmod 0600 "$result_dir/config.yaml"
}

# Poll readiness and observable pool state without assuming fixed daemon timing.
wait_for_socket() {
  local name=$1
  local socket="/run/conch/conch-ci-$name.sock"
  for ((attempt = 0; attempt < 900; attempt++)); do
    [[ ! -S "$socket" ]] || return 0
    if ! process_is_running "$active_pid"; then
      echo "conchd exited before scenario $name became ready" >&2
      return 1
    fi
    sleep 0.1
  done
  echo "timed out waiting for conchd socket in scenario $name" >&2
  return 1
}

wait_for_log() {
  local log=$1
  local pattern=$2
  local timeout_tenths=$3
  for ((attempt = 0; attempt < timeout_tenths; attempt++)); do
    grep -Fq -- "$pattern" "$log" 2>/dev/null && return 0
    process_is_running "$active_pid" || return 1
    sleep 0.1
  done
  echo "timed out waiting for log pattern: $pattern" >&2
  return 1
}

wait_for_netns_count() {
  local expected=$1
  local timeout_tenths=$2
  local actual
  for ((attempt = 0; attempt < timeout_tenths; attempt++)); do
    actual=$(find /run/conch/netns -mindepth 1 -maxdepth 1 2>/dev/null | wc -l)
    [[ "$actual" -ne "$expected" ]] || return 0
    process_is_running "$active_pid" || return 1
    sleep 0.1
  done
  echo "timed out waiting for $expected Conch network namespace(s)" >&2
  return 1
}

start_scenario() {
  local name=$1
  local runtime_dir="$scenarios_dir/$name"
  local result_dir="$results_dir/$name"
  local log="$result_dir/conchd.log"

  [[ -z "$active_pid" ]]
  active_name=$name
  # Run the real daemon. Successful CNI calls are delegated to the locked bridge
  # plugin; the control plugin records every call and injects planned failures.
  env \
    HOME="$runtime_dir/home" \
    TMPDIR="$runtime_dir/tmp" \
    CONCH_API_TIMEOUT=10s \
    CONCH_CNI_REAL_BRIDGE="$cni_bin_dir/bridge" \
    CONCH_CNI_EVENT_LOG="$global_events" \
    "$conchd" -config "$result_dir/config.yaml" > "$log" 2>&1 &
  active_pid=$!
  echo "$active_pid" > "$result_dir/conchd.pid"
  wait_for_socket "$name"
}

stop_scenario() {
  local name=$1
  local timeout_tenths=$2
  local status

  [[ "$active_name" == "$name" ]] || {
    echo "active scenario is $active_name, cannot stop $name" >&2
    return 1
  }
  # SIGTERM handling is part of each scenario: conchd must exit cleanly and
  # remove every network namespace it created before the deadline.
  kill -TERM "$active_pid"
  for ((attempt = 0; attempt < timeout_tenths; attempt++)); do
    process_is_running "$active_pid" || break
    sleep 0.1
  done
  if process_is_running "$active_pid"; then
    echo "conchd did not stop within the scenario $name deadline" >&2
    return 1
  fi
  set +e
  wait "$active_pid"
  status=$?
  set -e
  active_pid=
  active_name=
  if ((status != 0)); then
    echo "conchd exited with status $status in scenario $name" >&2
    return 1
  fi
  wait_for_netns_count 0 50
}

event_log() {
  printf '%s/results/%s/control/events.jsonl\n' "$work_dir" "$1"
}

assert_event_count() {
  local name=$1
  local command=$2
  local phase=$3
  local count=$4
  "$eventctl" count "$(event_log "$name")" \
    --command "$command" --phase "$phase" --count "$count"
}

# A healthy startup must synchronously prefill the configured warm pool.
echo "[network-integration] initial prefill"
prepare_scenario initial 2 ci-init0 10.21.0.0/24 \
  '{"outcomes":{"ADD":["pass","pass"]}}'
start_scenario initial
"$eventctl" wait "$(event_log initial)" \
  --command ADD --phase finish --outcome pass --count 2
wait_for_netns_count 2 300
wait_for_log "$results_dir/initial/conchd.log" "initial prefill completed" 300
stop_scenario initial 150
"$eventctl" sequence "$(event_log initial)" \
  --command ADD --phase start --outcomes pass,pass
assert_event_count initial DEL finish 2

# One initial ADD failure must not stop the background refill loop.
echo "[network-integration] continuous refill after partial prefill failure"
prepare_scenario continuous 2 ci-cont0 10.22.0.0/24 \
  '{"outcomes":{"ADD":["pass","fail","pass"]}}'
start_scenario continuous
"$eventctl" wait "$(event_log continuous)" \
  --command ADD --phase finish --count 3
wait_for_log "$results_dir/continuous/conchd.log" \
  "initial prefill exited with error" 300
wait_for_netns_count 2 300
stop_scenario continuous 150
"$eventctl" sequence "$(event_log continuous)" \
  --command ADD --phase start --outcomes pass,fail,pass
assert_event_count continuous DEL finish 3

# Failures grow the retry delay; a successful ADD must reset that backoff.
echo "[network-integration] retry growth and reset"
prepare_scenario retry 2 ci-retry0 10.23.0.0/24 \
  '{"outcomes":{"ADD":["fail","fail","fail","fail","pass","fail","fail","pass"]}}'
start_scenario retry
"$eventctl" wait "$(event_log retry)" \
  --command ADD --phase finish --count 8 --timeout 60
wait_for_netns_count 2 300
stop_scenario retry 150
"$eventctl" retry "$(event_log retry)"
assert_event_count retry DEL finish 8

# Shutdown must interrupt a long retry sleep instead of waiting for it to fire.
echo "[network-integration] cancellation during retry backoff"
prepare_scenario cancel 1 ci-cancel0 10.24.0.0/24 \
  '{"defaults":{"ADD":"fail"}}'
start_scenario cancel
"$eventctl" wait "$(event_log cancel)" \
  --command ADD --phase finish --count 6 --timeout 60
wait_for_log "$results_dir/cancel/conchd.log" "retry_delay=16s" 300
stop_scenario cancel 80
assert_event_count cancel ADD start 6
assert_event_count cancel ADD finish 6
assert_event_count cancel DEL finish 6

# Hold ADD in flight and verify concurrent Close still releases its network.
echo "[network-integration] close during in-flight CNI ADD"
prepare_scenario close 1 ci-close0 10.25.0.0/24 \
  '{"outcomes":{"ADD":["block"]}}'
start_scenario close
"$eventctl" wait "$(event_log close)" \
  --command ADD --phase start --count 1
stop_scenario close 100
assert_event_count close ADD start 1
assert_event_count close ADD finish 0
assert_event_count close DEL finish 1

echo "[network-integration] all scenarios passed"
