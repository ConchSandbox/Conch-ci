#!/usr/bin/env bash
set -euo pipefail

repository=
for ((index = 1; index <= $#; index++)); do
  if [[ ${!index} == --repository ]]; then
    value_index=$((index + 1))
    repository=${!value_index:-}
    break
  fi
done

if [[ "$repository" != https://github.com/SToPire/Conch.git ]]; then
  exec scripts/runner-env/jobs/resolve-conch-commit.sh "$@"
fi

ref=
pr_number=
while (($#)); do
  case "$1" in
    --repository) shift 2 ;;
    --ref) ref=${2:?}; shift 2 ;;
    --pr-number) pr_number=${2:?}; shift 2 ;;
    *) echo "unsupported resolve argument: $1" >&2; exit 2 ;;
  esac
done
if [[ -n "$ref" && -n "$pr_number" ]] || [[ -z "$ref" && -z "$pr_number" ]]; then
  echo "exactly one of --ref or --pr-number is required" >&2
  exit 2
fi
if [[ -n "$pr_number" ]]; then
  [[ "$pr_number" =~ ^[1-9][0-9]*$ ]]
  remote_ref="refs/pull/$pr_number/head"
elif [[ "$ref" =~ ^[0-9a-f]{40}$ ]]; then
  remote_ref=$ref
else
  remote_ref="refs/heads/$ref"
fi
resolved=$(git ls-remote --exit-code "$repository" "$remote_ref")
commit=$(awk 'NR == 1 {print $1}' <<<"$resolved")
[[ "$commit" =~ ^[0-9a-f]{40}$ ]]
printf '%s\n' "$commit"
if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  printf 'conch-commit=%s\n' "$commit" >> "$GITHUB_OUTPUT"
fi
