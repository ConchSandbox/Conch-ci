#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: checkout-conch.sh --repository URL --commit FULL_SHA --destination DIR" >&2
}

repository=
commit=
destination=
while (($#)); do
  case "$1" in
    --repository) repository=${2:?}; shift 2 ;;
    --commit) commit=${2:?}; shift 2 ;;
    --destination) destination=${2:?}; shift 2 ;;
    *) usage; exit 2 ;;
  esac
done

[[ "$repository" == "https://github.com/ConchSandbox/Conch.git" ]]
[[ "$commit" =~ ^[0-9a-f]{40}$ ]]
[[ "$destination" == /* && "$destination" != / && ! -L "$destination" ]]

cleanup_destination() {
  if [[ -e "$destination" ]]; then
    find "$destination" -depth -delete
  fi
}

checkout_complete=false
trap '[[ "$checkout_complete" == true ]] || cleanup_destination' EXIT

prepare_destination() {
  if [[ -e "$destination" ]]; then
    find "$destination" -depth -mindepth 1 -delete
  else
    mkdir -p "$destination"
  fi
}

fetch_complete=false
for attempt in 1 2 3; do
  prepare_destination
  git -C "$destination" init -q
  git -C "$destination" remote add origin "$repository"
  printf 'fetching Conch commit (attempt %d/3): %s\n' "$attempt" "$commit"
  if git \
    -c http.version=HTTP/1.1 \
    -c http.lowSpeedLimit=1024 \
    -c http.lowSpeedTime=60 \
    -C "$destination" \
    fetch --no-tags --depth 1 origin "$commit"; then
    fetch_complete=true
    break
  fi
done
[[ "$fetch_complete" == true ]] || {
  echo "failed to fetch Conch commit after 3 attempts: $commit" >&2
  exit 1
}

git -C "$destination" checkout --detach "$commit"
[[ "$(git -C "$destination" rev-parse HEAD)" == "$commit" ]]
git -C "$destination" status --short --branch
checkout_complete=true
trap - EXIT
