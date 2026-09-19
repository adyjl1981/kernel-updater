"""Per-user launcher detection and installation; generation stays in the shell installer."""
import configparser
import os
from pathlib import Path
import re
import subprocess

# Tk title-cases the root class: avoid internal capitals. The instance is
# kernel-manager-gui, matching the desktop filename; keep StartupWMClass in
# install-desktop-entry.sh equal to this actual class (also used by children).
APP_CLASS = "Kernel-manager-gui"


def launcher_path():
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "applications/kernel-manager-gui.desktop"


def _value(text):
    """Decode the desktop-entry string layer (before Exec argument escaping)."""
    escapes = {"s": " ", "n": "\n", "t": "\t", "r": "\r", "\\": "\\"}
    return re.sub(r"\\(.)", lambda match: escapes[match[1]], text)


def _exec_arguments(text):
    # Exec is not shell syntax: only double quotes and four quoted escapes
    # are special. Reject field codes except literal %% in our fixed command.
    tokens = []
    while text:
        match = re.match(r'\s*("(?:\\.|[^"\\])*"|[^\s"\\]+)(?=\s|$)', text)
        if not match:
            raise ValueError("Invalid Exec argument")
        token = match[1]
        if token.startswith('"'):
            token = re.sub(r'\\([\\"`$])', r'\1', token[1:-1])
        if re.search(r"%(?!%)", token.replace("%%", "")):
            raise ValueError("Unexpected Exec field code")
        tokens.append(token.replace("%%", "%"))
        text = text[match.end():].lstrip()
    return tokens


def launcher_is_current(script_dir):
    """Accept only a visible launcher for this checkout, icon and window class."""
    script_dir = Path(script_dir).resolve()
    try:
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        parser.read_string(launcher_path().read_text(encoding="utf-8"))
        entry = parser["Desktop Entry"]
        if (entry.get("Type") != "Application" or
                entry.get("Hidden", "false").lower() != "false" or
                entry.get("NoDisplay", "false").lower() != "false" or
                entry.get("StartupWMClass") != APP_CLASS):
            return False
        args = _exec_arguments(_value(entry["Exec"]))
        app = script_dir / "kernel-manager-gui.py"
        # The installer uses python3; also accept an absolute Python 3 path.
        if len(args) == 2 and (args[0] == "python3" or
                (Path(args[0]).is_absolute() and Path(args[0]).name == "python3")):
            target = args[1]
        elif len(args) == 1:
            target = args[0]
        else:
            return False
        icon = Path(_value(entry["Icon"]))
        return (Path(target).is_absolute() and Path(target).resolve() == app and
                app.is_file() and icon.is_absolute() and
                icon.resolve() == script_dir / "kernel-manager-icon.png" and icon.is_file())
    except (OSError, UnicodeError, ValueError, KeyError, configparser.Error):
        return False


def install_launcher(script_dir):
    """Run the existing installer as the current user, without shell interpolation."""
    script_dir = Path(script_dir).resolve()
    installer = script_dir / "install-desktop-entry.sh"
    if not installer.is_file():
        raise RuntimeError(f"Desktop launcher installer is missing: {installer}")
    try:
        result = subprocess.run(["bash", str(installer)], cwd=script_dir,
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"Could not install the desktop launcher: {exc}") from exc
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit status {result.returncode}"
        raise RuntimeError(f"Could not install the desktop launcher:\n{detail}")
    if not launcher_is_current(script_dir):
        raise RuntimeError(f"The installer finished, but the launcher is invalid: {launcher_path()}")
    return launcher_path()
