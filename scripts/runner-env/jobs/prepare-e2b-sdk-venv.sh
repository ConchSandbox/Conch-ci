#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: prepare-e2b-sdk-venv.sh --conch-source DIR --venv DIR [--sdk-only]" >&2
}

conch_source=
venv=
sdk_only=false
while (($#)); do
  case "$1" in
    --conch-source) conch_source=${2:?}; shift 2 ;;
    --venv) venv=${2:?}; shift 2 ;;
    --sdk-only) sdk_only=true; shift ;;
    *) usage; exit 2 ;;
  esac
done

[[ -f "$conch_source/sdk/pyproject.toml" ]]
[[ -n "$venv" && "$venv" != / ]]
python3 - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit(f"python3 >= 3.10 is required, got {sys.version}")
PY

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
lock_file="$script_dir/lib/lock.py"
e2b_version=$(python3 "$lock_file" get job_build_inputs.e2b_sdk_packages.e2b)
interpreter_version=$(python3 "$lock_file" get job_build_inputs.e2b_sdk_packages.e2b-code-interpreter)

if [[ -e "$venv" ]]; then
  find "$venv" -depth -mindepth 1 -delete
else
  mkdir -p "$venv"
fi
python3 -m venv "$venv"
export PIP_CACHE_DIR="$venv/pip-cache"
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_NO_CACHE_DIR=1
pip_options=(
  --disable-pip-version-check
  --no-cache-dir
  --index-url https://pypi.tuna.tsinghua.edu.cn/simple
  --timeout 120
  --retries 5
)
"$venv/bin/python" -m pip install "${pip_options[@]}" "$conch_source/sdk"
if [[ "$sdk_only" == false ]]; then
  "$venv/bin/python" -m pip install "${pip_options[@]}" \
    "e2b==$e2b_version" \
    "e2b-code-interpreter==$interpreter_version"
fi

CONCH_SOURCE="$conch_source" SDK_ONLY="$sdk_only" \
E2B_VERSION="$e2b_version" INTERPRETER_VERSION="$interpreter_version" \
"$venv/bin/python" - <<'PY'
import importlib.metadata
import os
import sys

sys.path.insert(0, os.environ["CONCH_SOURCE"])
from conch.client import AgentClient

client = AgentClient("127.0.0.1")
assert callable(client.health_check)
if os.environ["SDK_ONLY"] == "false":
    assert importlib.metadata.version("e2b") == os.environ["E2B_VERSION"]
    assert importlib.metadata.version("e2b-code-interpreter") == os.environ["INTERPRETER_VERSION"]
print("Conch SDK environment smoke test passed")
PY

if [[ -n "${GITHUB_PATH:-}" ]]; then
  printf '%s\n' "$venv/bin" >> "$GITHUB_PATH"
fi
if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  {
    printf 'venv=%s\n' "$venv"
    printf 'python=%s\n' "$venv/bin/python"
  } >> "$GITHUB_OUTPUT"
fi
