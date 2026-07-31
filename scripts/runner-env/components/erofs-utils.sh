#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: erofs-utils.sh SOURCE_DIR OUTPUT_FILE" >&2
  exit 2
fi

source_dir=$1
output_file=$2

for command_name in autoconf automake gcc libtoolize make pkg-config; do
  command -v "$command_name" >/dev/null
done

cd "$source_dir"
./autogen.sh
configure_options=(--with-libzstd)
if pkg-config --exists libdeflate; then
  configure_options+=(--with-libdeflate)
fi
./configure "${configure_options[@]}"
make -j"$(nproc)"
install -m 0755 mkfs/mkfs.erofs "$output_file"
case "$(uname -m)" in
  aarch64) file "$output_file" | grep -Eq 'ELF 64-bit.*ARM aarch64' ;;
  x86_64) file "$output_file" | grep -Eq 'ELF 64-bit.*x86-64' ;;
  *) echo "unsupported erofs-utils build architecture: $(uname -m)" >&2; exit 1 ;;
esac
"$output_file" --version
