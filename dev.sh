#!/usr/bin/env bash
# Install, then launch a windowed dev shell (no relogin needed).
set -euo pipefail
cd "$(dirname "$0")"

./install.sh
exec dbus-run-session -- gnome-shell --devkit
