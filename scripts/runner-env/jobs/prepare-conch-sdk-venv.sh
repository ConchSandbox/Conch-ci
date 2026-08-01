#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: prepare-conch-sdk-venv.sh --conch-source DIR --venv DIR [--requirements FILE]" >&2
}

conch_source=
venv=
requirements=
while (($#)); do
  case "$1" in
    --conch-source) conch_source=${2:?}; shift 2 ;;
    --venv) venv=${2:?}; shift 2 ;;
    --requirements) requirements=${2:?}; shift 2 ;;
    *) usage; exit 2 ;;
  esac
done

[[ -f "$conch_source/sdk/pyproject.toml" ]]
[[ -n "$venv" && "$venv" != / ]]
[[ -z "$requirements" || -f "$requirements" ]]
python3 - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit(f"python3 >= 3.10 is required, got {sys.version}")
PY

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
if [[ -n "$requirements" ]]; then
  "$venv/bin/python" -m pip install "${pip_options[@]}" --requirement "$requirements"
fi

CONCH_SOURCE="$conch_source" \
"$venv/bin/python" - <<'PY'
import os
import sys

sys.path.insert(0, os.environ["CONCH_SOURCE"])
from conch.client import AgentClient

client = AgentClient("127.0.0.1")
assert callable(client.health_check)
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
