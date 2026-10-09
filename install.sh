#!/bin/sh
# Downloading is an explicit setup operation; hooks never invoke this script.
set -eu

version=${WHIP_IT_VERSION:-0.1.0}
case "$version" in ''|*[!0-9.]*) echo 'invalid release version' >&2; exit 1;; esac
case "$(uname -s)/$(uname -m)" in
  Darwin/arm64|Darwin/x86_64) target=universal-apple-darwin;;
  Linux/x86_64) target=x86_64-unknown-linux-musl;;
  Linux/aarch64|Linux/arm64) target=aarch64-unknown-linux-musl;;
  *) echo 'unsupported platform; use install.ps1 on windows' >&2; exit 1;;
esac

install_dir=${WHIP_IT_INSTALL_DIR:-"$HOME/.local/bin"}
case "$install_dir" in /*) ;; *) echo 'install directory must be absolute' >&2; exit 1;; esac
mkdir -p "$install_dir"
work_dir=$(mktemp -d "$install_dir/.whip-it-install.XXXXXXXX")
trap 'rm -rf "$work_dir"' EXIT HUP INT TERM
archive="whip-it-$target.tar.gz"
base="https://github.com/awill1988/whip-it/releases/download/v$version"
for name in "$archive" "$archive.sha256"; do
  curl --proto '=https' --tlsv1.2 --fail --location --retry 2 \
    --connect-timeout 10 --max-time 120 "$base/$name" -o "$work_dir/$name"
done
expected=$(awk 'NR == 1 {print $1}' "$work_dir/$archive.sha256")
case "$expected" in ''|*[!0-9a-fA-F]*) echo 'invalid checksum' >&2; exit 1;; esac
[ "${#expected}" -eq 64 ] || { echo 'invalid checksum length' >&2; exit 1; }
if command -v sha256sum >/dev/null 2>&1; then
  actual=$(sha256sum "$work_dir/$archive" | awk '{print $1}')
else
  actual=$(shasum -a 256 "$work_dir/$archive" | awk '{print $1}')
fi
[ "$actual" = "$expected" ] || { echo 'checksum mismatch; existing installation preserved' >&2; exit 1; }
tar -xzf "$work_dir/$archive" -C "$work_dir" whip-it
[ -f "$work_dir/whip-it" ] && [ ! -L "$work_dir/whip-it" ] || exit 1
chmod 755 "$work_dir/whip-it"
"$work_dir/whip-it" --version
[ ! -L "$install_dir/whip-it" ] || { echo 'refusing to replace a managed symlink' >&2; exit 1; }
mv -f "$work_dir/whip-it" "$install_dir/whip-it"

if [ "${WHIP_IT_NO_MODIFY_PATH:-0}" != 1 ]; then
  # Quote the directory as data, including spaces and literal shell characters.
  quoted_dir=$(printf '%s' "$install_dir" | sed "s/'/'\\\\''/g")
  path_line="export PATH='$quoted_dir':\"\$PATH\" # whip-it installer"
  case "${SHELL:-}" in
    */zsh) profile="$HOME/.zshrc";;
    */bash) profile="$HOME/.bashrc";;
    */fish)
      profile="${XDG_CONFIG_HOME:-$HOME/.config}/fish/conf.d/whip-it.fish"
      mkdir -p "$(dirname "$profile")"
      path_line="fish_add_path '$quoted_dir' # whip-it installer";;
    *) profile="$HOME/.profile";;
  esac
  if ! grep -Fqx "$path_line" "$profile" 2>/dev/null; then
    printf '\n%s\n' "$path_line" >> "$profile"
  fi
fi
printf 'installed: %s/whip-it\nrestart your terminal and agent client before enabling hooks\n' "$install_dir"
