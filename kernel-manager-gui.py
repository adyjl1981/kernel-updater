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
  3. Checks kernel.org for a newer stable version than what's installed.
  4. Lets you pick a toolchain (GCC / Clang, +LTO, +full debug info) and
     kick off a build.
  5. Streams the build's live output into the window.
  6. Offers an "Install Now" button once a build finishes successfully.

Expects build-custom-kernel.sh, install-custom-kernel.sh, and
askpass-gui.py to live in the same directory as this script.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import queue
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BUILD_SCRIPT = SCRIPT_DIR / "build-custom-kernel.sh"
INSTALL_SCRIPT = SCRIPT_DIR / "install-custom-kernel.sh"
ASKPASS_SCRIPT = SCRIPT_DIR / "askpass-gui.py"
KERNEL_BUILD_DIR = Path.home() / "kernel-build"
MOK_DIR = SCRIPT_DIR / "mok"
CONFIG_DIR = Path.home() / ".config" / "kernel-manager-gui"
CONFIG_FILE = CONFIG_DIR / "config.json"

VER_RE = re.compile(r"^\d+\.\d+(\.\d+)?$")


def running_kernel() -> str:
    return subprocess.check_output(["uname", "-r"], text=True).strip()


def gui_env() -> dict:
    """Environment for subprocesses launched by this GUI: points sudo at
    our askpass helper so password prompts show up as a dialog instead of
    failing (there's no terminal attached to a GUI-launched process)."""
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
        return json.loads(CONFIG_FILE.read_text())
    except Exception:
        return {}


def save_presets(d: dict):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(d, indent=2))
    except Exception:
        pass  # presets are a convenience, not worth failing over


def find_terminal_emulator():
    for candidate in ("x-terminal-emulator", "gnome-terminal", "konsole",
                       "xfce4-terminal", "xterm"):
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
    total = sum(values)
    return idle, total


def grub_menu_titles(env: dict = None):
    """Best-effort extraction of top-level menuentry titles from grub.cfg,
    purely informational (helps verify the naming convention this app
    assumes when setting a default/one-time boot kernel).

    Returns (titles, error). grub.cfg is sometimes locked to root-only
    read access, so this falls back to reading it via sudo (through the
    same askpass mechanism used elsewhere in the app) if a plain read
    fails."""
    for cfg in ("/boot/grub/grub.cfg", "/boot/grub2/grub.cfg"):
        p = Path(cfg)
        if not p.exists():
            continue

        text = None
        try:
            text = p.read_text(errors="ignore")
        except PermissionError:
            try:
                proc = subprocess.run(
                    ["sudo", "-A", "cat", cfg],
                    env=env or gui_env(), capture_output=True, text=True, timeout=15
                )
                if proc.returncode == 0:
                    text = proc.stdout
                else:
                    return [], f"Permission denied reading {cfg}, and sudo cat also failed:\n{proc.stderr}"
            except Exception as e:
                return [], f"Permission denied reading {cfg}, and the sudo fallback failed: {e}"
        except Exception as e:
            return [], f"Error reading {cfg}: {e}"

        if text is not None:
            titles = re.findall(r"menuentry\s+['\"]([^'\"]+)['\"]", text)
            if not titles:
                return [], f"{cfg} was read successfully but no menuentry lines matched — its format may differ from what this app expects."
            return titles, None

    return [], "Couldn't find grub.cfg at /boot/grub/grub.cfg or /boot/grub2/grub.cfg."


def kernel_build_source_dirs():
    """Extracted linux-<version> source trees under ~/kernel-build, which
    is where most of the disk space from repeated builds accumulates."""
    dirs = []
    if not KERNEL_BUILD_DIR.exists():
        return dirs
    for entry in sorted(KERNEL_BUILD_DIR.iterdir()):
        if entry.is_dir() and entry.name.startswith("linux-"):
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
    def __init__(self, version, apt_managed, running, size_str):
        self.version = version
        self.apt_managed = apt_managed
        self.running = running
        self.size_str = size_str


def list_installed_kernels():
    """Enumerate installed kernels from /lib/modules, cross-checked against
    dpkg to tell apt-managed kernels apart from custom/manually-built ones."""
    kernels = []
    modules_root = Path("/lib/modules")
    if not modules_root.exists():
        return kernels

    current = running_kernel()

    for entry in sorted(modules_root.iterdir()):
        if not entry.is_dir():
            continue
        version = entry.name

        # Is this kernel owned by a dpkg package (apt-installed) or not
        # (built by our script)?
        apt_managed = False
        try:
            result = subprocess.run(
                ["dpkg", "-S", f"/boot/vmlinuz-{version}"],
                capture_output=True, text=True
            )
            apt_managed = result.returncode == 0
        except FileNotFoundError:
            pass  # dpkg not present (non-Debian system) — treat as custom

        # Rough size on disk for the modules directory
        size_str = "?"
        try:
            out = subprocess.check_output(["du", "-sh", str(entry)], text=True)
            size_str = out.split()[0]
        except Exception:
            pass

        kernels.append(KernelInfo(version, apt_managed, version == current, size_str))

    return kernels


def latest_stable_version() -> str | None:
    """Same git-ls-remote approach used by build-custom-kernel.sh, kept
    independent here so the GUI doesn't need to invoke the build script
    just to check for updates."""
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


def delete_kernel(kernel: KernelInfo, env: dict, log_fn):
    """Remove a kernel. apt-managed kernels are purged via apt (so dpkg's
    database stays consistent); custom-built kernels are removed by hand,
    then the bootloader is refreshed either way."""
    if kernel.running:
        raise RuntimeError("Refusing to delete the kernel that's currently running.")

    def run(cmd):
        log_fn(f"$ {' '.join(cmd)}\n")
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
        if proc.stdout:
            log_fn(proc.stdout)
        if proc.stderr:
            log_fn(proc.stderr)
        if proc.returncode != 0:
            raise RuntimeError(f"Command failed ({proc.returncode}): {' '.join(cmd)}")

    if kernel.apt_managed:
        pkgs = [
            f"linux-image-{kernel.version}",
            f"linux-headers-{kernel.version}",
            f"linux-modules-{kernel.version}",
            f"linux-modules-extra-{kernel.version}",
        ]
        run(["sudo", "-A", "apt", "purge", "-y", *pkgs])
        run(["sudo", "-A", "apt", "autoremove", "-y"])
    else:
        for path in [
            f"/boot/vmlinuz-{kernel.version}",
            f"/boot/System.map-{kernel.version}",
            f"/boot/config-{kernel.version}",
            f"/boot/initrd.img-{kernel.version}",
        ]:
            if Path(path).exists():
                run(["sudo", "-A", "rm", "-f", path])
        modules_dir = f"/lib/modules/{kernel.version}"
        if Path(modules_dir).exists():
            run(["sudo", "-A", "rm", "-rf", modules_dir])
def grub_entry_title(version: str) -> str:
    """Best-effort GRUB menu path for a given kernel version, following the
    standard Ubuntu layout (top-level 'Ubuntu' + 'Advanced options for
    Ubuntu' submenu generated by update-grub). This is a convention, not a
    guarantee — use 'Show actual GRUB entries' in the GUI to confirm it
    matches your system before relying on it."""
    return f"Advanced options for Ubuntu>Ubuntu, with Linux {version}"


def set_default_boot(version: str, env: dict, log_fn, once: bool):
    """Persistently (grub-set-default) or one-time (grub-reboot) select
    which kernel GRUB should boot next."""
    entry = grub_entry_title(version)
    cmd = ["sudo", "-A", "grub-reboot" if once else "grub-set-default", entry]
    log_fn(f"$ {' '.join(cmd)}\n")
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if proc.stdout:
        log_fn(proc.stdout)
    if proc.stderr:
        log_fn(proc.stderr)
    if proc.returncode != 0:
        raise RuntimeError(
            f"grub-{'reboot' if once else 'set-default'} failed. This usually means "
            f"the assumed menu entry name doesn't match your actual GRUB config — "
            f"use 'Show actual GRUB entries' to check the real title."
        )
    if not once:
        # grub-set-default only stages the change; update-grub commits it.
        subprocess.run(["sudo", "-A", "update-grub"], env=env,
                        capture_output=True, text=True)


# ---------------------------------------------------------------------------
# Secure Boot / MOK signing helpers
# ---------------------------------------------------------------------------

def mok_key_exists() -> bool:
    return (MOK_DIR / "MOK.priv").exists() and (MOK_DIR / "MOK.der").exists()


def generate_mok_key(log_fn):
    MOK_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [
        "openssl", "req", "-new", "-x509", "-newkey", "rsa:2048",
        "-keyout", str(MOK_DIR / "MOK.priv"),
        "-outform", "DER", "-out", str(MOK_DIR / "MOK.der"),
        "-nodes", "-days", "36500",
        "-subj", "/CN=Kernel Manager GUI signing key/",
    ]
    log_fn(f"$ {' '.join(cmd)}\n")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    log_fn(proc.stdout + proc.stderr)
    if proc.returncode != 0:
        raise RuntimeError("Key generation failed — see log above.")
    os.chmod(MOK_DIR / "MOK.priv", 0o600)


def enroll_mok_key_in_terminal(log_fn):
    """mokutil reads its password directly from the terminal (not via
    sudo/SUDO_ASKPASS), and enrollment is only actually completed by a
    firmware dialog on the next reboot — neither of those can be scripted,
    so this opens a real terminal window for the interactive part."""
    term = find_terminal_emulator()
    if not term:
        raise RuntimeError("No terminal emulator found (tried gnome-terminal, "
                            "konsole, xfce4-terminal, xterm).")
    inner = (
        f"sudo mokutil --import '{MOK_DIR / 'MOK.der'}'; "
        f"echo; echo 'Press Enter to close this window, then reboot to "
        f"finish enrollment in the blue MOK Manager screen.'; read"
    )
    if "gnome-terminal" in term:
        cmd = [term, "--", "bash", "-c", inner]
    else:
        cmd = [term, "-e", f"bash -c \"{inner}\""]
    log_fn(f"Opening a terminal for interactive enrollment: {' '.join(cmd)}\n")
    subprocess.Popen(cmd)


def build_sign_file_tool(kernel_src_dir: Path, log_fn) -> Path:
    """scripts/sign-file is a small host tool built from the kernel source
    tree; it may not exist yet if module signing wasn't enabled during the
    build, so compile just that one file on demand."""
    sign_file = kernel_src_dir / "scripts" / "sign-file"
    if sign_file.exists():
        return sign_file
    log_fn(f"Building scripts/sign-file in {kernel_src_dir}...\n")
    proc = subprocess.run(["make", "scripts/sign-file"], cwd=str(kernel_src_dir),
                           capture_output=True, text=True)
    log_fn(proc.stdout + proc.stderr)
    if not sign_file.exists():
        raise RuntimeError("Couldn't build scripts/sign-file — see log above.")
    return sign_file


def sign_kernel_and_modules(version: str, env: dict, log_fn):
    if not mok_key_exists():
        raise RuntimeError("No MOK signing key yet — generate one first.")

    kernel_src_dir = KERNEL_BUILD_DIR / f"linux-{version.split('-')[0]}"
    if not kernel_src_dir.exists():
        raise RuntimeError(f"Can't find the source tree for {version} at "
                            f"{kernel_src_dir} (needed for scripts/sign-file).")

    sign_file = build_sign_file_tool(kernel_src_dir, log_fn)
    priv, der = MOK_DIR / "MOK.priv", MOK_DIR / "MOK.der"

    def run(cmd):
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
        if proc.stdout:
            log_fn(proc.stdout)
        if proc.stderr:
            log_fn(proc.stderr)
        if proc.returncode != 0:
            raise RuntimeError(f"Signing failed: {' '.join(cmd)}")

    vmlinuz = Path(f"/boot/vmlinuz-{version}")
    if vmlinuz.exists():
        log_fn(f"Signing {vmlinuz}\n")
        run(["sudo", "-A", str(sign_file), "sha256", str(priv), str(der), str(vmlinuz)])

    modules_dir = Path(f"/lib/modules/{version}")
    kos = list(modules_dir.rglob("*.ko")) if modules_dir.exists() else []
    log_fn(f"Signing {len(kos)} modules in {modules_dir}...\n")
    for ko in kos:
        run(["sudo", "-A", str(sign_file), "sha256", str(priv), str(der), str(ko)])
    log_fn("All done.\n")


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class KernelManagerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Kernel Manager")
        root.geometry("880x640")

        self.build_proc = None
        self.build_thread = None
        self.log_queue = queue.Queue()
        self.built_kernel_dir = None  # set once a build finishes successfully
        self._prev_cpu_times = None
        self.presets = load_presets()

        nb = ttk.Notebook(root)
        nb.pack(fill="both", expand=True, padx=8, pady=8)

        self.installed_tab = ttk.Frame(nb)
        self.build_tab = ttk.Frame(nb)
        self.logs_tab = ttk.Frame(nb)
        self.maintenance_tab = ttk.Frame(nb)
        self.sysinfo_tab = ttk.Frame(nb)
        self.mok_tab = ttk.Frame(nb)
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
        self.root.after(100, self._poll_log_queue)
        self.root.after(1000, self._update_resource_monitor)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

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

        self.update_label = ttk.Label(frame, text="")
        self.update_label.pack(fill="x", pady=(0, 6))

        columns = ("version", "type", "running", "size")
        self.tree = ttk.Treeview(frame, columns=columns, show="headings", height=14)
        for col, label, width in [
            ("version", "Version", 220),
            ("type", "Source", 140),
            ("running", "Running", 90),
            ("size", "Modules size", 110),
        ]:
            self.tree.heading(col, text=label)
            self.tree.column(col, width=width, anchor="w")
        self.tree.pack(fill="both", expand=True)

    def refresh_installed(self):
        for row in self.tree.get_children():
            self.tree.delete(row)
        for k in list_installed_kernels():
            self.tree.insert("", "end", iid=k.version, values=(
                k.version,
                "apt-managed" if k.apt_managed else "custom-built",
                "✓ running" if k.running else "",
                k.size_str,
            ))
        if hasattr(self, "mok_version_combo"):
            self.refresh_mok_tab()

    def check_for_updates(self):
        self.update_label.config(text="Checking kernel.org for the latest stable release…")

        def worker():
            latest = latest_stable_version()
            current = running_kernel().split("-")[0]  # strip -custom suffix etc.
            if latest is None:
                text = "Couldn't reach kernel.org to check for updates."
            elif latest == current:
                text = f"You're up to date — running kernel matches latest stable ({latest})."
            else:
                text = f"Update available: latest stable is {latest} (you're running {current})."
            self.root.after(0, lambda: self.update_label.config(text=text))

        threading.Thread(target=worker, daemon=True).start()

    def on_delete_selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Delete kernel", "Select a kernel first.")
            return
        version = sel[0]
        kernels = {k.version: k for k in list_installed_kernels()}
        kernel = kernels.get(version)
        if kernel is None:
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

        log_win = LogWindow(self.root, f"Deleting {version}")

        def worker():
            try:
                delete_kernel(kernel, gui_env(), log_win.append)
                log_win.append("\nDone.\n")
                self.root.after(0, self.refresh_installed)
            except Exception as e:
                log_win.append(f"\n[error] {e}\n")

        threading.Thread(target=worker, daemon=True).start()

    def on_set_boot(self, once: bool):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Boot selection", "Select a kernel first.")
            return
        version = sel[0]
        entry = grub_entry_title(version)
        action = "one-time boot (grub-reboot)" if once else "persistent default (grub-set-default)"
        if not messagebox.askyesno(
            "Confirm boot selection",
            f"This assumes the standard Ubuntu GRUB menu layout and will run "
            f"the equivalent of:\n\n  grub-{'reboot' if once else 'set-default'} "
            f"\"{entry}\"\n\nas {action}.\n\n"
            f"If this doesn't match your actual GRUB menu, use 'Show actual "
            f"GRUB entries' first to check. Continue?"
        ):
            return

        log_win = LogWindow(self.root, f"Setting boot kernel: {version}")

        def worker():
            try:
                set_default_boot(version, gui_env(), log_win.append, once)
                log_win.append("\nDone.\n")
            except Exception as e:
                log_win.append(f"\n[error] {e}\n")

        threading.Thread(target=worker, daemon=True).start()

    def on_show_grub_entries(self):
        titles, error = grub_menu_titles(gui_env())
        win = tk.Toplevel(self.root)
        win.title("Actual GRUB menu entries")
        win.geometry("560x300")
        ttk.Label(win, text="Menu entry titles found in your grub.cfg "
                             "(compare against what 'Set as Default Boot' assumes):"
                  ).pack(anchor="w", padx=8, pady=(8, 4))
        listbox = tk.Listbox(win)
        listbox.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        if titles:
            for t in titles:
                listbox.insert("end", t)
        else:
            listbox.insert("end", error or "(no menuentry lines found)")

    # ---------------- Build tab ----------------

    def _build_build_tab(self):
        frame = self.build_tab

        opts = ttk.LabelFrame(frame, text="Toolchain options")
        opts.pack(fill="x", padx=4, pady=4)

        self.toolchain_var = tk.StringVar(value=self.presets.get("toolchain", "gcc"))
        ttk.Radiobutton(opts, text="GCC", variable=self.toolchain_var, value="gcc",
                        command=self._sync_lto_state).grid(row=0, column=0, sticky="w", padx=6, pady=4)
        ttk.Radiobutton(opts, text="Clang + LLD", variable=self.toolchain_var, value="clang",
                        command=self._sync_lto_state).grid(row=0, column=1, sticky="w", padx=6, pady=4)

        self.lto_var = tk.BooleanVar(value=self.presets.get("lto", False))
        self.lto_check = ttk.Checkbutton(opts, text="Enable ThinLTO (Clang only, slow/RAM-heavy)",
                                          variable=self.lto_var,
                                          state="normal" if self.toolchain_var.get() == "clang" else "disabled")
        self.lto_check.grid(row=1, column=0, columnspan=2, sticky="w", padx=6, pady=2)

        self.debug_var = tk.BooleanVar(value=self.presets.get("debug", False))
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
        self.resource_label = ttk.Label(monitor, text="Reading...", font=("Monospace", 9))
        self.resource_label.pack(anchor="w", padx=8, pady=6)

        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill="x", pady=6)
        self.start_btn = ttk.Button(btn_frame, text="Start Build", command=self.start_build)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(btn_frame, text="Stop", command=self.stop_build, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        self.install_btn = ttk.Button(btn_frame, text="Install Now", command=self.start_install, state="disabled")
        self.install_btn.pack(side="left", padx=6)

        ttk.Label(frame, text="Live build output:").pack(anchor="w", padx=4)
        log_frame = ttk.Frame(frame)
        log_frame.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        self.log_text = tk.Text(log_frame, wrap="none", height=20, bg="black", fg="#33ff33",
                                 insertbackground="#33ff33", font=("Monospace", 9))
        yscroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=yscroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        yscroll.pack(side="right", fill="y")

    def _sync_lto_state(self):
        if self.toolchain_var.get() == "clang":
            self.lto_check.configure(state="normal")
        else:
            self.lto_var.set(False)
            self.lto_check.configure(state="disabled")

    def _append_log(self, text: str):
        self.log_text.insert("end", text)
        self.log_text.see("end")

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
        self.presets = {
            "toolchain": self.toolchain_var.get(),
            "lto": self.lto_var.get(),
            "debug": self.debug_var.get(),
            "jobs": self.jobs_var.get(),
        }
        save_presets(self.presets)
        self.root.destroy()

    def start_build(self):
        if self.build_proc is not None:
            return
        if not BUILD_SCRIPT.exists():
            messagebox.showerror("Build", f"Can't find {BUILD_SCRIPT}")
            return

        cmd = ["bash", str(BUILD_SCRIPT), "--jobs", self.jobs_var.get()]
        if self.toolchain_var.get() == "clang":
            cmd.append("--clang")
            if self.lto_var.get():
                cmd.append("--lto")
        if self.debug_var.get():
            cmd.append("--full-debug-info")
        if self.force_var.get():
            cmd.append("--force")

        save_presets({
            "toolchain": self.toolchain_var.get(),
            "lto": self.lto_var.get(),
            "debug": self.debug_var.get(),
            "jobs": self.jobs_var.get(),
        })

        self.log_text.delete("1.0", "end")
        self._append_log(f"$ {' '.join(cmd)}\n\n")
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.install_btn.configure(state="disabled")
        self.built_kernel_dir = None

        env = gui_env()

        def worker():
            self.build_proc = subprocess.Popen(
                cmd, cwd=str(SCRIPT_DIR), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1
            )
            for line in self.build_proc.stdout:
                self.log_queue.put(line)
                # Watch for the build script telling us where the built
                # kernel source directory is, so "Install Now" knows where
                # to run install-custom-kernel.sh from.
                m = re.search(r"cd (.+/linux-[\d.]+)\s*$", line)
                if m:
                    self.built_kernel_dir = m.group(1).strip()
            rc = self.build_proc.wait()
            self.log_queue.put(f"\n[build finished with exit code {rc}]\n")
            self.build_proc = None
            self.root.after(0, lambda: self._on_build_finished(rc))

        self.build_thread = threading.Thread(target=worker, daemon=True)
        self.build_thread.start()

    def _on_build_finished(self, rc: int):
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        if rc == 0 and self.built_kernel_dir:
            self.install_btn.configure(state="normal")
            send_notification("Kernel Manager", "Build finished successfully. Ready to install.")
        elif rc == 0:
            self._append_log("\n[warn] Build succeeded but couldn't detect the "
                              "kernel source directory automatically — check the "
                              "log above and run install-custom-kernel.sh manually.\n")
            send_notification("Kernel Manager", "Build finished successfully.")
        else:
            send_notification("Kernel Manager", f"Build failed (exit code {rc}). Check the log.")

    def stop_build(self):
        if self.build_proc is not None:
            self._append_log("\n[stopping build...]\n")
            self.build_proc.terminate()

    def start_install(self):
        if not self.built_kernel_dir:
            messagebox.showerror("Install", "No successfully built kernel to install.")
            return
        if not INSTALL_SCRIPT.exists():
            messagebox.showerror("Install", f"Can't find {INSTALL_SCRIPT}")
            return

        # Make sure install-custom-kernel.sh is present in the kernel dir
        # (build-custom-kernel.sh normally copies it there already).
        dest = Path(self.built_kernel_dir) / "install-custom-kernel.sh"
        if not dest.exists():
            shutil.copy(INSTALL_SCRIPT, dest)
            dest.chmod(0o755)

        self._append_log(f"\n$ cd {self.built_kernel_dir} && ./install-custom-kernel.sh\n\n")
        self.install_btn.configure(state="disabled")
        env = gui_env()

        def worker():
            proc = subprocess.Popen(
                ["bash", str(dest)], cwd=self.built_kernel_dir, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1
            )
            for line in proc.stdout:
                self.log_queue.put(line)
            rc = proc.wait()
            self.log_queue.put(f"\n[install finished with exit code {rc}]\n")
            if rc == 0:
                self.log_queue.put(
                    "\nInstalled. Reboot and select the new kernel from the "
                    "GRUB menu to try it — your previous kernel is still there "
                    "as a fallback.\n"
                )
                send_notification("Kernel Manager", "Kernel installed. Reboot to try it.")
            else:
                send_notification("Kernel Manager", f"Install failed (exit code {rc}). Check the log.")
            self.root.after(0, lambda: self.refresh_installed())

        threading.Thread(target=worker, daemon=True).start()

    def _poll_log_queue(self):
        try:
            while True:
                line = self.log_queue.get_nowait()
                self._append_log(line)
        except queue.Empty:
            pass
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
        self.logs_tree = ttk.Treeview(frame, columns=columns, show="headings", height=16)
        for col, label, width in [("name", "Log file", 320), ("date", "Date", 180), ("size", "Size", 100)]:
            self.logs_tree.heading(col, text=label)
            self.logs_tree.column(col, width=width, anchor="w")
        self.logs_tree.pack(fill="both", expand=True, padx=4, pady=(0, 4))
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
        text = tk.Text(win, wrap="none", bg="black", fg="#33ff33", font=("Monospace", 9))
        scroll = ttk.Scrollbar(win, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        try:
            text.insert("end", path.read_text(errors="ignore"))
        except Exception as e:
            text.insert("end", f"Couldn't read log: {e}")
        text.configure(state="disabled")

    def on_delete_log(self):
        sel = self.logs_tree.selection()
        if not sel:
            messagebox.showinfo("Build logs", "Select a log file first.")
            return
        if not messagebox.askyesno("Delete log", f"Delete {len(sel)} log file(s)?"):
            return
        for s in sel:
            try:
                Path(s).unlink()
            except Exception:
                pass
        self.refresh_logs_tab()

    # ---------------- Maintenance tab ----------------

    def _build_maintenance_tab(self):
        frame = self.maintenance_tab

        info = ttk.Frame(frame)
        info.pack(fill="x", padx=4, pady=6)
        self.disk_usage_label = ttk.Label(info, text="")
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
        self.maint_tree = ttk.Treeview(frame, columns=columns, show="headings", height=14)
        self.maint_tree.heading("dir", text="Directory")
        self.maint_tree.heading("size", text="Size")
        self.maint_tree.column("dir", width=500, anchor="w")
        self.maint_tree.column("size", width=100, anchor="w")
        self.maint_tree.pack(fill="both", expand=True, padx=4, pady=(0, 4))

        self.refresh_maintenance_tab()

    def refresh_maintenance_tab(self):
        for row in self.maint_tree.get_children():
            self.maint_tree.delete(row)
        total = "?"
        if KERNEL_BUILD_DIR.exists():
            try:
                total = subprocess.check_output(["du", "-sh", str(KERNEL_BUILD_DIR)], text=True).split()[0]
            except Exception:
                pass
        self.disk_usage_label.configure(text=f"Total size of {KERNEL_BUILD_DIR}: {total}")

        current_src = f"linux-{running_kernel().split('-')[0]}"
        for path, size in kernel_build_source_dirs():
            note = "  (matches running kernel)" if path.name == current_src else ""
            self.maint_tree.insert("", "end", iid=str(path), values=(path.name + note, size))

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
        for s in sel:
            try:
                shutil.rmtree(s)
            except Exception as e:
                messagebox.showerror("Maintenance", f"Failed to remove {s}: {e}")
        self.refresh_maintenance_tab()

    # ---------------- System Info tab ----------------

    def _build_sysinfo_tab(self):
        frame = self.sysinfo_tab
        ttk.Button(frame, text="Refresh", command=self.refresh_sysinfo_tab).pack(anchor="w", padx=4, pady=6)
        self.sysinfo_text = tk.Text(frame, wrap="word", height=20, font=("Monospace", 10))
        self.sysinfo_text.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        self.sysinfo_text.configure(state="disabled")
        self.refresh_sysinfo_tab()

    def refresh_sysinfo_tab(self):
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
        self.sysinfo_text.configure(state="normal")
        self.sysinfo_text.delete("1.0", "end")
        self.sysinfo_text.insert("end", "\n".join(lines))
        self.sysinfo_text.configure(state="disabled")

    # ---------------- Secure Boot (MOK) tab ----------------

    def _build_mok_tab(self):
        frame = self.mok_tab

        ttk.Label(frame, text=
            "Signing your custom kernel/modules lets you keep Secure Boot enabled.\n"
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
        versions = [k.version for k in list_installed_kernels() if not k.apt_managed]
        self.mok_version_combo.configure(values=versions)
        if versions and not self.mok_version_var.get():
            self.mok_version_var.set(versions[0])

    def on_generate_mok_key(self):
        if mok_key_exists() and not messagebox.askyesno(
            "Generate key", "A key already exists — generate a new one and replace it?"
        ):
            return
        log_win = LogWindow(self.root, "Generating MOK signing key")

        def worker():
            try:
                generate_mok_key(log_win.append)
                log_win.append("\nDone. Key stored in: " + str(MOK_DIR) + "\n")
                self.root.after(0, self.refresh_mok_tab)
            except Exception as e:
                log_win.append(f"\n[error] {e}\n")

        threading.Thread(target=worker, daemon=True).start()

    def on_enroll_mok_key(self):
        if not mok_key_exists():
            messagebox.showerror("Enroll key", "Generate a key first.")
            return
        log_win = LogWindow(self.root, "Enrolling MOK key")
        try:
            enroll_mok_key_in_terminal(log_win.append)
            log_win.append(
                "\nComplete the password prompts in the terminal window, then "
                "reboot and follow the blue MOK Manager screen to finish "
                "enrollment (this last step can't be automated by any script)."
            )
        except Exception as e:
            log_win.append(f"\n[error] {e}\n")

    def on_sign_kernel(self):
        version = self.mok_version_var.get()
        if not version:
            messagebox.showinfo("Sign kernel", "Select a kernel version first.")
            return
        log_win = LogWindow(self.root, f"Signing {version}")
        env = gui_env()

        def worker():
            try:
                sign_kernel_and_modules(version, env, log_win.append)
            except Exception as e:
                log_win.append(f"\n[error] {e}\n")

        threading.Thread(target=worker, daemon=True).start()


class LogWindow:
    """Small popup with a live-updating text log, used for delete operations."""
    def __init__(self, parent, title):
        self.win = tk.Toplevel(parent)
        self.win.title(title)
        self.win.geometry("600x300")
        self.text = tk.Text(self.win, bg="black", fg="#33ff33", font=("Monospace", 9))
        self.text.pack(fill="both", expand=True)
        self.queue = queue.Queue()
        self.win.after(100, self._poll)

    def append(self, s: str):
        self.queue.put(s)

    def _poll(self):
        try:
            while True:
                s = self.queue.get_nowait()
                self.text.insert("end", s)
                self.text.see("end")
        except queue.Empty:
            pass
        self.win.after(100, self._poll)


def main():
    if not BUILD_SCRIPT.exists() or not INSTALL_SCRIPT.exists():
        print(f"Expected build-custom-kernel.sh and install-custom-kernel.sh "
              f"next to this script in {SCRIPT_DIR}", file=sys.stderr)
        sys.exit(1)
    root = tk.Tk()
    app = KernelManagerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
