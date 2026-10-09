#!/bin/sh
# Network access belongs to explicit setup; installed hooks only execute locally.
set -eu
version=0.1.0
client=
while [ "$#" -gt 0 ]; do
  case "$1" in
    --client) client=${2:?missing client}; shift 2;;
    --version) version=${2:?missing version}; shift 2;;
    *) echo 'usage: install.sh --client claude|codex|agy [--version VERSION]' >&2; exit 1;;
  esac
done
case "$client" in claude|codex|agy) ;; *) echo 'choose --client claude, codex, or agy' >&2; exit 1;; esac
printf '%s\n' "$version" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+(-[A-Za-z0-9.-]+)?$' || { echo 'invalid version' >&2; exit 1; }
command -v "$client" >/dev/null 2>&1 || { echo "client not installed: $client" >&2; exit 1; }
case "$(uname -s)/$(uname -m)" in
  Darwin/arm64|Darwin/x86_64) target=universal-apple-darwin;;
  Linux/x86_64) target=x86_64-unknown-linux-musl;;
  Linux/aarch64|Linux/arm64) target=aarch64-unknown-linux-musl;;
  *) echo 'unsupported platform; use install.ps1 on windows' >&2; exit 1;;
esac
install_dir=${WHIP_IT_INSTALL_DIR:-"${XDG_DATA_HOME:-$HOME/.local/share}/whip-it/plugins"}
case "$install_dir" in /*) ;; *) echo 'install directory must be absolute' >&2; exit 1;; esac
mkdir -p "$install_dir"
work_dir=$(mktemp -d "$install_dir/.whip-it-install.XXXXXXXX")
trap 'rm -rf "$work_dir"' EXIT HUP INT TERM
archive="whip-it-plugin-$target.tar.gz"
base="https://github.com/awill1988/whip-it/releases/download/v$version"
for name in "$archive" "$archive.sha256"; do
  curl --proto '=https' --tlsv1.2 --fail --location --retry 2 \
    --connect-timeout 10 --max-time 120 "$base/$name" -o "$work_dir/$name"
done
expected=$(awk 'NR == 1 {print $1}' "$work_dir/$archive.sha256")
case "$expected" in ''|*[!0-9a-f]*) echo 'invalid checksum' >&2; exit 1;; esac
[ "${#expected}" -eq 64 ] || { echo 'invalid checksum length' >&2; exit 1; }
if command -v sha256sum >/dev/null 2>&1; then
  actual=$(sha256sum "$work_dir/$archive" | awk '{print $1}')
else
  actual=$(shasum -a 256 "$work_dir/$archive" | awk '{print $1}')
fi
[ "$actual" = "$expected" ] || { echo 'checksum mismatch; existing installation preserved' >&2; exit 1; }
mkdir "$work_dir/plugin"
# Extract only the release package's known files; never follow archive links.
files='plugin.json hooks.json LICENSE release.json .claude-plugin/plugin.json .claude-plugin/marketplace.json .codex-plugin/plugin.json hooks/hooks.json hooks/codex-plugin.json bin/whip-it'
tar -tvzf "$work_dir/$archive" | awk '$1 !~ /^[-d]/ {bad=1} END {exit bad}' || { echo 'archive contains links or special files' >&2; exit 1; }
# Intentional splitting: this constant list contains no spaces in filenames.
# shellcheck disable=SC2086
tar -xzf "$work_dir/$archive" -C "$work_dir/plugin" $files
[ "$("$work_dir/plugin/bin/whip-it" --version)" = "whip-it $version" ] || { echo 'release version mismatch' >&2; exit 1; }
destination="$install_dir/$version-$target-$expected"
if [ -e "$destination" ]; then
  diff -r "$work_dir/plugin" "$destination" >/dev/null || { echo 'existing release directory differs' >&2; exit 1; }
else
  mv "$work_dir/plugin" "$destination"
fi
case "$client" in
  claude) claude plugin marketplace add "$destination"; claude plugin install whip-it@awill1988;;
  codex)
    if output=$(codex plugin marketplace add "$destination" 2>&1); then
      printf '%s\n' "$output"
    else
      case "$output" in
        *"marketplace 'awill1988' is already added from a different source"*)
          codex plugin marketplace remove awill1988
          codex plugin marketplace add "$destination";;
        *) printf '%s\n' "$output" >&2; exit 1;;
      esac
    fi
    codex --enable plugins plugin add whip-it@awill1988;;
  agy) agy plugin install "$destination";;
esac
printf 'installed whip-it %s for %s\nrestart the client and trust its hooks, then verify a delegation denial\n' "$version" "$client"
