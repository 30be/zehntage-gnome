#!/usr/bin/env bash
# Pack and install the extension for the current user. `gnome-extensions
# install` compiles the schemas. On Wayland a new version activates after
# re-login (or use ./dev.sh to try it in a windowed dev shell).
set -euo pipefail
cd "$(dirname "$0")"

out=$(mktemp -d)
trap 'rm -rf "$out"' EXIT
gnome-extensions pack --force --out-dir "$out" \
    --extra-source=claude.js \
    --extra-source=cli.js \
    --extra-source=history.js \
    --extra-source=indicator.js \
    --extra-source=selector.js \
    .
gnome-extensions install --force "$out"/zehntage-gnome@lyka.shell-extension.zip
echo "installed $(jq -r '."version-name"' metadata.json)"
