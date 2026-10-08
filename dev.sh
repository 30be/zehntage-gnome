#!/usr/bin/env bash
# Pack, install, and launch a windowed dev shell (no relogin needed).
set -euo pipefail
cd "$(dirname "$0")"

gnome-extensions pack --force \
    --extra-source=selector.js \
    --extra-source=claude.js \
    --extra-source=cli.js \
    --extra-source=history.js \
    --extra-source=indicator.js \
    .
gnome-extensions install --force zehntage-gnome@lyka.shell-extension.zip
rm zehntage-gnome@lyka.shell-extension.zip

exec dbus-run-session -- gnome-shell --devkit
