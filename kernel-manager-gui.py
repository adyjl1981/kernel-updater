#!/usr/bin/env python3
"""
Kernel Manager GUI
==================

A lightweight (Tkinter, no heavy dependencies — chosen deliberately for
low-RAM machines) GUI front-end for the build-custom-kernel.sh /
install-custom-kernel.sh scripts.

Features:
  1. Shows currently installed kernels and which one is running.
  2. Lets you delete surplus kernels (apt-managed or custom-built).
  3. Checks kernel.org for a newer stable version than what's running.
  4. Lets you pick a toolchain (GCC / Clang, +LTO, +full debug info) and
     kick off a build.
  5. Streams the build's live output into the window.
  6. Offers an "Install Now" button once a build finishes successfully.

Expects build-custom-kernel.sh, install-custom-kernel.sh, and
askpass-gui.py to live in the same directory as this script.
"""

import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import stat
import tempfile
import subprocess
import sys
import threading
import time
import queue
import urllib.request
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path

from ubuntu_theme import apply_theme, scrolled_text, scrolled_tree

SCRIPT_DIR = Path(__file__).resolve().parent
BUILD_SCRIPT = SCRIPT_DIR / "build-custom-kernel.sh"
INSTALL_SCRIPT = SCRIPT_DIR / "install-custom-kernel.sh"
ASKPASS_SCRIPT = SCRIPT_DIR / "askpass-gui.py"
KERNEL_BUILD_DIR = Path.home() / "kernel-build"
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "kernel-manager-gui"
# Continue using existing keys; generate new ones in private user storage.
LEGACY_MOK_DIR = SCRIPT_DIR / "mok"
MOK_DIR = LEGACY_MOK_DIR if any(LEGACY_MOK_DIR.glob("MOK.*")) else CONFIG_DIR / "mok"
CONFIG_FILE = CONFIG_DIR / "config.json"
GRUB_DEFAULTS_FILE = Path("/etc/default/grub")

VER_RE = re.compile(r"^\d+\.\d+(\.\d+)?$")


def running_kernel() -> str:
    return subprocess.check_output(["uname", "-r"], text=True).strip()


def gui_env() -> dict:
    """Environment for subprocesses launched by this GUI: points sudo at
    our askpass helper so password prompts show up as a dialog instead of
    failing (there's no terminal attached to a GUI-launched process)."""
    if not ASKPASS_SCRIPT.is_file() or not os.access(ASKPASS_SCRIPT, os.R_OK | os.X_OK):
        raise RuntimeError(f"Missing or inaccessible password helper: {ASKPASS_SCRIPT}")
    env = os.environ.copy()
    env["SUDO_ASKPASS"] = str(ASKPASS_SCRIPT)
    env["DEBIAN_FRONTEND"] = "noninteractive"
    env["NEEDRESTART_MODE"] = "a"
    return env


def send_notification(title: str, body: str):
    """Best-effort desktop notification. Silently does nothing if
    notify-send isn't available rather than failing the calling operation."""
    if shutil.which("notify-send"):
        try:
            subprocess.run(["notify-send", title, body], timeout=5)
        except Exception:
            pass


def load_presets() -> dict:
    try:
        data = json.loads(CONFIG_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_presets(d: dict):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=CONFIG_DIR, delete=False) as f:
            json.dump(d, f, indent=2)
        os.replace(f.name, CONFIG_FILE)
    except OSError as e:
        print(f"Could not save preferences: {e}", file=sys.stderr)


def find_terminal_emulator():
    for candidate in ("gnome-terminal", "konsole", "xfce4-terminal",
                       "xterm", "x-terminal-emulator"):
        path = shutil.which(candidate)
        if path:
            return path
    return None


def secure_boot_state() -> str:
    if not shutil.which("mokutil"):
        return "unknown (mokutil not installed)"
    try:
        out = subprocess.check_output(["mokutil", "--sb-state"], text=True,
                                       stderr=subprocess.STDOUT, timeout=10)
        return out.strip()
    except Exception as e:
        return f"unknown ({e})"


def cpu_info() -> dict:
    model = "unknown"
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    model = line.split(":", 1)[1].strip()
                    break
    except Exception:
        pass
    return {"model": model, "cores": os.cpu_count() or 1}


def mem_swap_info() -> dict:
    info = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                if key in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
                    info[key] = int(rest.strip().split()[0])  # kB
    except Exception:
        pass
    return info


def read_cpu_times():
    """First line of /proc/stat: cpu  user nice system idle iowait irq softirq..."""
    with open("/proc/stat") as f:
        parts = f.readline().split()
    values = [int(x) for x in parts[1:]]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    total = sum(values[:8])  # guest times are already included in user/nice
    return idle, total


def run_command(cmd, env=None, log_fn=lambda text: None):
    cmd = [str(arg) for arg in cmd]
    if cmd[0] == "sudo":
        cmd.insert(1, "--preserve-env=DEBIAN_FRONTEND,NEEDRESTART_MODE")
    log_fn(f"$ {shlex.join(cmd)}\n")
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    log_fn(proc.stdout + proc.stderr)
    if proc.returncode:
        raise RuntimeError(f"Command failed ({proc.returncode}): {shlex.join(cmd)}")
    return proc


def grub_config_path():
    for name in ("/boot/grub/grub.cfg", "/boot/grub2/grub.cfg"):
        path = Path(name)
        if path.is_file():
            return path
    raise RuntimeError("No supported GRUB configuration was found.")


def read_grub_config(env=None, path=None):
    path = grub_config_path() if path is None else path
    try:
        return path.read_text()
    except PermissionError:
        return run_command(["sudo", "-A", "cat", str(path)], env or gui_env()).stdout


def parse_grub_entries(text, with_images=False):
    """Read title paths from generated GRUB menu/submenu blocks."""
    entries, stack, images = [], [], {}
    for line in text.splitlines():
        line = line.strip()
        if line == "}":
            if stack:
                stack.pop()
            continue
        if re.match(r"^linux(?:efi|16)?\s", line) and stack and stack[-1][0] == "menuentry":
            words = shlex.split(line, comments=True)
            if len(words) > 1:
                entry = ">".join(name for _, name in stack)
                images.setdefault(entry, []).append(words[1])
        if not re.match(r"^(menuentry|submenu)\s", line):
            continue
        words = shlex.split(line, comments=True)
        if len(words) < 3 or words[-1] != "{":
            raise RuntimeError("Unsupported GRUB entry format; select the boot entry manually.")
        kind, title = words[:2]
        if kind == "menuentry":
            entries.append(">".join([name for typ, name in stack if typ == "submenu"] + [title]))
        stack.append((kind, title))
    if stack:
        raise RuntimeError("Unbalanced GRUB menu blocks; select the boot entry manually.")
    return [(entry, images.get(entry, [])) for entry in entries] if with_images else entries


def grub_menu_titles(env=None):
    try:
        titles = parse_grub_entries(read_grub_config(env))
        return titles, None if titles else "No supported menu entries found (BLS entries require manual selection)."
    except Exception as e:
        return [], str(e)


def grub_update_command():
    for cfg, commands in (("/boot/grub/grub.cfg", ("update-grub", "grub-mkconfig")),
                          ("/boot/grub2/grub.cfg", ("grub2-mkconfig",))):
        if Path(cfg).is_file():
            for command in commands:
                if shutil.which(command):
                    return [command] if command == "update-grub" else [command, "-o", cfg]
    raise RuntimeError("No supported GRUB update command/configuration. Manage this kernel manually.")


def read_system_file(path: Path, env: dict):
    try:
        return path.read_text()
    except PermissionError:
        return run_command(["sudo", "-A", "cat", path], env).stdout


def saved_default_config(text: str):
    """Return /etc/default/grub with only its effective GRUB_DEFAULT changed."""
    lines = text.splitlines(keepends=True)
    assignments = [i for i, line in enumerate(lines)
                   if re.match(r"^\s*(?:export\s+)?GRUB_DEFAULT\s*=", line)]
    if len(assignments) > 1:
        raise RuntimeError("Multiple active GRUB_DEFAULT settings were found; no boot setting was changed.")
    replacement = "GRUB_DEFAULT=saved"
    if assignments:
        ending = "\n" if lines[assignments[0]].endswith("\n") else ""
        lines[assignments[0]] = replacement + ending
    else:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(replacement + "\n")
    return "".join(lines)


def enable_saved_grub_default(env: dict, log_fn, config_path: Path):
    """Enable GRUB's saved default, restoring both files if regeneration fails."""
    defaults = GRUB_DEFAULTS_FILE
    if not defaults.is_file() or defaults.is_symlink():
        raise RuntimeError(f"{defaults} is not a regular configuration file; no boot setting was changed.")
    original = read_system_file(defaults, env)
    updated = saved_default_config(original)
    update_command = grub_update_command()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = defaults.with_name(f"{defaults.name}.kernel-manager-backup-{stamp}-{os.getpid()}")
    if backup.exists():
        raise RuntimeError(f"Refusing to replace existing backup {backup}; no boot setting was changed.")

    temporary = None
    changed = updated != original
    backup_created = False
    try:
        if changed:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", prefix="kernel-manager-grub-",
                                             delete=False) as staged:
                staged.write(updated)
                temporary = Path(staged.name)
            temporary.chmod(0o644)
            run_command(["sudo", "-A", "cp", "--preserve=all", "--", defaults, backup], env, log_fn)
            backup_created = True
            run_command(["sudo", "-A", "install", "-o", "root", "-g", "root", "-m", "0644",
                         "--", temporary, defaults], env, log_fn)
        run_command(["sudo", "-A", *update_command], env, log_fn)
        verified_defaults = read_system_file(defaults, env)
        generated = read_grub_config(env, path=config_path)
        if saved_default_config(verified_defaults) != verified_defaults or not re.search(
                r'^\s*set default=["\']?\$\{saved_entry\}["\']?\s*$', generated, re.M):
            raise RuntimeError("GRUB regeneration did not enable the saved default.")
    except Exception as error:
        if changed and backup_created:
            try:
                run_command(["sudo", "-A", "cp", "--preserve=all", "--", backup, defaults], env, log_fn)
                run_command(["sudo", "-A", *update_command], env, log_fn)
            except Exception as rollback_error:
                raise RuntimeError(f"Failed to enable GRUB saved defaults ({error}); rollback also failed ({rollback_error}). Inspect {defaults}, {backup}, and {config_path} before rebooting.") from error
            raise RuntimeError(f"Failed to enable GRUB saved defaults ({error}). The original configuration was restored from {backup}.") from error
        if changed:
            raise RuntimeError(f"Failed to back up {defaults} ({error}); no boot setting was changed.") from error
        raise RuntimeError(f"Failed to regenerate GRUB with its saved-default setting ({error}).") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    if changed:
        log_fn(f"Enabled GRUB_DEFAULT=saved; backup retained at {backup}.\n")
    return read_grub_config(env, path=config_path)


def validate_release(version):
    if not re.fullmatch(r"[0-9]+\.[0-9]+[a-zA-Z0-9._+-]*", version):
        raise RuntimeError("Invalid kernel release.")
    return version


def completed_build_dirs():
    result = []
    for marker in KERNEL_BUILD_DIR.glob("linux-*/.kernel-manager-complete"):
        tree = marker.parent
        try:
            if tree.is_symlink() or not marker.read_text().strip():
                continue
            release = validate_release((tree / "include/config/kernel.release").read_text().strip())
            if Path(f"/lib/modules/{release}").exists() or Path(f"/boot/vmlinuz-{release}").exists():
                continue
            if not (tree / ".kernel-manager-make-args").is_file():
                continue
            checked = subprocess.run(["sha256sum", "--status", "-c", ".kernel-manager-checksums"], cwd=tree,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if checked.returncode == 0:
                result.append((marker.stat().st_mtime, tree))
        except (OSError, RuntimeError):
            continue
    return [tree for _, tree in sorted(result, reverse=True)]


def find_source_tree(version):
    for release in KERNEL_BUILD_DIR.glob("linux-*/include/config/kernel.release"):
        try:
            if release.read_text().strip() == version:
                return release.parents[2]
        except OSError:
            continue
    raise RuntimeError(f"No matching built source tree for {version}.")


def kernel_build_source_dirs():
    """Extracted linux-<version> source trees under ~/kernel-build, which
    is where most of the disk space from repeated builds accumulates."""
    dirs = []
    if not KERNEL_BUILD_DIR.exists():
        return dirs
    for entry in sorted(KERNEL_BUILD_DIR.iterdir()):
        if entry.is_dir() and not entry.is_symlink() and entry.name.startswith("linux-"):
            try:
                size_out = subprocess.check_output(["du", "-sh", str(entry)], text=True)
                size = size_out.split()[0]
            except Exception:
                size = "?"
            dirs.append((entry, size))
    return dirs


def build_log_files():
    if not KERNEL_BUILD_DIR.exists():
        return []
    logs = sorted(KERNEL_BUILD_DIR.glob("build-*.log"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    return logs


# ---------------------------------------------------------------------------
# Kernel discovery / management
# ---------------------------------------------------------------------------

class KernelInfo:
    def __init__(self, version, apt_managed, running, size_str, manager=None):
        self.version = version
        self.apt_managed = apt_managed
        self.running = running
        self.size_str = size_str
        self.manager = manager or ("apt" if apt_managed else "custom")


def package_manager_for_kernel(version):
    """Unknown ownership must never authorize direct removal of files."""
    paths = [f"/boot/vmlinuz-{version}", f"/lib/modules/{version}",
             f"/usr/lib/modules/{version}", f"/usr/lib/modules/{version}/vmlinuz"]
    found_manager = False
    for tool, manager, args in (("dpkg-query", "apt", ["-S"]),
                                ("rpm", "rpm", ["-qf"]),
                                ("pacman", "pacman", ["-Qo"])):
        if not shutil.which(tool):
            continue
        found_manager = True
        for path in paths:
            proc = subprocess.run([tool, *args, path], env=dict(os.environ, LC_ALL="C"), capture_output=True, text=True)
            if proc.returncode == 0:
                return manager
            missing = {"apt": "no path found matching pattern", "rpm": "is not owned by any package", "pacman": "No package owns"}
            if proc.returncode != 1 or missing[manager] not in proc.stdout + proc.stderr:
                return "unknown"
    return "custom" if found_manager else "unknown"


def list_installed_kernels():
    kernels = []
    modules_root = Path("/lib/modules")
    if not modules_root.exists():
        return kernels
    current = running_kernel()
    for entry in sorted(modules_root.iterdir()):
        if not entry.is_dir() or not re.fullmatch(r"[0-9]+\.[0-9]+[a-zA-Z0-9._+-]*", entry.name):
            continue
        manager = package_manager_for_kernel(entry.name)
        try:
            size = subprocess.check_output(["du", "-sh", str(entry)], text=True).split()[0]
        except (OSError, subprocess.SubprocessError):
            size = "?"
        kernels.append(KernelInfo(entry.name, manager == "apt", entry.name == current, size, manager))
    return kernels


def latest_stable_version() -> str | None:
    """Use kernel.org's structured release field, with a bounded Git fallback."""
    try:
        with urllib.request.urlopen("https://www.kernel.org/releases.json", timeout=15) as response:
            version = json.load(response)["latest_stable"]["version"]
        if VER_RE.fullmatch(version):
            return version
    except Exception:
        pass
    try:
        out = subprocess.check_output(
            ["git", "ls-remote", "--tags", "--refs",
             "https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git"],
            text=True, timeout=30, stderr=subprocess.DEVNULL
        )
    except Exception:
        return None

    versions = []
    for line in out.splitlines():
        if "refs/tags/v" not in line:
            continue
        tag = line.split("refs/tags/v", 1)[1].strip()
        if VER_RE.match(tag):
            versions.append(tuple(int(x) for x in tag.split(".")))

    if not versions:
        return None
    best = max(versions)
    return ".".join(str(x) for x in best)


def installed_kernel_packages(version):
    proc = run_command(["dpkg-query", "-W", "-f=${binary:Package}\t${db:Status-Status}\n"])
    pattern = re.compile(r"linux-(?:image(?:-unsigned)?|headers|modules(?:-extra)?)-" + re.escape(version) + r"(?::[\w-]+)?")
    return [name for line in proc.stdout.splitlines() if "\t" in line
            for name, status in [line.split("\t", 1)]
            if status == "installed" and pattern.fullmatch(name)]


def delete_kernel(kernel: KernelInfo, env: dict, log_fn):
    version = validate_release(kernel.version)
    current = running_kernel()
    if kernel.running or version == current:
        raise RuntimeError("Refusing to delete the running kernel.")
    manager = package_manager_for_kernel(version)
    if manager not in ("apt", "custom"):
        raise RuntimeError(f"Kernel ownership is {manager}; use the distribution's package manager.")
    if not Path(f"/boot/vmlinuz-{current}").is_file():
        raise RuntimeError("Could not verify the running kernel's boot image as a fallback.")
    grub_cmd = grub_update_command()  # Preflight before removing anything.
    if re.search(r"^\s*blscfg\b", read_grub_config(env), re.M):
        raise RuntimeError("BLS kernel removal requires the distribution's tools.")
    if manager == "apt":
        pkgs = installed_kernel_packages(version)
        if not pkgs or not any(p.startswith("linux-image-") for p in pkgs):
            raise RuntimeError("Could not identify the installed kernel image package; nothing removed.")
        simulation_env = dict(env, LC_ALL="C")
        simulation = run_command(["apt-get", "-s", "purge", *pkgs], simulation_env, log_fn)
        removals = set(re.findall(r"^(?:Remv|Purg) (\S+)", simulation.stdout, re.M))
        allowed = {p.split(":")[0] for p in pkgs}
        if not removals or any(p.split(":")[0] not in allowed for p in removals):
            raise RuntimeError("APT would remove other packages; review the removal manually.")
        run_command(["sudo", "-A", "apt-get", "purge", "-y", *pkgs], env, log_fn)
    else:
        paths = [Path(f"/boot/{prefix}{version}{suffix}") for prefix, suffix in
                 (("vmlinuz-", ""), ("System.map-", ""), ("config-", ""),
                  ("initrd.img-", ""), ("initramfs-", ".img"))]
        modules = Path(f"/lib/modules/{version}")
        if modules.is_symlink() or any(p.is_symlink() for p in paths):
            raise RuntimeError("Unexpected symlink in this kernel's installation; remove it manually.")
        for path in paths:
            if path.exists():
                run_command(["sudo", "-A", "rm", "-f", "--", path], env, log_fn)
        if modules.exists():
            run_command(["sudo", "-A", "rm", "-rf", "--", modules], env, log_fn)
    run_command(["sudo", "-A", *grub_cmd], env, log_fn)


def grub_entry_title(version, env=None, config=None):
    validate_release(version)
    titles, error = grub_menu_titles(env) if config is None else (parse_grub_entries(config), None)
    if error:
        raise RuntimeError(error)
    pattern = re.compile(r"(?<![\w.+-])" + re.escape(version) + r"(?![\w.+-])")
    matches = [title for title in titles if pattern.search(title.rsplit(">", 1)[-1]) and "recovery" not in title.lower()]
    if len(matches) != 1:
        raise RuntimeError("Could not identify one unambiguous GRUB entry; select it manually.")
    return matches[0]


def set_default_boot(version: str, env: dict, log_fn, once: bool):
    """Select a verified GRUB entry, enabling saved defaults when required."""
    validate_release(version)
    config_path = grub_config_path()
    config = read_grub_config(env, path=config_path)
    if re.search(r"^\s*blscfg\b", config, re.M):
        raise RuntimeError("BLS boot entries are unsupported here; use the distribution's boot tools.")
    entry = grub_entry_title(version, env, config=config)
    if once and ("next_entry" not in config or "save_env next_entry" not in config):
        raise RuntimeError("This GRUB configuration does not support a one-time saved entry.")
    if not Path(f"/boot/vmlinuz-{version}").is_file() or not Path(f"/lib/modules/{version}").is_dir():
        raise RuntimeError("The selected kernel is no longer fully installed. Refresh the kernel list; no boot setting was changed.")
    images = dict(parse_grub_entries(config, with_images=True)).get(entry, [])
    if len(images) != 1 or Path(images[0]).name != f"vmlinuz-{version}" or "$" in images[0]:
        raise RuntimeError("The selected GRUB entry does not unambiguously load this kernel. No boot setting was changed.")
    if not re.search(r"^\s*load_env\s*$", config, re.M) or re.search(r"^\s*(?:load_env|save_env)\s+-f\b", config, re.M):
        raise RuntimeError("This GRUB configuration does not use the standard environment block. No boot setting was changed.")
    # Validate the matching GRUB tools and environment block before changing
    # /etc/default/grub, so an unavailable tool or pending override cannot
    # leave a needless partial configuration change.
    prefix = "grub2" if config_path.parent.name == "grub2" else "grub"
    command = f"{prefix}-{'reboot' if once else 'set-default'}"
    editor = f"{prefix}-editenv"
    if not shutil.which(command) or not shutil.which(editor):
        raise RuntimeError("GRUB boot-selection/verification commands are not installed.")
    env_file = config_path.parent / "grubenv"
    read_env = ["sudo", "-A", editor, str(env_file), "list"]
    before = run_command(read_env, env, log_fn).stdout
    before_values = dict(line.split("=", 1) for line in before.splitlines() if "=" in line)
    if not once and (before_values.get("next_entry") or before_values.get("prev_saved_entry")):
        raise RuntimeError("A one-time GRUB boot override is already pending. Reboot or clear it before changing the persistent default; no boot setting was changed.")
    if not once and not re.search(r'^\s*set default=[\"\']?\$\{saved_entry\}[\"\']?\s*$', config, re.M):
        config = enable_saved_grub_default(env, log_fn, config_path)
    entry = grub_entry_title(version, env, config=config)

    # Confirm the selected list entry still exists and its menu actually loads
    # that image. A matching title alone can refer to a stale/custom entry.
    images = dict(parse_grub_entries(config, with_images=True)).get(entry, [])
    if len(images) != 1 or Path(images[0]).name != f"vmlinuz-{version}" or "$" in images[0]:
        raise RuntimeError("The selected GRUB entry does not unambiguously load this kernel. No boot setting was changed.")
    run_command(["sudo", "-A", command, f"--boot-directory={config_path.parent.parent}", entry], env, log_fn)
    saved = run_command(read_env, env, log_fn).stdout
    values = dict(line.split("=", 1) for line in saved.splitlines() if "=" in line)
    key = "next_entry" if once else "saved_entry"
    if values.get(key) != entry:
        raise RuntimeError("GRUB did not retain the requested boot selection. Check grubenv before rebooting.")
    log_fn(f"{'Next boot' if once else 'Default boot kernel'} set to {version} (verified).\n")


# ---------------------------------------------------------------------------
# Secure Boot / MOK signing helpers
# ---------------------------------------------------------------------------

def mok_key_exists() -> bool:
    return (MOK_DIR / "MOK.priv").exists() and (MOK_DIR / "MOK.der").exists()


def generate_mok_key(log_fn):
    MOK_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Generate both files privately before replacing an existing pair.
    with tempfile.TemporaryDirectory(dir=MOK_DIR) as tmp:
        priv, der = Path(tmp) / "MOK.priv", Path(tmp) / "MOK.der"
        run_command(["openssl", "req", "-new", "-x509", "-newkey", "rsa:2048",
                     "-keyout", priv, "-outform", "DER", "-out", der,
                     "-nodes", "-days", "36500", "-subj", "/CN=Kernel Manager GUI signing key/"], log_fn=log_fn)
        priv.chmod(0o600)
        # Retain a recoverable copy when replacing an enrolled key.
        for name in ("MOK.priv", "MOK.der"):
            existing = MOK_DIR / name
            if existing.exists():
                backup = existing.with_name(name + ".previous")
                shutil.copy2(existing, backup)
                if name.endswith("priv"):
                    backup.chmod(0o600)
        os.replace(priv, MOK_DIR / "MOK.priv")
        os.replace(der, MOK_DIR / "MOK.der")


def enroll_mok_key_in_terminal(log_fn):
    term = find_terminal_emulator()
    if not term or not shutil.which("mokutil"):
        raise RuntimeError("A terminal emulator and mokutil must be installed to enroll the key.")
    inner = ('sudo mokutil --import "$1"; status=$?; '
             'if [ "$status" -eq 0 ]; then echo "Reboot to finish MOK enrollment."; '
             'else echo "Enrollment request failed."; fi; '
             'read -r -p "Press Enter to close."; exit "$status"')
    args = ["bash", "-c", inner, "kernel-manager-mok", str(MOK_DIR / "MOK.der")]
    actual = Path(term).resolve().name
    if "gnome-terminal" in actual:
        cmd = [term, "--wait", "--", *args]
    elif "xfce4-terminal" in actual:
        cmd = [term, "--disable-server", "--command", shlex.join(args)]
    elif "konsole" in actual:
        cmd = [term, "--separate", "-e", *args]
    else:
        cmd = [term, "-e", *args]
    log_fn(f"Opening enrollment terminal: {shlex.join(cmd)}\n")
    proc = subprocess.run(cmd)
    if proc.returncode:
        raise RuntimeError("Enrollment terminal exited unsuccessfully; check the enrollment status.")


def build_sign_file_tool(kernel_src_dir: Path, log_fn) -> Path:
    """scripts/sign-file is a small host tool built from the kernel source
    tree; it may not exist yet if module signing wasn't enabled during the
    build, so compile just that one file on demand."""
    sign_file = kernel_src_dir / "scripts" / "sign-file"
    if sign_file.exists():
        return sign_file
    log_fn(f"Building scripts/sign-file in {kernel_src_dir}...\n")
    saved = kernel_src_dir / ".kernel-manager-make-args"
    args = [x for x in saved.read_text().split("\0") if x] if saved.exists() else []
    proc = subprocess.run(["make", *args, "scripts/sign-file"], cwd=str(kernel_src_dir),
                           capture_output=True, text=True)
    log_fn(proc.stdout + proc.stderr)
    if proc.returncode or not sign_file.exists():
        raise RuntimeError("Couldn't build scripts/sign-file — see log above.")
    return sign_file


def initramfs_command(version):
    if shutil.which("update-initramfs"):
        return ["update-initramfs", "-u", "-k", version]
    if shutil.which("dracut"):
        return ["dracut", "--force", f"/boot/initramfs-{version}.img", version]
    raise RuntimeError("No supported initramfs generator; sign and regenerate it manually.")


def replace_signed_file(source, destination, env, log_fn):
    """Stage beside the destination so the final replacement is atomic."""
    info = destination.stat()
    staged = run_command(["sudo", "-A", "mktemp", str(destination) + ".signed.XXXXXX"], env).stdout.strip()
    try:
        run_command(["sudo", "-A", "install", "-m", format(stat.S_IMODE(info.st_mode), "o"),
                     "-o", str(info.st_uid), "-g", str(info.st_gid), "--", source, staged], env, log_fn)
        run_command(["sudo", "-A", "mv", "-f", "--", staged, destination], env, log_fn)
    finally:
        run_command(["sudo", "-A", "rm", "-f", "--", staged], env)


def sign_kernel_and_modules(version: str, env: dict, log_fn):
    validate_release(version)
    if not mok_key_exists():
        raise RuntimeError("No MOK signing key yet — generate one first.")
    if package_manager_for_kernel(version) != "custom":
        raise RuntimeError("Only verified custom kernels can be signed here.")
    for tool in ("sbsign", "sbverify", "openssl", "mokutil", "depmod"):
        if not shutil.which(tool):
            raise RuntimeError(f"Required signing tool is not installed: {tool}")
    initrd_cmd = initramfs_command(version)
    priv, der = MOK_DIR / "MOK.priv", MOK_DIR / "MOK.der"
    run_command(["mokutil", "--test-key", der], env, log_fn)
    kernel_src_dir = find_source_tree(version)
    config = (kernel_src_dir / ".config").read_text()
    if "CONFIG_MODULE_SIG=y" not in config:
        raise RuntimeError("This kernel lacks module signature support. Rebuild with CONFIG_MODULE_SIG enabled.")
    vmlinuz = Path(f"/boot/vmlinuz-{version}")
    modules_dir = Path(f"/lib/modules/{version}")
    if not vmlinuz.is_file() or not modules_dir.is_dir():
        raise RuntimeError("Kernel image or modules directory is missing.")
    modules = sorted(p for p in modules_dir.rglob("*")
                     if any(p.name.endswith(ext) for ext in (".ko", ".ko.xz", ".ko.gz", ".ko.zst")))
    if not modules:
        raise RuntimeError("No modules found; nothing was signed.")
    if vmlinuz.is_symlink() or modules_dir.is_symlink() or any(p.is_symlink() for p in modules):
        raise RuntimeError("Unexpected symlink in kernel files; sign this installation manually.")
    compression = {".xz": ("xz", ["-C", "crc32"]), ".gz": ("gzip", ["-n"]), ".zst": ("zstd", ["-q"])}
    for module in modules:
        if module.suffix in compression and not shutil.which(compression[module.suffix][0]):
            raise RuntimeError(f"Missing compression tool for {module.name}")
    sign_file = build_sign_file_tool(kernel_src_dir, log_fn)
    with tempfile.TemporaryDirectory(prefix="kernel-sign-") as tmp:
        tmp = Path(tmp)
        pem, signed_image = tmp / "MOK.pem", tmp / "vmlinuz.signed"
        run_command(["openssl", "x509", "-inform", "DER", "-in", der, "-out", pem], env, log_fn)
        run_command(["sbsign", "--key", priv, "--cert", pem, "--output", signed_image, vmlinuz], env, log_fn)
        run_command(["sbverify", "--cert", pem, signed_image], env, log_fn)
        for module in modules:
            raw = tmp / "module.ko"
            packed = tmp / "module.packed"
            if module.suffix in compression:
                tool, flags = compression[module.suffix]
                with raw.open("wb") as output:
                    subprocess.run([tool, "-dc", str(module)], stdout=output, check=True)
            else:
                shutil.copyfile(module, raw)
            run_command([sign_file, "sha256", priv, der, raw], env, log_fn)
            staged = raw
            if module.suffix in compression:
                with packed.open("wb") as output:
                    subprocess.run([tool, *flags, "-c", str(raw)], stdout=output, check=True)
                staged = packed
            replace_signed_file(staged, module, env, log_fn)
        run_command(["sudo", "-A", "depmod", "-a", version], env, log_fn)
        run_command(["sudo", "-A", *initrd_cmd], env, log_fn)
        replace_signed_file(signed_image, vmlinuz, env, log_fn)
    log_fn("Signed kernel and modules; initramfs refreshed. Verify module-key trust on this kernel before relying on Secure Boot.\n")


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class KernelManagerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Kernel Manager")
        apply_theme(root)
        root.geometry("960x700")
        root.minsize(820, 600)

        self.build_proc = None
        self.build_thread = None
        self.log_queue = queue.Queue(maxsize=2000)
        self.ui_queue = queue.Queue()
        self.busy = None
        self.operation_lock = None
        self.closed = False
        self.reads_pending = set()
        self.kernels = []
        self.build_cancellable = False
        self.cancel_thread = None
        self.built_kernel_dir = None  # set once a build finishes successfully
        self._prev_cpu_times = None
        self.presets = load_presets()

        nb = ttk.Notebook(root)
        nb.pack(fill="both", expand=True, padx=12, pady=12)
        nb.enable_traversal()

        self.installed_tab = ttk.Frame(nb, padding=12)
        self.build_tab = ttk.Frame(nb, padding=12)
        self.logs_tab = ttk.Frame(nb, padding=12)
        self.maintenance_tab = ttk.Frame(nb, padding=12)
        self.sysinfo_tab = ttk.Frame(nb, padding=12)
        self.mok_tab = ttk.Frame(nb, padding=12)
        nb.add(self.installed_tab, text="Installed Kernels")
        nb.add(self.build_tab, text="Build New Kernel")
        nb.add(self.logs_tab, text="Build Logs")
        nb.add(self.maintenance_tab, text="Maintenance")
        nb.add(self.sysinfo_tab, text="System Info")
        nb.add(self.mok_tab, text="Secure Boot (MOK)")

        self._build_installed_tab()
        self._build_build_tab()
        self._build_logs_tab()
        self._build_maintenance_tab()
        self._build_sysinfo_tab()
        self._build_mok_tab()

        self.refresh_installed()
        self._restore_completed_build()
        self.root.after(100, self._poll_log_queue)
        self.root.after(1000, self._update_resource_monitor)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _dispatch(self, callback):
        if not self.closed:
            self.ui_queue.put(callback)

    def _read_async(self, key, work, done):
        if key in self.reads_pending:
            return
        self.reads_pending.add(key)

        def worker():
            try:
                result = work()
                self._dispatch(lambda: done(result))
            except Exception as e:
                self._dispatch(lambda error=str(e): messagebox.showerror(key, error))
            finally:
                self._dispatch(lambda: self.reads_pending.discard(key))
        threading.Thread(target=worker, daemon=True).start()

    def _begin_operation(self, name):
        if self.busy:
            messagebox.showinfo("Operation in progress", f"Wait for {self.busy} to finish.")
            return False
        handle = None
        try:
            KERNEL_BUILD_DIR.mkdir(parents=True, exist_ok=True)
            handle = (KERNEL_BUILD_DIR / ".operation.lock").open("a")
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if handle:
                handle.close()
            messagebox.showerror("Operation unavailable", f"Another kernel operation may be running: {e}")
            return False
        self.operation_lock = handle
        self.busy = name
        self.start_btn.configure(state="disabled")
        self.install_btn.configure(state="disabled")
        return True

    def _finish_operation(self):
        if self.operation_lock:
            self.operation_lock.close()
            self.operation_lock = None
        self.busy = None
        self.build_proc = None
        self.build_cancellable = False
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        ready = self.built_kernel_dir and (Path(self.built_kernel_dir) / ".kernel-manager-complete").is_file()
        self.install_btn.configure(state="normal" if ready else "disabled")
        self.refresh_installed()
        self.refresh_maintenance_tab()
        self.refresh_logs_tab()

    def _run_operation(self, title, work):
        if not self._begin_operation(title):
            return
        log_win = LogWindow(self.root, title)

        def worker():
            try:
                work(log_win.append)
                log_win.append("\nDone.\n")
                self._dispatch(lambda: log_win.finish(True))
            except Exception as e:
                log_win.append(f"\n[error] {e}\n")
                self._dispatch(lambda: log_win.finish(False))
            finally:
                self._dispatch(self._finish_operation)
        threading.Thread(target=worker, daemon=True).start()

    def _restore_completed_build(self):
        def done(trees):
            if not self.busy and not self.built_kernel_dir and trees:
                self.built_kernel_dir = str(trees[0])
                self.install_btn.configure(state="normal")
                self._append_log(f"Completed build available: {trees[0]}\n")
        self._read_async("Completed builds", completed_build_dirs, done)

    # ---------------- Installed Kernels tab ----------------

    def _build_installed_tab(self):
        frame = self.installed_tab

        toolbar = ttk.Frame(frame)
        toolbar.pack(fill="x", pady=(0, 6))
        ttk.Button(toolbar, text="Refresh", command=self.refresh_installed).pack(side="left")
        ttk.Button(toolbar, text="Check for kernel.org updates",
                   command=self.check_for_updates).pack(side="left", padx=6)
        ttk.Button(toolbar, text="Delete Selected", command=self.on_delete_selected).pack(side="left")

        toolbar2 = ttk.Frame(frame)
        toolbar2.pack(fill="x", pady=(0, 6))
        ttk.Button(toolbar2, text="Set as Default Boot",
                   command=lambda: self.on_set_boot(once=False)).pack(side="left")
        ttk.Button(toolbar2, text="Boot Once (test)",
                   command=lambda: self.on_set_boot(once=True)).pack(side="left", padx=6)
        ttk.Button(toolbar2, text="Show actual GRUB entries",
                   command=self.on_show_grub_entries).pack(side="left")

        self.update_label = ttk.Label(frame, text="", style="Status.TLabel")
        self.update_label.pack(fill="x", pady=(0, 6))

        columns = ("version", "type", "running", "size")
        tree_frame, self.tree = scrolled_tree(frame, columns=columns, show="headings", height=14)
        for col, label, width in [
            ("version", "Version", 220),
            ("type", "Source", 140),
            ("running", "Running", 90),
            ("size", "Modules size", 110),
        ]:
            self.tree.heading(col, text=label)
            self.tree.column(col, width=width, anchor="w")
        tree_frame.pack(fill="both", expand=True)

    def refresh_installed(self):
        def done(kernels):
            self.kernels = kernels
            for row in self.tree.get_children():
                self.tree.delete(row)
            for k in kernels:
                source = "custom-built" if k.manager == "custom" else f"{k.manager}-managed"
                self.tree.insert("", "end", iid=k.version, values=(
                    k.version, source, "✓ running" if k.running else "", k.size_str))
            self.refresh_mok_tab()
        self._read_async("Installed kernels", list_installed_kernels, done)

    def check_for_updates(self):
        self.update_label.config(text="Checking kernel.org for the latest stable release…")

        def worker():
            latest = latest_stable_version()
            current = running_kernel().split("-")[0]
            if latest is None:
                return "Couldn't reach kernel.org to check for updates."
            def version_tuple(value):
                parts = tuple(map(int, value.split(".")))
                return parts + (0,) * (3 - len(parts))
            if not VER_RE.fullmatch(current):
                return f"Latest stable: {latest}; running release: {current}."
            if version_tuple(latest) > version_tuple(current):
                return f"Newer stable release: {latest} (running {current})."
            return f"No newer stable release (latest {latest}, running {current})."
        self._read_async("Update check", worker, lambda text: self.update_label.config(text=text))

    def on_delete_selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Delete kernel", "Select a kernel first.")
            return
        version = sel[0]
        kernels = {k.version: k for k in self.kernels}
        kernel = kernels.get(version)
        if kernel is None:
            return
        if kernel.manager not in ("apt", "custom"):
            messagebox.showerror("Delete kernel", "Use the distribution package manager for this kernel.")
            return
        if kernel.running:
            messagebox.showerror("Delete kernel", "You can't delete the kernel that's currently running.")
            return

        if not messagebox.askyesno(
            "Confirm delete",
            f"Delete kernel {version}?\n\n"
            f"Source: {'apt-managed (will be purged via apt)' if kernel.apt_managed else 'custom-built (files removed manually)'}\n"
            "This cannot be undone."
        ):
            return

        self._run_operation(f"Deleting {version}", lambda log: delete_kernel(kernel, gui_env(), log))

    def on_set_boot(self, once: bool):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Boot selection", "Select a kernel first.")
            return
        version = sel[0]
        action = "next boot only" if once else "the default boot"
        if messagebox.askyesno("Confirm boot selection", f"Select {version} for {action}?"):
            self._run_operation(f"Setting boot kernel: {version}",
                                lambda log: set_default_boot(version, gui_env(), log, once))

    def on_show_grub_entries(self):
        def done(result):
            titles, error = result
            win = tk.Toplevel(self.root)
            win.title("Actual GRUB menu entries")
            win.geometry("560x300")
            ttk.Label(win, text="Menu paths found in grub.cfg:").pack(anchor="w", padx=8, pady=8)
            win.transient(self.root)
            panel, entries = scrolled_tree(win, columns=("entry",), show="headings", selectmode="browse")
            entries.heading("entry", text="Boot menu entry")
            entries.column("entry", width=620, minwidth=300, stretch=True)
            panel.pack(fill="both", expand=True, padx=12, pady=(0, 12))
            for title in titles or [error or "No entries found."]:
                entries.insert("", "end", values=(title,))
        self._read_async("GRUB entries", lambda: grub_menu_titles(gui_env()), done)

    # ---------------- Build tab ----------------

    def _build_build_tab(self):
        frame = self.build_tab

        opts = ttk.LabelFrame(frame, text="Toolchain options")
        opts.pack(fill="x", padx=4, pady=4)

        self.toolchain_var = tk.StringVar(value=("clang" if self.presets.get("toolchain") == "clang" else "gcc"))
        ttk.Radiobutton(opts, text="GCC", variable=self.toolchain_var, value="gcc",
                        command=self._sync_lto_state).grid(row=0, column=0, sticky="w", padx=6, pady=4)
        ttk.Radiobutton(opts, text="Clang + LLD", variable=self.toolchain_var, value="clang",
                        command=self._sync_lto_state).grid(row=0, column=1, sticky="w", padx=6, pady=4)

        self.lto_var = tk.BooleanVar(value=self.presets.get("lto") is True)
        self.lto_check = ttk.Checkbutton(opts, text="Enable ThinLTO (Clang only, slow/RAM-heavy)",
                                          variable=self.lto_var,
                                          state="normal" if self.toolchain_var.get() == "clang" else "disabled")
        self.lto_check.grid(row=1, column=0, columnspan=2, sticky="w", padx=6, pady=2)
        self._sync_lto_state()

        self.debug_var = tk.BooleanVar(value=self.presets.get("debug") is True)
        ttk.Checkbutton(opts, text="Keep full debug info (needs 8GB+ RAM)",
                         variable=self.debug_var).grid(row=2, column=0, columnspan=2, sticky="w", padx=6, pady=2)

        self.force_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Force rebuild even if already up to date",
                         variable=self.force_var).grid(row=3, column=0, columnspan=2, sticky="w", padx=6, pady=2)

        jobs_frame = ttk.Frame(opts)
        jobs_frame.grid(row=4, column=0, columnspan=2, sticky="w", padx=6, pady=4)
        ttk.Label(jobs_frame, text="Parallel jobs:").pack(side="left")
        self.jobs_var = tk.StringVar(value=str(self.presets.get("jobs", os.cpu_count() or 2)))
        ttk.Spinbox(jobs_frame, from_=1, to=64, width=5, textvariable=self.jobs_var).pack(side="left", padx=4)

        # Live resource monitor — updated every second regardless of whether
        # a build is running, so you can see memory pressure building before
        # it turns into an OOM kill.
        monitor = ttk.LabelFrame(frame, text="System resources (live)")
        monitor.pack(fill="x", padx=4, pady=(0, 4))
        self.resource_label = ttk.Label(monitor, text="Reading...", font="TkFixedFont")
        self.resource_label.pack(anchor="w", padx=8, pady=6)

        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill="x", pady=6)
        self.start_btn = ttk.Button(btn_frame, text="Start Build", command=self.start_build, style="Accent.TButton")
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(btn_frame, text="Stop", command=self.stop_build, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        self.install_btn = ttk.Button(btn_frame, text="Install Now", command=self.start_install, state="disabled", style="Accent.TButton")
        self.install_btn.pack(side="left", padx=6)

        ttk.Label(frame, text="Live build output:").pack(anchor="w", padx=4)
        log_frame, self.log_text = scrolled_text(frame, wrap="none", height=20)
        log_frame.pack(fill="both", expand=True, padx=4, pady=(0, 4))

    def _sync_lto_state(self):
        if self.toolchain_var.get() == "clang":
            self.lto_check.configure(state="normal")
        else:
            self.lto_var.set(False)
            self.lto_check.configure(state="disabled")

    def _append_log(self, text: str):
        append_bounded(self.log_text, text)

    def _update_resource_monitor(self):
        try:
            idle, total = read_cpu_times()
            if self._prev_cpu_times is not None:
                prev_idle, prev_total = self._prev_cpu_times
                delta_idle = idle - prev_idle
                delta_total = total - prev_total
                cpu_pct = 100.0 * (1 - delta_idle / delta_total) if delta_total > 0 else 0.0
            else:
                cpu_pct = 0.0
            self._prev_cpu_times = (idle, total)

            mem = mem_swap_info()
            mem_total_gb = mem.get("MemTotal", 0) / 1024 / 1024
            mem_avail_gb = mem.get("MemAvailable", 0) / 1024 / 1024
            mem_used_gb = mem_total_gb - mem_avail_gb
            swap_total_gb = mem.get("SwapTotal", 0) / 1024 / 1024
            swap_free_gb = mem.get("SwapFree", 0) / 1024 / 1024
            swap_used_gb = swap_total_gb - swap_free_gb

            text = (f"CPU: {cpu_pct:5.1f}%   "
                    f"RAM: {mem_used_gb:.1f}/{mem_total_gb:.1f} GB   "
                    f"Swap: {swap_used_gb:.1f}/{swap_total_gb:.1f} GB")
            self.resource_label.configure(text=text)
        except Exception:
            pass  # monitor is best-effort, never worth crashing the app over
        self.root.after(2000, self._update_resource_monitor)

    def _on_close(self):
        if self.busy:
            messagebox.showinfo("Operation in progress", "Wait for the active operation to finish. For a build, use Stop when available first.")
            return
        self.presets = {
            "toolchain": self.toolchain_var.get(),
            "lto": self.lto_var.get(),
            "debug": self.debug_var.get(),
            "jobs": self.jobs_var.get(),
        }
        save_presets(self.presets)
        self.closed = True
        self.root.destroy()

    def start_build(self):
        jobs = self.jobs_var.get()
        if not re.fullmatch(r"[1-9][0-9]*", jobs):
            messagebox.showerror("Build", "Parallel jobs must be a positive integer.")
            return
        cmd = ["bash", str(BUILD_SCRIPT), "--jobs", jobs]
        if self.toolchain_var.get() == "clang":
            cmd.append("--clang")
            if self.lto_var.get():
                cmd.append("--lto")
        if self.debug_var.get():
            cmd.append("--full-debug-info")
        if self.force_var.get():
            cmd.append("--force")
        self._start_stream(cmd, SCRIPT_DIR, "build")

    def _start_stream(self, cmd, cwd, kind):
        if not Path(cmd[1]).is_file():
            messagebox.showerror(kind, f"Missing script: {cmd[1]}")
            return
        try:
            env = gui_env()
        except Exception as e:
            messagebox.showerror(kind, str(e))
            return
        if not self._begin_operation(kind):
            return
        if kind == "build":
            self.built_kernel_dir = None
            self.log_text.delete("1.0", "end")
            save_presets({"toolchain": self.toolchain_var.get(), "lto": self.lto_var.get(),
                          "debug": self.debug_var.get(), "jobs": self.jobs_var.get()})
        self._append_log(f"$ {shlex.join(cmd)} (in {cwd})\n")
        fd = self.operation_lock.fileno()
        env["KERNEL_MANAGER_LOCK_FD"] = str(fd)

        def worker():
            rc, built_dir = 1, None
            proc = None
            try:
                proc = subprocess.Popen(cmd, cwd=str(cwd), env=env,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, errors="replace", bufsize=1,
                                        start_new_session=True, pass_fds=(fd,))
                self.build_proc = proc
                with proc.stdout:
                    for line in proc.stdout:
                        self.log_queue.put(line[:65536])
                        if kind == "build" and line.strip() == "KERNEL_MANAGER_CANCELLABLE=1":
                            self._dispatch(self._allow_build_stop)
                        if kind == "build" and line.startswith("KERNEL_MANAGER_BUILD_DIR="):
                            candidate = Path(line.rstrip("\n").split("=", 1)[1])
                            if candidate.parent.resolve() == KERNEL_BUILD_DIR.resolve() and (candidate / ".kernel-manager-complete").is_file():
                                built_dir = str(candidate)
                rc = proc.wait()
            except Exception as e:
                self.log_queue.put(f"\n[error] {e}\n")
                if proc is not None:
                    if kind == "build" and self.build_cancellable:
                        terminate_build_group(proc)
                    else:
                        # Do not interrupt an installation or package transaction.
                        proc.wait()
            finally:
                if self.cancel_thread is not None:
                    self.cancel_thread.join()
                    self.cancel_thread = None
                    rc = -signal.SIGTERM
                self.log_queue.put(f"\n[{kind} finished with exit code {rc}]\n")
                self._dispatch(lambda: self._stream_finished(kind, rc, built_dir))
                send_notification("Kernel Manager", f"{kind.capitalize()} {'finished' if rc == 0 else 'failed'}. Check the log.")
        self.build_thread = threading.Thread(target=worker, daemon=True)
        self.build_thread.start()

    def _allow_build_stop(self):
        if self.busy == "build":
            self.build_cancellable = True
            self.stop_btn.configure(state="normal")

    def _stream_finished(self, kind, rc, built_dir):
        if self.cancel_thread is not None:
            if self.cancel_thread.is_alive():
                self.root.after(100, lambda: self._stream_finished(kind, rc, built_dir))
                return
            self.cancel_thread = None
            rc = -signal.SIGTERM
        if kind == "build":
            self.built_kernel_dir = built_dir if rc == 0 else None
        elif rc == 0:
            self.built_kernel_dir = None
            self._append_log("Installed. Verify the boot menu, signatures, and fallback before rebooting.\n")
        self._finish_operation()

    def stop_build(self):
        if self.busy == "build" and self.build_cancellable and self.build_proc:
            self._append_log("\n[stopping build and its child processes...]\n")
            self.stop_btn.configure(state="disabled")
            self.build_cancellable = False
            proc = self.build_proc
            thread = threading.Thread(target=terminate_build_group, args=(proc,), daemon=True)
            thread.start()
            self.cancel_thread = thread

    def start_install(self):
        if not self.built_kernel_dir:
            messagebox.showerror("Install", "No completed kernel build is available.")
            return
        # Always use the current installer, never a stale source-tree copy.
        self._start_stream(["bash", str(INSTALL_SCRIPT)], Path(self.built_kernel_dir), "install")

    def _poll_log_queue(self):
        try:
            for _ in range(200):
                self._append_log(self.log_queue.get_nowait())
        except queue.Empty:
            pass
        try:
            for _ in range(50):
                callback = self.ui_queue.get_nowait()
                callback()
        except queue.Empty:
            pass
        if not self.closed:
            self.root.after(100, self._poll_log_queue)

    # ---------------- Build Logs tab ----------------

    def _build_logs_tab(self):
        frame = self.logs_tab
        toolbar = ttk.Frame(frame)
        toolbar.pack(fill="x", pady=(4, 6), padx=4)
        ttk.Button(toolbar, text="Refresh", command=self.refresh_logs_tab).pack(side="left")
        ttk.Button(toolbar, text="View Selected", command=self.on_view_log).pack(side="left", padx=6)
        ttk.Button(toolbar, text="Delete Selected", command=self.on_delete_log).pack(side="left")

        columns = ("name", "date", "size")
        tree_frame, self.logs_tree = scrolled_tree(frame, columns=columns, show="headings", height=16)
        for col, label, width in [("name", "Log file", 320), ("date", "Date", 180), ("size", "Size", 100)]:
            self.logs_tree.heading(col, text=label)
            self.logs_tree.column(col, width=width, anchor="w")
        tree_frame.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        self.refresh_logs_tab()

    def refresh_logs_tab(self):
        for row in self.logs_tree.get_children():
            self.logs_tree.delete(row)
        for log in build_log_files():
            stat = log.stat()
            date = time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime))
            size = f"{stat.st_size / 1024 / 1024:.1f} MB" if stat.st_size > 1024*1024 else f"{stat.st_size // 1024} KB"
            self.logs_tree.insert("", "end", iid=str(log), values=(log.name, date, size))

    def on_view_log(self):
        sel = self.logs_tree.selection()
        if not sel:
            messagebox.showinfo("Build logs", "Select a log file first.")
            return
        path = Path(sel[0])
        win = tk.Toplevel(self.root)
        win.title(path.name)
        win.geometry("800x600")
        win.transient(self.root)
        panel, text = scrolled_text(win, wrap="none")
        panel.pack(fill="both", expand=True, padx=12, pady=12)
        try:
            with path.open("rb") as source:
                source.seek(0, os.SEEK_END)
                size = source.tell()
                source.seek(max(0, size - 1024 * 1024))
                content = source.read().decode(errors="replace")
            text.insert("end", ("[Showing the last 1 MiB of this log]\n" if size > 1024 * 1024 else "") + content)
        except Exception as e:
            text.insert("end", f"Couldn't read log: {e}")
        text.configure(state="disabled")

    def on_delete_log(self):
        if self.busy:
            messagebox.showinfo("Build logs", "Wait for the active operation before deleting logs.")
            return
        sel = self.logs_tree.selection()
        if not sel:
            messagebox.showinfo("Build logs", "Select a log file first.")
            return
        if not messagebox.askyesno("Delete log", f"Delete {len(sel)} log file(s)?"):
            return
        for s in sel:
            try:
                Path(s).unlink()
            except OSError as e:
                messagebox.showerror("Delete log", str(e))
        self.refresh_logs_tab()

    # ---------------- Maintenance tab ----------------

    def _build_maintenance_tab(self):
        frame = self.maintenance_tab

        info = ttk.Frame(frame)
        info.pack(fill="x", padx=4, pady=6)
        self.disk_usage_label = ttk.Label(info, text="", style="Status.TLabel")
        self.disk_usage_label.pack(anchor="w")

        toolbar = ttk.Frame(frame)
        toolbar.pack(fill="x", padx=4, pady=(0, 6))
        ttk.Button(toolbar, text="Refresh", command=self.refresh_maintenance_tab).pack(side="left")
        ttk.Button(toolbar, text="Clean Selected Source Trees",
                   command=self.on_clean_source_dirs).pack(side="left", padx=6)

        ttk.Label(frame, text="Extracted kernel source trees under ~/kernel-build "
                               "(these are what eats disk space across repeated builds — "
                               "the downloaded .tar.xz files are left alone):"
                  ).pack(anchor="w", padx=4)

        columns = ("dir", "size")
        tree_frame, self.maint_tree = scrolled_tree(frame, columns=columns, show="headings", height=14)
        self.maint_tree.heading("dir", text="Directory")
        self.maint_tree.heading("size", text="Size")
        self.maint_tree.column("dir", width=500, anchor="w")
        self.maint_tree.column("size", width=100, anchor="w")
        tree_frame.pack(fill="both", expand=True, padx=4, pady=(0, 4))

        self.refresh_maintenance_tab()

    def refresh_maintenance_tab(self):
        def work():
            total = "0"
            if KERNEL_BUILD_DIR.exists():
                total = subprocess.check_output(["du", "-sh", str(KERNEL_BUILD_DIR)], text=True).split()[0]
            return total, running_kernel().split("-")[0], kernel_build_source_dirs()

        def done(result):
            total, current, dirs = result
            for row in self.maint_tree.get_children():
                self.maint_tree.delete(row)
            self.disk_usage_label.configure(text=f"Total size of {KERNEL_BUILD_DIR}: {total}")
            for path, size in dirs:
                note = "  (matches running kernel)" if path.name == f"linux-{current}" else ""
                self.maint_tree.insert("", "end", iid=str(path), values=(path.name + note, size))
        self._read_async("Source tree sizes", work, done)

    def on_clean_source_dirs(self):
        sel = self.maint_tree.selection()
        if not sel:
            messagebox.showinfo("Maintenance", "Select one or more source trees first.")
            return
        current_src = f"linux-{running_kernel().split('-')[0]}"
        warn = ""
        if any(Path(s).name == current_src for s in sel):
            warn = ("\n\nWarning: this includes the source tree matching your "
                    "currently running kernel — you won't be able to (re)sign "
                    "its modules via the MOK tab without it.")
        if not messagebox.askyesno(
            "Confirm cleanup",
            f"Permanently delete {len(sel)} extracted source tree(s)?{warn}"
        ):
            return
        def work(log):
            for selected in sel:
                path = Path(selected)
                if path.is_symlink() or path.parent.resolve() != KERNEL_BUILD_DIR.resolve() or not path.name.startswith("linux-"):
                    raise RuntimeError(f"Refusing unexpected source path: {path}")
                shutil.rmtree(path)
                log(f"Removed {path}\n")
        self._run_operation("Cleaning source trees", work)

    # ---------------- System Info tab ----------------

    def _build_sysinfo_tab(self):
        frame = self.sysinfo_tab
        ttk.Button(frame, text="Refresh", command=self.refresh_sysinfo_tab).pack(anchor="w", padx=4, pady=6)
        panel, self.sysinfo_text = scrolled_text(frame, wrap="word", height=20)
        panel.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        self.sysinfo_text.configure(state="disabled")
        self.refresh_sysinfo_tab()

    def refresh_sysinfo_tab(self):
        def work():
            cpu = cpu_info()
            mem = mem_swap_info()
            mem_gb = mem.get("MemTotal", 0) / 1024 / 1024
            swap_gb = mem.get("SwapTotal", 0) / 1024 / 1024
            try:
                cmdline = Path("/proc/cmdline").read_text().strip()
            except Exception:
                cmdline = "unknown"
            try:
                uptime_s = float(Path("/proc/uptime").read_text().split()[0])
                uptime_str = f"{int(uptime_s // 3600)}h {int((uptime_s % 3600) // 60)}m"
            except Exception:
                uptime_str = "unknown"

            lines = [
                f"Running kernel:     {running_kernel()}",
                f"CPU:                {cpu['model']}",
                f"CPU cores:          {cpu['cores']}",
                f"Total RAM:          {mem_gb:.1f} GB",
                f"Total swap:         {swap_gb:.1f} GB",
                f"Secure Boot:        {secure_boot_state()}",
                f"Uptime:             {uptime_str}",
                "",
                "Kernel command line:",
                cmdline,
            ]
            return "\n".join(lines)

        def done(text):
            self.sysinfo_text.configure(state="normal")
            self.sysinfo_text.delete("1.0", "end")
            self.sysinfo_text.insert("end", text)
            self.sysinfo_text.configure(state="disabled")
        self._read_async("System information", work, done)

    # ---------------- Secure Boot (MOK) tab ----------------

    def _build_mok_tab(self):
        frame = self.mok_tab

        ttk.Label(frame, text=
            "Secure Boot requires a signed kernel, signed modules, and a trusted enrolled key.\n"
            "Steps: 1) generate a signing key once  2) enroll it with the firmware "
            "(needs a reboot to confirm)  3) sign a kernel's files after each build.",
            wraplength=780, justify="left"
        ).pack(anchor="w", padx=8, pady=8)

        step1 = ttk.LabelFrame(frame, text="1. Signing key")
        step1.pack(fill="x", padx=8, pady=4)
        self.mok_key_label = ttk.Label(step1, text="")
        self.mok_key_label.pack(anchor="w", padx=6, pady=4)
        ttk.Button(step1, text="Generate Key", command=self.on_generate_mok_key).pack(anchor="w", padx=6, pady=(0, 6))

        step2 = ttk.LabelFrame(frame, text="2. Enroll key with firmware (opens a terminal)")
        step2.pack(fill="x", padx=8, pady=4)
        ttk.Label(step2, text="You'll set a one-time password in the terminal, then confirm "
                               "enrollment on the blue MOK Manager screen after rebooting.",
                  wraplength=780, justify="left").pack(anchor="w", padx=6, pady=4)
        ttk.Button(step2, text="Enroll Key", command=self.on_enroll_mok_key).pack(anchor="w", padx=6, pady=(0, 6))

        step3 = ttk.LabelFrame(frame, text="3. Sign a kernel's vmlinuz + modules")
        step3.pack(fill="x", padx=8, pady=4)
        sign_frame = ttk.Frame(step3)
        sign_frame.pack(fill="x", padx=6, pady=6)
        ttk.Label(sign_frame, text="Kernel version:").pack(side="left")
        self.mok_version_var = tk.StringVar()
        self.mok_version_combo = ttk.Combobox(sign_frame, textvariable=self.mok_version_var, width=30, state="readonly")
        self.mok_version_combo.pack(side="left", padx=6)
        ttk.Button(sign_frame, text="Sign", command=self.on_sign_kernel).pack(side="left", padx=6)

        self.refresh_mok_tab()

    def refresh_mok_tab(self):
        self.mok_key_label.configure(
            text="Key found." if mok_key_exists() else "No key yet — click Generate Key."
        )
        versions = [k.version for k in self.kernels if k.manager == "custom"]
        self.mok_version_combo.configure(values=versions)
        if self.mok_version_var.get() not in versions:
            self.mok_version_var.set(versions[0] if versions else "")

    def on_generate_mok_key(self):
        if mok_key_exists() and not messagebox.askyesno(
            "Generate key", "A key already exists — generate a new one and replace it?"
        ):
            return
        self._run_operation("Generating MOK signing key", generate_mok_key)

    def on_enroll_mok_key(self):
        if not mok_key_exists():
            messagebox.showerror("Enroll key", "Generate a key first.")
            return
        self._run_operation("Enrolling MOK key", enroll_mok_key_in_terminal)

    def on_sign_kernel(self):
        version = self.mok_version_var.get()
        if not version:
            messagebox.showinfo("Sign kernel", "Select a kernel version first.")
            return
        self._run_operation(f"Signing {version}", lambda log: sign_kernel_and_modules(version, gui_env(), log))


def terminate_build_group(proc):
    """Only used after the privileged dependency phase has completed."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    # Give compiler children time to exit, then reap anything still in the group.
    threading.Event().wait(3)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()


def append_bounded(widget, text):
    limit = 1024 * 1024
    widget.insert("end", text[-limit:])
    length = widget.count("1.0", "end", "chars")[0]
    if length > limit:
        widget.delete("1.0", f"1.0+{length - limit}c")
    widget.see("end")


class LogWindow:
    """Small popup with a live-updating text log, used for delete operations."""
    def __init__(self, parent, title):
        self.win = tk.Toplevel(parent)
        self.win.title(title)
        self.win.geometry("680x380")
        self.win.transient(parent)
        self.status_label = ttk.Label(self.win, text="Working…", style="Status.TLabel")
        self.status_label.pack(side="bottom", fill="x", padx=12, pady=(0, 6))
        panel, self.text = scrolled_text(self.win, wrap="none")
        panel.pack(fill="both", expand=True, padx=12, pady=(12, 6))
        self.queue = queue.Queue(maxsize=2000)
        self.closed = False
        self.win.protocol("WM_DELETE_WINDOW", self._close)
        self.win.after(100, self._poll)

    def finish(self, success):
        if not self.closed:
            self.status_label.configure(text="Completed" if success else "Failed — see details above")

    def _close(self):
        self.closed = True
        self.win.destroy()

    def append(self, s: str):
        # A closed log window must not block or abort the underlying operation.
        if not self.closed:
            try:
                self.queue.put_nowait(s[-65536:])
            except queue.Full:
                pass

    def _poll(self):
        try:
            for _ in range(200):
                append_bounded(self.text, self.queue.get_nowait())
        except queue.Empty:
            pass
        if not self.closed:
            self.win.after(100, self._poll)


def main():
    if not BUILD_SCRIPT.exists() or not INSTALL_SCRIPT.exists():
        print(f"Expected build-custom-kernel.sh and install-custom-kernel.sh "
              f"next to this script in {SCRIPT_DIR}", file=sys.stderr)
        sys.exit(1)
    try:
        gui_env()
        root = tk.Tk()
    except (RuntimeError, tk.TclError) as e:
        print(f"Cannot start Kernel Manager: {e}", file=sys.stderr)
        sys.exit(1)
    app = KernelManagerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
