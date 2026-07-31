#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage: build-kernel.sh plan|ensure \
  --conch-source DIR --conch-commit FULL_SHA --cache-root DIR \
  [--cache-hit true|false]
EOF
}

mode=${1:-}
shift || true
conch_source=
conch_commit=
cache_root=
cache_hit=false
while (($#)); do
  case "$1" in
    --conch-source) conch_source=${2:?}; shift 2 ;;
    --conch-commit) conch_commit=${2:?}; shift 2 ;;
    --cache-root) cache_root=${2:?}; shift 2 ;;
    --cache-hit) cache_hit=${2:?}; shift 2 ;;
    *) usage; exit 2 ;;
  esac
done
[[ "$mode" == plan || "$mode" == ensure ]] || { usage; exit 2; }
[[ "$conch_commit" =~ ^[0-9a-f]{40}$ ]]
[[ "$(git -C "$conch_source" rev-parse HEAD)" == "$conch_commit" ]]
[[ -n "$cache_root" && "$cache_root" != / ]]
[[ "$cache_hit" == true || "$cache_hit" == false ]]

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
lock_file="$script_dir/lib/lock.py"
ids="$script_dir/lib/ids.py"
kernel_commit=$(python3 "$lock_file" get job_build_inputs.kernel_commit)
kernel_archive_sha256=$(python3 "$lock_file" get job_build_inputs.kernel_archive_sha256)
kernel_archive_url="https://gitee.com/openeuler/kernel/repository/archive/${kernel_commit}.tar.gz"
platform=arm64
config="$conch_source/config/oe-kernel/aarch/.config"
[[ -f "$config" ]]
config_sha256=$(sha256sum "$config" | awk '{print $1}')
build_id=$(python3 "$ids" kernel \
  --source-commit "$kernel_commit" \
  --source-archive-sha256 "$kernel_archive_sha256" \
  --config-sha256 "$config_sha256" \
  --platform "$platform")
artifact_name="kernel-${platform}-${build_id}"
cache_dir="$cache_root/$artifact_name"

write_outputs() {
  local sha=${1:-}
  local hit=${2:-false}
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    {
      printf 'kernel-build-id=%s\n' "$build_id"
      printf 'kernel-platform=%s\n' "$platform"
      printf 'kernel-sha256=%s\n' "$sha"
      printf 'kernel-artifact-name=%s\n' "$artifact_name"
      printf 'cache-dir=%s\n' "$cache_dir"
      printf 'cache-hit=%s\n' "$hit"
      printf 'source-commit=%s\n' "$kernel_commit"
      printf 'source-archive-sha256=%s\n' "$kernel_archive_sha256"
      printf 'config-sha256=%s\n' "$config_sha256"
    } >> "$GITHUB_OUTPUT"
  fi
}

if [[ "$mode" == plan ]]; then
  printf 'kernel build ID: %s\n' "$build_id"
  printf 'kernel source commit: %s\n' "$kernel_commit"
  printf 'kernel source archive SHA-256: %s\n' "$kernel_archive_sha256"
  printf 'kernel config SHA-256: %s\n' "$config_sha256"
  printf 'kernel platform: %s\n' "$platform"
  write_outputs "" false
  exit 0
fi

verify_cache() {
  [[ -f "$cache_dir/Image" && -f "$cache_dir/bzImage" && -f "$cache_dir/kernel-metadata.json" ]]
  cmp -s "$cache_dir/Image" "$cache_dir/bzImage"
  [[ $(stat -c '%s' "$cache_dir/Image") -gt 1048576 ]]
  file "$cache_dir/Image" | grep -Eq 'ARM64|ARM aarch64'
  KERNEL_METADATA="$cache_dir/kernel-metadata.json" \
  KERNEL_BUILD_ID="$build_id" \
  KERNEL_SOURCE_COMMIT="$kernel_commit" \
  KERNEL_SOURCE_ARCHIVE_SHA256="$kernel_archive_sha256" \
  KERNEL_CONFIG_SHA256="$config_sha256" \
  KERNEL_PLATFORM="$platform" \
  python3 - <<'PY'
import hashlib
import json
import os
import re
from pathlib import Path

metadata_path = Path(os.environ["KERNEL_METADATA"])
metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
expected = {
    "schema_version": 2,
    "build_id": os.environ["KERNEL_BUILD_ID"],
    "source_commit": os.environ["KERNEL_SOURCE_COMMIT"],
    "source_archive_sha256": os.environ["KERNEL_SOURCE_ARCHIVE_SHA256"],
    "config_sha256": os.environ["KERNEL_CONFIG_SHA256"],
    "platform": os.environ["KERNEL_PLATFORM"],
    "native_output": "Image",
    "workflow_alias": "bzImage",
    "format": "ARM64 Image",
}
for key, value in expected.items():
    if metadata.get(key) != value:
        raise SystemExit(f"kernel metadata mismatch for {key}: {metadata.get(key)!r} != {value!r}")
if not re.fullmatch(r"[0-9a-f]{40}", metadata.get("conch_commit", "")):
    raise SystemExit("kernel metadata has an invalid Conch provenance commit")
image = metadata_path.parent / "Image"
actual = hashlib.sha256(image.read_bytes()).hexdigest()
if metadata.get("sha256") != actual:
    raise SystemExit(f"kernel content digest mismatch: {metadata.get('sha256')} != {actual}")
PY
}

if [[ "$cache_hit" == true ]]; then
  verify_cache
else
  for command_name in bc bison curl flex gcc git gzip make openssl pahole perl rsync tar; do
    command -v "$command_name" >/dev/null
  done
  build_root="${RUNNER_TEMP:?RUNNER_TEMP must be set}/conch-kernel-build-$build_id"
  [[ "$build_root" == /* && "$build_root" != / ]]
  cleanup_build_root() {
    if [[ -e "$build_root" ]]; then
      find "$build_root" -depth -delete
    fi
  }
  trap cleanup_build_root EXIT
  if [[ -e "$build_root" ]]; then
    find "$build_root" -depth -mindepth 1 -delete
  else
    mkdir -p "$build_root"
  fi
  source_dir="$build_root/source"
  archive="$build_root/kernel-${kernel_commit}.tar.gz"
  partial="$archive.partial"
  archive_ready=false
  for attempt in 1 2 3; do
    find "$partial" -maxdepth 0 -delete 2>/dev/null || true
    printf 'downloading kernel source (attempt %d/3): %s\n' "$attempt" "$kernel_archive_url"
    if curl \
      --fail \
      --location \
      --silent \
      --show-error \
      --connect-timeout 30 \
      --speed-limit 1024 \
      --speed-time 60 \
      --max-time 1800 \
      --output "$partial" \
      "$kernel_archive_url"; then
      mv "$partial" "$archive"
      archive_ready=true
      break
    fi
  done
  [[ "$archive_ready" == true ]] || {
    echo "failed to download kernel source after 3 attempts" >&2
    exit 1
  }
  actual_archive_sha256=$(sha256sum "$archive" | awk '{print $1}')
  [[ "$actual_archive_sha256" == "$kernel_archive_sha256" ]] || {
    echo "kernel source archive digest mismatch: $actual_archive_sha256 != $kernel_archive_sha256" >&2
    exit 1
  }
  gzip -t "$archive"
  archive_commit=$(git get-tar-commit-id < <(gzip -dc "$archive"))
  [[ "$archive_commit" == "$kernel_commit" ]] || {
    echo "kernel source archive commit mismatch: $archive_commit != $kernel_commit" >&2
    exit 1
  }
  mkdir -p "$source_dir"
  tar \
    --extract \
    --gzip \
    --file "$archive" \
    --directory "$source_dir" \
    --strip-components=1 \
    --no-same-owner \
    --no-same-permissions
  [[ -f "$source_dir/Makefile" ]]

  install -m 0644 "$config" "$source_dir/.config"
  make -C "$source_dir" olddefconfig
  make -C "$source_dir" -j"$(nproc)" Image
  native_image="$source_dir/arch/arm64/boot/Image"
  [[ -f "$native_image" ]]
  [[ $(stat -c '%s' "$native_image") -gt 1048576 ]]
  file "$native_image" | grep -Eq 'ARM64|ARM aarch64'

  mkdir -p "$cache_dir"
  install -m 0644 "$native_image" "$cache_dir/Image"
  install -m 0644 "$native_image" "$cache_dir/bzImage"
  kernel_sha256=$(sha256sum "$cache_dir/Image" | awk '{print $1}')
  KERNEL_METADATA="$cache_dir/kernel-metadata.json" \
  KERNEL_BUILD_ID="$build_id" \
  KERNEL_SOURCE_COMMIT="$kernel_commit" \
  KERNEL_SOURCE_ARCHIVE_SHA256="$kernel_archive_sha256" \
  KERNEL_CONFIG_SHA256="$config_sha256" \
  KERNEL_PLATFORM="$platform" \
  CONCH_COMMIT="$conch_commit" \
  KERNEL_SHA256="$kernel_sha256" \
  python3 - <<'PY'
import json
import os
from pathlib import Path

metadata = {
    "schema_version": 2,
    "build_id": os.environ["KERNEL_BUILD_ID"],
    "source_commit": os.environ["KERNEL_SOURCE_COMMIT"],
    "source_archive_sha256": os.environ["KERNEL_SOURCE_ARCHIVE_SHA256"],
    "config_sha256": os.environ["KERNEL_CONFIG_SHA256"],
    "platform": os.environ["KERNEL_PLATFORM"],
    "conch_commit": os.environ["CONCH_COMMIT"],
    "native_output": "Image",
    "workflow_alias": "bzImage",
    "format": "ARM64 Image",
    "sha256": os.environ["KERNEL_SHA256"],
}
Path(os.environ["KERNEL_METADATA"]).write_text(
    json.dumps(metadata, sort_keys=True, indent=2) + "\n",
    encoding="utf-8",
)
PY
  cleanup_build_root
  trap - EXIT
  verify_cache
fi

kernel_sha256=$(sha256sum "$cache_dir/Image" | awk '{print $1}')
printf 'ARM64 Image ready (workflow alias: bzImage)\n'
printf 'kernel sha256: %s\n' "$kernel_sha256"
write_outputs "$kernel_sha256" "$cache_hit"

if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
  {
    echo "### Kernel"
    echo
    echo "- Build ID: \`$build_id\`"
    echo "- Source commit: \`$kernel_commit\`"
    echo "- Source archive SHA-256: \`$kernel_archive_sha256\`"
    echo "- Config SHA-256: \`$config_sha256\`"
    echo "- Platform: \`$platform\`"
    echo "- Cache hit: \`$cache_hit\`"
    echo "- Output: ARM64 \`Image\` (workflow alias: \`bzImage\`)"
    echo "- SHA-256: \`$kernel_sha256\`"
  } >> "$GITHUB_STEP_SUMMARY"
fi
