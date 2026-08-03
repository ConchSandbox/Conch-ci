#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage: ensure-rootfs.sh \
  --conch-source DIR --conch-commit FULL_SHA --dockerfile REPOSITORY_RELATIVE_PATH \
  --repository GHCR_REPOSITORY \
  --bin-dir DIR --work-dir DIR
EOF
}

conch_source=
conch_commit=
dockerfile_relative=
repository=
bin_dir=
work_dir=
while (($#)); do
  case "$1" in
    --conch-source) conch_source=${2:?}; shift 2 ;;
    --conch-commit) conch_commit=${2:?}; shift 2 ;;
    --dockerfile) dockerfile_relative=${2:?}; shift 2 ;;
    --repository) repository=${2:?}; shift 2 ;;
    --bin-dir) bin_dir=${2:?}; shift 2 ;;
    --work-dir) work_dir=${2:?}; shift 2 ;;
    *) usage; exit 2 ;;
  esac
done
[[ "$conch_commit" =~ ^[0-9a-f]{40}$ ]]
[[ "$(git -C "$conch_source" rev-parse HEAD)" == "$conch_commit" ]]
[[ "$dockerfile_relative" =~ ^[A-Za-z0-9._/-]+$ ]]
case "/$dockerfile_relative/" in
  *"/../"*|*"/./"*|*"//"*) echo "invalid Dockerfile path: $dockerfile_relative" >&2; exit 2 ;;
esac
git -C "$conch_source" ls-files --error-unmatch -- "$dockerfile_relative" >/dev/null
[[ "$repository" =~ ^ghcr\.io/[a-z0-9_.-]+/[a-z0-9_.-]+$ ]]
[[ -x "$bin_dir/buildctl" && -x "$bin_dir/buildkitd" && -x "$bin_dir/buildkit-runc" ]]
[[ -n "$work_dir" && "$work_dir" != / ]]
dockerfile="$conch_source/$dockerfile_relative"
[[ -f "$dockerfile" && ! -L "$dockerfile" ]]
dockerfile_dir=$(dirname -- "$dockerfile")
dockerfile_name=$(basename -- "$dockerfile")
command -v docker >/dev/null
docker buildx version

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
platform=linux/arm64
script_sha256=$(sha256sum "${BASH_SOURCE[0]}" | awk '{print $1}')
build_id=$(python3 "$script_dir/lib/ids.py" rootfs \
  --platform "$platform" \
  --conch-commit "$conch_commit" \
  --script-sha256 "$script_sha256" \
  --dockerfile "$dockerfile_relative")
tag="$repository:build-$build_id"
manifest_json="$work_dir/manifest.json"
inspect_text="$work_dir/inspect.txt"
mkdir -p "$work_dir"

inspect_image() {
  docker buildx imagetools inspect "$tag" >"$inspect_text"
  docker buildx imagetools inspect --raw "$tag" >"$manifest_json"
  local index_digest
  index_digest=$(awk '$1 == "Digest:" {print $2; exit}' "$inspect_text")
  [[ "$index_digest" =~ ^sha256:[0-9a-f]{64}$ ]]
  local platform_digest
  platform_digest=$(ROOTFS_MANIFEST="$manifest_json" \
  ROOTFS_BUILD_ID="$build_id" \
  ROOTFS_CONCH_COMMIT="$conch_commit" \
  ROOTFS_PLATFORM="$platform" \
  ROOTFS_SCRIPT_SHA256="$script_sha256" \
  ROOTFS_DOCKERFILE="$dockerfile_relative" \
  ROOTFS_INDEX_DIGEST="$index_digest" \
  python3 - <<'PY'
import json
import os
from pathlib import Path

manifest = json.loads(Path(os.environ["ROOTFS_MANIFEST"]).read_text(encoding="utf-8"))
expected = {
    "io.conch.rootfs.build-id": os.environ["ROOTFS_BUILD_ID"],
    "io.conch.rootfs.conch-commit": os.environ["ROOTFS_CONCH_COMMIT"],
    "io.conch.rootfs.platform": os.environ["ROOTFS_PLATFORM"],
    "io.conch.rootfs.script-sha256": os.environ["ROOTFS_SCRIPT_SHA256"],
    "io.conch.rootfs.dockerfile": os.environ["ROOTFS_DOCKERFILE"],
    "io.conch.rootfs.source-repository": "https://github.com/ConchSandbox/Conch.git",
}
annotations = manifest.get("annotations", {})
for key, value in expected.items():
    if annotations.get(key) != value:
        raise SystemExit(
            f"rootfs metadata mismatch for {key}: {annotations.get(key)!r} != {value!r}"
        )

media_type = manifest.get("mediaType", "")
index_digest = os.environ["ROOTFS_INDEX_DIGEST"]
if media_type.endswith("image.index.v1+json") or media_type.endswith("manifest.list.v2+json"):
    matches = [
        item
        for item in manifest.get("manifests", [])
        if item.get("platform", {}).get("os") == "linux"
        and item.get("platform", {}).get("architecture") == "arm64"
    ]
    if len(matches) != 1:
        raise SystemExit(f"expected one linux/arm64 platform manifest, got {len(matches)}")
    platform_digest = matches[0].get("digest", "")
else:
    platform_digest = index_digest
if not platform_digest.startswith("sha256:") or len(platform_digest) != 71:
    raise SystemExit(f"invalid platform digest: {platform_digest!r}")
print(platform_digest)
PY
  )
  printf '%s %s\n' "$index_digest" "$platform_digest"
}

cache_hit=false
if docker buildx imagetools inspect "$tag" >/dev/null 2>&1; then
  read -r index_digest platform_digest < <(inspect_image)
  cache_hit=true
else
  runtime_output="$work_dir/buildkit-output"
  : > "$runtime_output"
  cleanup_buildkit() {
    if [[ -n "${buildkit_pid_file:-}" ]]; then
      "$script_dir/runtime/buildkit.sh" stop --pid-file "$buildkit_pid_file" || true
    fi
  }
  trap cleanup_buildkit EXIT
  GITHUB_OUTPUT="$runtime_output" \
    "$script_dir/runtime/buildkit.sh" start --bin-dir "$bin_dir" --work-dir "$work_dir/buildkit"
  buildkit_address=$(awk -F= '$1 == "address" {print substr($0, index($0, "=") + 1)}' "$runtime_output")
  buildkit_pid_file=$(awk -F= '$1 == "pid-file" {print substr($0, index($0, "=") + 1)}' "$runtime_output")
  [[ -n "$buildkit_address" && -n "$buildkit_pid_file" ]]

  metadata=(
    "annotation-manifest.io.conch.rootfs.build-id=$build_id"
    "annotation-manifest.io.conch.rootfs.conch-commit=$conch_commit"
    "annotation-manifest.io.conch.rootfs.platform=$platform"
    "annotation-manifest.io.conch.rootfs.script-sha256=$script_sha256"
    "annotation-manifest.io.conch.rootfs.dockerfile=$dockerfile_relative"
    "annotation-manifest.io.conch.rootfs.source-repository=https://github.com/ConchSandbox/Conch.git"
  )
  output="type=image,name=$tag,push=true,oci-mediatypes=true"
  for item in "${metadata[@]}"; do
    output+=",$item"
  done
  sudo -n env "DOCKER_CONFIG=${DOCKER_CONFIG:-}" "$bin_dir/buildctl" --addr "$buildkit_address" build \
    --frontend dockerfile.v0 \
    --local "context=$dockerfile_dir" \
    --local "dockerfile=$dockerfile_dir" \
    --opt "filename=$dockerfile_name" \
    --opt "platform=$platform" \
    --opt "build-arg:GOPROXY=https://goproxy.cn" \
    --output "$output"
  read -r index_digest platform_digest < <(inspect_image)
  cleanup_buildkit
  buildkit_pid_file=
  trap - EXIT
fi

image_ref="$repository@$index_digest"

if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  {
    printf 'rootfs-image-ref=%s\n' "$image_ref"
    printf 'rootfs-index-digest=%s\n' "$index_digest"
    printf 'rootfs-platform-digest=%s\n' "$platform_digest"
    printf 'rootfs-build-id=%s\n' "$build_id"
    printf 'cache-hit=%s\n' "$cache_hit"
    printf 'script-sha256=%s\n' "$script_sha256"
    printf 'dockerfile=%s\n' "$dockerfile_relative"
  } >> "$GITHUB_OUTPUT"
fi

printf 'rootfs build ID: %s\n' "$build_id"
printf 'rootfs cache hit: %s\n' "$cache_hit"
[[ -z "$image_ref" ]] || printf 'rootfs image: %s\n' "$image_ref"
if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
  {
    echo "### Rootfs image"
    echo
    echo "- Build ID: \`$build_id\`"
    echo "- Conch commit: \`$conch_commit\`"
    echo "- Platform: \`$platform\`"
    echo "- Script SHA-256: \`$script_sha256\`"
    echo "- Dockerfile: \`$dockerfile_relative\`"
    echo "- Cache hit: \`$cache_hit\`"
    [[ -z "$image_ref" ]] || echo "- Image: \`$image_ref\`"
  } >> "$GITHUB_STEP_SUMMARY"
fi
