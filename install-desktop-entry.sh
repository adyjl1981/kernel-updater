#!/usr/bin/env bash
#
# install-desktop-entry.sh
#
# Creates a .desktop launcher for Kernel Manager GUI so it shows up in the
# application grid and can be pinned to the dock/taskbar. Run this once
# from wherever you've placed the app's files (it detects the paths
# automatically).
#
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
APP_SCRIPT="$SCRIPT_DIR/kernel-manager-gui.py"
ICON_FILE="$SCRIPT_DIR/kernel-manager-icon.png"
DESKTOP_DIR="$HOME/.local/share/applications"
DESKTOP_FILE="$DESKTOP_DIR/kernel-manager-gui.desktop"

[[ -f "$APP_SCRIPT" ]] || { echo "Can't find kernel-manager-gui.py in $SCRIPT_DIR"; exit 1; }
[[ -f "$ICON_FILE"  ]] || { echo "Can't find kernel-manager-icon.png in $SCRIPT_DIR"; exit 1; }

chmod +x "$APP_SCRIPT"
mkdir -p "$DESKTOP_DIR"

cat > "$DESKTOP_FILE" << EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=Kernel Manager
Comment=Build, install, and manage custom Linux kernels
Exec=python3 "$APP_SCRIPT"
Icon=$ICON_FILE
Terminal=false
Categories=System;Settings;
StartupNotify=true
EOF

chmod +x "$DESKTOP_FILE"

# GNOME (Ubuntu's default desktop) marks new launchers as "untrusted" until
# explicitly trusted, which normally requires a manual right-click step.
# gio does this automatically where supported.
if command -v gio >/dev/null 2>&1; then
  gio set "$DESKTOP_FILE" metadata::trusted true 2>/dev/null || true
fi

# Refresh the desktop database so the app grid picks it up immediately
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
fi

echo "Installed launcher: $DESKTOP_FILE"
echo
echo "Next steps:"
echo "  1. Open the Activities/app grid and search for 'Kernel Manager'."
echo "  2. Right-click its icon and choose 'Add to Favorites' (or 'Pin to Dash')"
echo "     to pin it to the dock."
