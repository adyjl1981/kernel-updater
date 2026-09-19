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
DESKTOP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
DESKTOP_FILE="$DESKTOP_DIR/kernel-manager-gui.desktop"

[[ -f "$APP_SCRIPT" ]] || { echo "Can't find kernel-manager-gui.py in $SCRIPT_DIR"; exit 1; }
[[ -f "$ICON_FILE"  ]] || { echo "Can't find kernel-manager-icon.png in $SCRIPT_DIR"; exit 1; }

# Tkinter is needed by both the app and its password dialog.
python3 -c 'import tkinter' || { echo "Install Python 3 Tkinter before creating the launcher." >&2; exit 1; }
[[ -r "$SCRIPT_DIR/askpass-gui.py" && -x "$SCRIPT_DIR/askpass-gui.py" ]] || { echo "askpass-gui.py must be readable and executable" >&2; exit 1; }
mkdir -p "$DESKTOP_DIR"

# Desktop-entry escaping has two layers: string values, then Exec arguments.
python3 - "$DESKTOP_FILE" "$APP_SCRIPT" "$ICON_FILE" <<'PYTHON'
import sys
from pathlib import Path

def value(text):
    return text.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")

def argument(text):
    escaped = "".join("\\" + c if c in '\\"`$' else "%%" if c == "%" else c for c in text)
    return value('"' + escaped + '"')

output, app, icon = sys.argv[1:]
Path(output).write_text(
    "[Desktop Entry]\nType=Application\nVersion=1.0\nName=Kernel Manager\n"
    "Comment=Build, install, and manage custom Linux kernels\n"
    f"Exec=python3 {argument(app)}\nIcon={value(icon)}\n"
    "Terminal=false\nCategories=System;Settings;\nStartupNotify=true\n"
    "StartupWMClass=Kernel-manager-gui\n"
)
PYTHON

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
