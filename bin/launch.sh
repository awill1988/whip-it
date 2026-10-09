#!/bin/sh
set -eu
case "$(uname -sm)" in
  'Darwin arm64'|'Darwin x86_64') target=universal-apple-darwin;;
  'Linux x86_64') target=x86_64-unknown-linux-musl;;
  'Linux aarch64'|'Linux arm64') target=aarch64-unknown-linux-musl;;
  MINGW*|MSYS*)
    case "${PROCESSOR_ARCHITEW6432:-${PROCESSOR_ARCHITECTURE:-}}" in
      ARM64) target=aarch64-pc-windows-msvc;;
      AMD64) target=x86_64-pc-windows-msvc;;
      *) echo 'whip-it: unsupported windows architecture' >&2; exit 1;;
    esac;;
  *) echo 'whip-it: unsupported platform' >&2; exit 1;;
esac
binary="${0%/*}/native/$target/whip-it"
case "$target" in *-windows-*) binary="$binary.exe";; esac
exec "$binary" "$@"
