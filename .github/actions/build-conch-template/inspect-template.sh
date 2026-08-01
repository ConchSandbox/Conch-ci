#!/usr/bin/env bash
set -euo pipefail

reference=${1:?Conch template reference is required}
work_dir=$(mktemp -d)
trap 'find "$work_dir" -depth -delete' EXIT

docker buildx imagetools inspect "$reference" > "$work_dir/inspect.txt"
docker buildx imagetools inspect --raw "$reference" > "$work_dir/index.json"
digest=$(awk '$1 == "Digest:" {print $2; exit}' "$work_dir/inspect.txt")
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
