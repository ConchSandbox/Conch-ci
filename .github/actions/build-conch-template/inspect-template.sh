#!/usr/bin/env bash
set -euo pipefail

reference=${1:?Conch template reference is required}
work_dir=$(mktemp -d)
trap 'find "$work_dir" -depth -delete' EXIT

if [[ "$reference" =~ ^localhost:5000/conch-ci/conch-[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?-template:build-[0-9a-f]{64}$ ]]; then
  local_reference=${reference#localhost:5000/}
  repository=${local_reference%:*}
  tag=${local_reference##*:}
  curl \
    --fail \
    --silent \
    --show-error \
    --noproxy localhost \
    --dump-header "$work_dir/headers" \
    --header 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json' \
    --output "$work_dir/index.json" \
    "http://localhost:5000/v2/$repository/manifests/$tag"
  digest=$(awk '
    tolower($1) == "docker-content-digest:" {
      gsub("\\r", "", $2)
      print $2
      exit
    }
  ' "$work_dir/headers")
  [[ "$digest" == "sha256:$(sha256sum "$work_dir/index.json" | awk '{print $1}')" ]]
elif [[ "$reference" =~ ^ghcr\.io/conchsandbox/conch-[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?-template:build-[0-9a-f]{64}$ ]]; then
  docker buildx imagetools inspect "$reference" > "$work_dir/inspect.txt"
  docker buildx imagetools inspect --raw "$reference" > "$work_dir/index.json"
  digest=$(awk '$1 == "Digest:" {print $2; exit}' "$work_dir/inspect.txt")
else
  echo "unsupported Conch template reference: $reference" >&2
  exit 2
fi
[[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]

TEMPLATE_INDEX="$work_dir/index.json" python3 - <<'PY'
import json
import os
from pathlib import Path

index = json.loads(Path(os.environ["TEMPLATE_INDEX"]).read_text(encoding="utf-8"))
media_type = index.get("mediaType", "")
if not (
    media_type.endswith("image.index.v1+json")
    or media_type.endswith("manifest.list.v2+json")
):
    raise SystemExit(f"published template is not an OCI index: {media_type!r}")
kinds = {
    item.get("annotations", {}).get("io.conch.kind", "")
    for item in index.get("manifests", [])
}
missing = {"rootfs", "sandbox"} - kinds
if missing:
    raise SystemExit(f"published template is missing components: {sorted(missing)}")
PY

printf '%s\n' "$digest"
