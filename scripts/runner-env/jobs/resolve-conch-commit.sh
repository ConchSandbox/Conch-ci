#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: resolve-conch-commit.sh --repository URL --ref REF" >&2
}

repository=
ref=
while (($#)); do
  case "$1" in
    --repository) repository=${2:?}; shift 2 ;;
    --ref) ref=${2:?}; shift 2 ;;
    *) usage; exit 2 ;;
  esac
done

case "$repository" in
  https://github.com/ConchSandbox/Conch.git|https://atomgit.com/openeuler/Conch.git) ;;
  *) echo "unsupported Conch repository: $repository" >&2; exit 2 ;;
esac
[[ -n "$ref" ]]
if [[ "$ref" =~ ^[0-9a-f]{40}$ ]]; then
  commit=$ref
  git ls-remote --exit-code "$repository" "$commit" >/dev/null 2>&1 || {
    temp_dir=$(mktemp -d)
    trap 'find "$temp_dir" -depth -delete' EXIT
    git -C "$temp_dir" init --quiet
    git -C "$temp_dir" remote add origin "$repository"
    git -C "$temp_dir" fetch --quiet --depth 1 origin "$commit"
  }
else
  resolved=$(git ls-remote --exit-code "$repository" "$ref" "refs/heads/$ref")
  commit=$(awk 'NR == 1 {print $1}' <<<"$resolved")
fi
[[ "$commit" =~ ^[0-9a-f]{40}$ ]]

printf '%s\n' "$commit"
if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  printf 'conch-commit=%s\n' "$commit" >> "$GITHUB_OUTPUT"
fi
