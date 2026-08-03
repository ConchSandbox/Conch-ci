#!/usr/bin/env bash
set -euo pipefail

if (($# != 1)); then
  echo "usage: image_repositories.sh IMAGE_PROFILE" >&2
  exit 2
fi
image_profile=${1:?image profile is required}
[[ "$image_profile" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]]

local_registry=localhost:5000
local_prefix=$local_registry/conch-ci
ghcr_registry=ghcr.io
ghcr_prefix=$ghcr_registry/conchsandbox

printf 'image-profile=%s\n' "$image_profile"
printf 'local-registry=%s\n' "$local_registry"
printf 'local-rootfs-repository=%s/conch-%s-rootfs\n' "$local_prefix" "$image_profile"
printf 'local-template-repository=%s/conch-%s-template\n' "$local_prefix" "$image_profile"
printf 'ghcr-registry=%s\n' "$ghcr_registry"
printf 'ghcr-rootfs-repository=%s/conch-%s-rootfs\n' "$ghcr_prefix" "$image_profile"
printf 'ghcr-template-repository=%s/conch-%s-template\n' "$ghcr_prefix" "$image_profile"
