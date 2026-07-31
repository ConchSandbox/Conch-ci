#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: prepare-erofs-utils.sh --work-dir DIR --prefix DIR [--archive FILE] [--add-to-path]" >&2
}

work_dir=
prefix=
source_archive=
add_to_path=false
while (($#)); do
  case "$1" in
    --work-dir) work_dir=${2:?}; shift 2 ;;
    --prefix) prefix=${2:?}; shift 2 ;;
    --archive) source_archive=${2:?}; shift 2 ;;
    --add-to-path) add_to_path=true; shift ;;
    *) usage; exit 2 ;;
  esac
done
[[ -n "$work_dir" && "$work_dir" != / ]]
[[ -n "$prefix" && "$prefix" != / ]]

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
version=$(python3 "$script_dir/lib/lock.py" get managed_components.erofs_utils.version)
expected_sha256=$(python3 "$script_dir/lib/lock.py" get managed_components.erofs_utils.sha256)
clean_version=${version#v}
url="https://git.kernel.org/pub/scm/linux/kernel/git/xiang/erofs-utils.git/snapshot/erofs-utils-${clean_version}.tar.gz"

for target_dir in "$work_dir" "$prefix"; do
  if [[ -e "$target_dir" ]]; then
    find "$target_dir" -depth -mindepth 1 -delete
  else
    mkdir -p "$target_dir"
  fi
done
mkdir -p "$prefix/bin"
if [[ -n "$source_archive" ]]; then
  [[ -f "$source_archive" && ! -L "$source_archive" ]]
  archive=$source_archive
else
  archive="$work_dir/erofs-utils.tar.gz"
  curl --proto '=https' --tlsv1.2 --fail --location --retry 3 --retry-all-errors \
    --output "$archive" "$url"
fi
actual_sha256=$(sha256sum "$archive" | awk '{print $1}')
[[ "$actual_sha256" == "$expected_sha256" ]] || {
  echo "erofs-utils source digest mismatch: expected $expected_sha256, got $actual_sha256" >&2
  exit 1
}
python3 "$script_dir/lib/archive.py" \
  "$archive" "$work_dir" --expect-top "erofs-utils-$clean_version"
source_dir="$work_dir/erofs-utils-$clean_version"
output_file="$prefix/bin/mkfs.erofs"
for command_name in autoconf automake gcc libtoolize make pkg-config; do
  command -v "$command_name" >/dev/null
done
(
  cd "$source_dir"
  ./autogen.sh
  configure_options=(--with-libzstd --without-libdeflate)
  ./configure "${configure_options[@]}"
  make -j"$(nproc)"
  install -m 0755 mkfs/mkfs.erofs "$output_file"
)
case "$(uname -m)" in
  aarch64) file "$output_file" | grep -Eq 'ELF 64-bit.*ARM aarch64' ;;
  x86_64) file "$output_file" | grep -Eq 'ELF 64-bit.*x86-64' ;;
  *) echo "unsupported erofs-utils build architecture: $(uname -m)" >&2; exit 1 ;;
esac
"$output_file" --version
if [[ "$add_to_path" == true ]]; then
  [[ -n "${GITHUB_PATH:-}" ]]
  printf '%s\n' "$prefix/bin" >> "$GITHUB_PATH"
fi
