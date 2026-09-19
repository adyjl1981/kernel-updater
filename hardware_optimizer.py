#!/usr/bin/env python3
"""Hardware inventory and conservative kernel-config optimisation support."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass, asdict, field
from pathlib import Path


FS_CONFIG = {
    "ext4": "EXT4_FS", "ext3": "EXT3_FS", "ext2": "EXT2_FS",
    "btrfs": "BTRFS_FS", "xfs": "XFS_FS", "f2fs": "F2FS_FS",
    "vfat": "VFAT_FS", "fat": "FAT_FS", "exfat": "EXFAT_FS",
    "ntfs": "NTFS3_FS", "nfs": "NFS_FS", "nfs4": "NFS_V4",
}

# This is intentionally broader than the hardware snapshot.  Infrastructure is
# built in where it may be needed before the initramfs can load modules.
SAFETY_BUILTIN = {
    "MODULES", "BLOCK", "BLK_DEV_INITRD", "DEVTMPFS", "DEVTMPFS_MOUNT",
    "TMPFS", "PROC_FS", "SYSFS", "EFI", "EFI_STUB", "PARTITION_ADVANCED",
    "EFI_PARTITION", "MSDOS_PARTITION", "NET", "INET", "IPV6", "PCI",
    "HOTPLUG_PCI", "USB_SUPPORT", "USB", "HID", "INPUT", "VT",
    # AF_UNIX is used by udev/systemd in the initramfs.  It is a bool, not a
    # module: asking Kconfig for CONFIG_UNIX=m silently leaves it disabled.
    "UNIX", "PRINTK", "VT_CONSOLE", "VGA_CONSOLE", "FRAMEBUFFER_CONSOLE",
    # Keep every external-initramfs format which Ubuntu may select.
    "RD_GZIP", "RD_BZIP2", "RD_LZMA", "RD_XZ", "RD_LZO", "RD_LZ4", "RD_ZSTD",
    "FW_LOADER", "ACPI", "PCI_MSI",
    "NETDEVICES", "ETHERNET", "WLAN", "WIRELESS", "NETFILTER",
    # Boolean feature gates, even when their parent drivers are modules.
    "MEDIA_USB_SUPPORT", "USB_SERIAL_GENERIC",
}
SAFETY_MODULES = {
    # USB hosts, hubs/storage and removable media
    "USB_XHCI_HCD", "USB_XHCI_PCI", "USB_EHCI_HCD", "USB_EHCI_PCI",
    "USB_OHCI_HCD", "USB_OHCI_PCI", "USB_UHCI_HCD", "USB_STORAGE", "UAS",
    "SCSI", "BLK_DEV_SD", "CHR_DEV_SG", "SATA_AHCI", "NVME_CORE", "BLK_DEV_NVME",
    "BLK_DEV_DM", "DM_CRYPT", "MD", "BLK_DEV_MD", "MMC", "MMC_BLOCK", "CDROM",
    "BLK_DEV_SR", "ISO9660_FS", "UDF_FS",
    # Input, Bluetooth, audio, cameras, printers and serial adapters
    "HID_GENERIC", "USB_HID", "HID_MULTITOUCH", "INPUT_EVDEV",
    "KEYBOARD_ATKBD", "MOUSE_PS2", "BT", "BT_RFCOMM", "BT_BNEP", "BT_HIDP",
    "BT_HCIBTUSB", "BT_HCIUART", "SND", "SND_USB_AUDIO", "MEDIA_SUPPORT",
    "USB_VIDEO_CLASS", "USB_PRINTER", "USB_SERIAL",
    "USB_SERIAL_FTDI_SIO", "USB_SERIAL_PL2303",
    "USB_SERIAL_CP210X", "USB_ACM",
    # Normal desktop networking, Wi-Fi, printing and display connectors
    "CFG80211", "MAC80211", "RFKILL",
    "PACKET", "BRIDGE", "VLAN_8021Q", "MDNS_RESOLVER",
    "USB_USBNET", "USB_NET_CDCETHER", "USB_NET_RNDIS_HOST", "PPP",
    "DRM", "DRM_KMS_HELPER", "DRM_DISPLAY_HELPER", "I2C", "I2C_ALGOBIT",
    "TYPEC", "USB_TYPEC", "THUNDERBOLT",
    # Common removable-drive filesystems and character sets
    "FAT_FS", "VFAT_FS", "EXFAT_FS", "NTFS3_FS", "FUSE_FS", "NLS",
    "NLS_CODEPAGE_437", "NLS_ISO8859_1", "NLS_UTF8",
}

# localmodconfig is deliberately aggressive and also removes bools which do
# not correspond to a loaded module.  Preserve the known-working kernel's
# choices for a curated early-boot surface for the current architecture instead
# of trying to rediscover all of Kconfig's platform dependency graph here.
BOOT_BASELINE_SYMBOLS = {
    "64BIT", "X86_64", "SMP", "X86_LOCAL_APIC", "X86_IO_APIC",
    "ACPI", "ACPI_I2C_OPREGION", "PCI", "PCI_MSI", "PCI_DIRECT",
    "EFI", "EFI_STUB", "EFI_MIXED", "EFI_RUNTIME_MAP", "EFIVAR_FS",
    "FW_LOADER", "MODULES", "BLOCK", "SCSI", "BLK_DEV_INITRD",
    "DEVTMPFS", "DEVTMPFS_MOUNT", "TMPFS", "PROC_FS", "SYSFS",
    "UNIX", "PRINTK", "VT", "VT_CONSOLE", "VGA_CONSOLE",
    "FRAMEBUFFER_CONSOLE", "FRAMEBUFFER_CONSOLE_DEFERRED_TAKEOVER",
    "SERIAL_8250", "SERIAL_8250_CONSOLE", "EARLY_PRINTK",
    "DRM", "DRM_KMS_HELPER", "DRM_CLIENT_LIB", "DRM_CLIENT_SELECTION",
    "DRM_GEM_SHMEM_HELPER", "DRM_SYSFB_HELPER", "DRM_SIMPLEDRM",
    "RD_GZIP", "RD_BZIP2", "RD_LZMA", "RD_XZ", "RD_LZO", "RD_LZ4", "RD_ZSTD",
    "PARTITION_ADVANCED", "EFI_PARTITION", "MSDOS_PARTITION",
    "MD", "BLK_DEV_MD", "BLK_DEV_DM", "BLK_DEV_DM_BUILTIN",
    "DM_INIT", "DM_UEVENT", "DM_CRYPT",
}


# Common local OCI/LXC runtime capability floor. Keep both firewall backends:
# installed userspace can select iptables-nft or legacy independently of lsmod.
# Values are chosen from the TARGET Kconfig types, never guessed from names.
CONTAINER_REQUIRED = set("""
MODULES NET INET IPV6 NETDEVICES NET_CORE UNIX PACKET
NAMESPACES UTS_NS IPC_NS PID_NS NET_NS USER_NS
CGROUPS CGROUP_SCHED FAIR_GROUP_SCHED CFS_BANDWIDTH CPUSETS MEMCG
CGROUP_PIDS CGROUP_CPUACCT CGROUP_DEVICE CGROUP_FREEZER BLK_CGROUP
CGROUP_BPF BPF BPF_SYSCALL KEYS
SECCOMP SECCOMP_FILTER SYSVIPC POSIX_MQUEUE TMPFS TMPFS_POSIX_ACL
OVERLAY_FS VETH BRIDGE BRIDGE_NETFILTER
NETFILTER NETFILTER_ADVANCED NF_CONNTRACK NF_NAT NF_NAT_MASQUERADE
NF_TABLES NF_TABLES_INET NF_TABLES_IPV4 NF_TABLES_IPV6
NFT_CT NFT_NAT NFT_MASQ NFT_COMPAT NFT_FIB NFT_FIB_IPV4 NFT_FIB_IPV6
NETFILTER_XTABLES NETFILTER_XT_MATCH_ADDRTYPE NETFILTER_XT_MATCH_CONNTRACK
NETFILTER_XT_MATCH_COMMENT NETFILTER_XT_MARK NETFILTER_XT_NAT
NETFILTER_XT_TARGET_MASQUERADE NETFILTER_XT_TARGET_CHECKSUM
IP_NF_IPTABLES IP_NF_FILTER IP_NF_NAT IP_NF_MANGLE IP_NF_RAW
IP6_NF_IPTABLES IP6_NF_FILTER IP6_NF_NAT IP6_NF_MANGLE IP6_NF_RAW
""".split())
# These gates were introduced by newer kernels; require them when defined.
CONTAINER_VERSION_GATES = {
    "NETFILTER_XTABLES_LEGACY", "IP_NF_IPTABLES_LEGACY", "IP6_NF_IPTABLES_LEGACY",
}
CONTAINER_BASELINE_PATTERN = re.compile(
    r"CGROUP|^MEMCG|^CPUSETS$|^CFS_BANDWIDTH$|^FAIR_GROUP_SCHED$|"
    r"^NAMESPACES$|_NS$|^SECCOMP|^SYSVIPC|^POSIX_MQUEUE|^OVERLAY_FS|"
    r"^BPF|^KEYS$|^BLK_DEV_THROTTLING$|^SECURITY_(APPARMOR|SELINUX)")
RUNTIMES = {
    "Docker": (("dockerd",), ("docker-ce", "docker.io", "moby-engine"),
               ("docker.service", "docker.socket", "snap.docker.dockerd.service")),
    "containerd": (("containerd",), ("containerd", "containerd.io"), ("containerd.service",)),
    "Podman": (("podman",), ("podman",), ("podman.service", "podman.socket")),
    "CRI-O": (("crio",), ("cri-o",), ("crio.service",)),
    "LXC/LXD/Incus": (("lxc-start", "lxd", "incusd"), ("lxc", "lxd", "incus"),
                      ("lxc.service", "lxd.service", "incus.service", "snap.lxd.daemon.service")),
}


@dataclass
class HardwareReport:
    architecture: str
    cpu: str
    root_source: str = ""
    root_filesystem: str = ""
    boot_filesystems: list[str] = field(default_factory=list)
    pci_devices: list[str] = field(default_factory=list)
    usb_devices: list[str] = field(default_factory=list)
    modules: list[str] = field(default_factory=list)
    categories: dict[str, list[str]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    safe: bool = False
    refusal_reason: str = ""
    network_devices: list[dict] = field(default_factory=list)
    container_runtimes: dict[str, list[str]] = field(default_factory=dict)


class Scanner:
    def __init__(self, sys_root="/sys", proc_root="/proc", boot_root="/boot", runner=None):
        self.sys = Path(sys_root); self.proc = Path(proc_root); self.boot = Path(boot_root)
        self.runner = runner or self._run

    @staticmethod
    def _run(args):
        return subprocess.run(args, capture_output=True, text=True, timeout=12).stdout

    def command(self, args, warnings):
        if not shutil.which(args[0]):
            warnings.append(f"{args[0]} is unavailable; continuing with sysfs/proc data")
            return ""
        try:
            return self.runner(args)
        except Exception as exc:
            warnings.append(f"{args[0]} failed: {exc}")
            return ""

    def detect_container_runtimes(self, warnings):
        """Read-only evidence; never run/start a runtime or depend on loaded modules."""
        packages = self.command(
            ["dpkg-query", "-W", "-f=${binary:Package} ${db:Status-Status}\n"], warnings)
        installed = {p[0].split(":")[0] for line in packages.splitlines()
                     if len(p := line.split()) == 2 and p[1] == "installed"}
        found = {}
        for name, (executables, package_names, units) in RUNTIMES.items():
            evidence = ["executable: " + exe for exe in executables if shutil.which(exe)]
            evidence += ["package: " + pkg for pkg in package_names if pkg in installed]
            for unit in units:
                # --root reads enablement symlinks without requiring a system bus.
                state = self.command(["systemctl", "--root=/", "is-enabled", unit], warnings).strip()
                if state in ("enabled", "enabled-runtime", "linked", "linked-runtime"):
                    evidence.append("unit: " + unit + " (" + state + ")")
            if evidence:
                found[name] = evidence
        return found

    @staticmethod
    def _module_from_device(device):
        link = device / "driver" / "module"
        try:
            return link.resolve(strict=True).name.replace("-", "_")
        except OSError:
            return ""

    def scan(self):
        warnings = []
        cpu = "unknown"
        try:
            for line in (self.proc / "cpuinfo").read_text(errors="replace").splitlines():
                if re.match(r"^(model name|Hardware|Processor)\s*:", line):
                    cpu = line.split(":", 1)[1].strip(); break
        except OSError:
            warnings.append("Unable to read CPU information")
        arch = platform.machine()
        root = self.command(["findmnt", "-nro", "SOURCE,FSTYPE", "/"], warnings).strip().split()
        root_source, root_fs = (root[0], root[1].lower()) if len(root) >= 2 else ("", "")
        mounts = self.command(["findmnt", "-nro", "TARGET,FSTYPE"], warnings)
        boot_fs = sorted({p[1].lower() for p in (x.split() for x in mounts.splitlines())
                          if len(p) >= 2 and p[0] in ("/boot", "/boot/efi")})
        pci_raw = self.command(["lspci", "-nnk"], warnings)
        pci = [x for x in pci_raw.splitlines() if x and not x[0].isspace()]
        usb = [x for x in self.command(["lsusb"], warnings).splitlines() if x.strip()]
        usb_sysfs = self.sys / "bus/usb/devices"
        if usb_sysfs.is_dir():
            for dev in usb_sysfs.iterdir():
                try:
                    vendor = (dev / "manufacturer").read_text(errors="replace").strip() if (dev / "manufacturer").is_file() else ""
                    product = (dev / "product").read_text(errors="replace").strip() if (dev / "product").is_file() else ""
                except OSError:
                    continue
                label = " ".join(x for x in (vendor, product) if x)
                if label and not any(label in existing for existing in usb):
                    usb.append(f"sysfs {dev.name}: {label}")
        modules = set()
        lsmod = self.command(["lsmod"], warnings).splitlines()
        modules.update(x.split()[0].replace("-", "_") for x in lsmod[1:] if x.split())
        modules.update(match.group(1).strip().replace("-", "_")
                       for match in re.finditer(r"^\s*Kernel driver in use:\s*(\S+)", pci_raw, re.M))
        for bus in (self.sys / "bus/pci/devices", self.sys / "bus/usb/devices"):
            if bus.is_dir():
                for dev in bus.iterdir():
                    module = self._module_from_device(dev)
                    if module: modules.add(module)
        categories = {
            "Storage and boot": [x for x in pci if re.search(r"storage|sata|nvme|raid|scsi", x, re.I)],
            "Graphics/display": [x for x in pci if re.search(r"vga|display|3d", x, re.I)],
            "Ethernet/Wi-Fi": [x for x in pci if re.search(r"ethernet|network|wireless", x, re.I)],
            "Audio": [x for x in pci if re.search(r"audio|multimedia", x, re.I)],
            "Bluetooth": [x for x in usb if "bluetooth" in x.lower()],
            "Input devices": [x for x in usb if re.search(r"keyboard|mouse|input|touch|tablet", x, re.I)],
            "USB controllers/devices": [x for x in pci if "USB" in x] + usb,
            "Other PCI/PCIe": pci,
        }
        network_devices = []
        for block in re.split(r"(?=^\S)", pci_raw, flags=re.M):
            if not re.search(r"ethernet|network|wireless", block.split("\n")[0], re.I):
                continue
            drivers = re.findall(r"Kernel driver in use:\s*(\S+)", block)
            candidates = re.findall(r"Kernel modules:\s*([^\n]+)", block)
            drivers += [m.strip() for line in candidates for m in line.split(",")]
            network_devices.append({"device": block.split("\n")[0],
                                    "drivers": sorted(set(m.replace("-", "_") for m in drivers))})
        # PCI class 02 covers network controllers even without lspci or a
        # bound driver. Resolve modaliases read-only; never load a module.
        for device in (self.sys / "bus/pci/devices").glob("*"):
            try:
                network = (device / "class").read_text().strip().startswith("0x02")
            except OSError:
                continue
            if not network:
                continue
            drivers = []
            bound = self._module_from_device(device)
            if bound:
                drivers.append(bound)
            elif (device / "driver").exists():
                drivers.append((device / "driver").resolve().name.replace("-", "_"))
            try:
                alias = (device / "modalias").read_text().strip()
                drivers.extend(self.command(["modprobe", "--resolve-alias", alias], warnings).split())
            except OSError:
                pass
            network_devices.append({"device": str(device),
                                    "drivers": sorted(set(m.replace("-", "_") for m in drivers))})
        # Includes USB adapters and built-in drivers via their bound net device.
        for interface in (self.sys / "class/net").glob("*"):
            device = interface / "device"
            if not device.exists():
                continue
            driver = self._module_from_device(device)
            if not driver and (device / "driver").exists():
                driver = (device / "driver").resolve().name.replace("-", "_")
            network_devices.append({"device": str(interface), "drivers": [driver] if driver else []})
        modules.update(m for dev in network_devices for m in dev["drivers"])
        reason = ""
        if not root_source or not root_fs:
            reason = "The root storage source/filesystem could not be determined. Use Standard mode."
        elif root_fs not in FS_CONFIG:
            reason = f"Root filesystem '{root_fs}' is not in the verified boot filesystem set. Use Standard mode."
        elif root_source.startswith("/dev/") and not categories["Storage and boot"]:
            reason = "The controller behind the root block device could not be identified. Use Standard mode."
        elif not modules:
            reason = "No loaded or sysfs-bound hardware drivers were found. Use Standard mode."
        return HardwareReport(arch, cpu, root_source, root_fs, boot_fs, pci, usb,
                              sorted(modules), categories, warnings, not reason, reason, network_devices,
                              self.detect_container_runtimes(warnings))


def render_report(r):
    lines = [f"CPU: {r.cpu}", f"Architecture: {r.architecture}",
             f"Root: {r.root_source or 'unknown'} ({r.root_filesystem or 'unknown'})",
             f"Detected driver modules: {', '.join(r.modules) or 'none'}", ""]
    lines.append("Container runtimes: " + (", ".join(r.container_runtimes) or "none detected"))
    if r.container_runtimes:
        lines.append("  Retaining namespaces, cgroups, IPC, seccomp, overlayfs and container networking.")
    for name, items in r.categories.items():
        lines.append(f"{name}:" + ("\n  " + "\n  ".join(items) if items else " none detected"))
    lines += ["", "Compatibility retained:",
              "  USB hosts/hubs, HID/input, storage, Bluetooth, networking/Wi-Fi,",
              "  printers, USB audio/webcams/serial, removable media/filesystems,",
              "  optical media and graphics display infrastructure.", "",
              "Optimised build readiness: " + ("READY" if r.safe else "REFUSED — " + r.refusal_reason)]
    if r.warnings: lines += ["", "Scan notes:", *["  " + x for x in r.warnings]]
    return "\n".join(lines)


def _config_values(path):
    text = Path(path).read_text()
    return dict(re.findall(r"^CONFIG_([A-Za-z0-9_]+)=([^\n]+)$", text, re.M))


def networking_baseline(baseline, source, extra_symbols=()):
    """Conservatively retain working networking and its referenced dependencies.

    Use target-source symbol definitions, not a per-adapter name table. This is
    intentionally an over-approximation, not a second Kconfig evaluator: only
    working values are restored; real Kconfig resolves expressions afterwards.
    """
    working = _config_values(baseline)
    source = Path(source)
    files = list(source.rglob("Kconfig*"))
    definitions = {}
    retained = set(extra_symbols)
    for path in files:
        text = path.read_text(errors="replace")
        blocks = re.split(r"^\s*(?:menuconfig|config)\s+(\w+)[^\n]*\n", text, flags=re.M)
        for name, body in zip(blocks[1::2], blocks[2::2]):
            # Only dependency/select expressions, not unrelated neighbouring
            # symbols or names mentioned in help text.
            expressions = " ".join(re.findall(
                r"^\s*(?:depends on|select|imply|default|def_bool|def_tristate)\s+([^\n]+)",
                body, re.M))
            definitions.setdefault(name, set()).update(
                set(re.findall(r"\b[A-Za-z][A-Za-z0-9_]*\b", expressions)) & working.keys())
        relative = path.relative_to(source).as_posix()
        if relative.startswith(("drivers/net/", "net/")):
            retained.update(re.findall(r"^\s*(?:menuconfig|config)\s+(\w+)", text, re.M))
            # Menu/if gates can be outside individual symbol definitions.
            gates = " ".join(re.findall(r"^\s*if\s+([^\n]+)", text, re.M))
            retained.update(set(re.findall(r"\b\w+\b", gates)) & working.keys())
    if not retained:
        raise RuntimeError("target networking Kconfig definitions are unavailable")
    pending = list(retained)
    while pending:
        name = pending.pop()
        if working.get(name) not in ("y", "m"):
            continue
        for dependency in definitions.get(name, set()) - retained:
            retained.add(dependency)
            pending.append(dependency)
    return {k: working[k] for k in retained if working.get(k) in ("y", "m")}


def container_requirements(report, baseline, source):
    if not report.container_runtimes:
        return {}
    if not baseline or not source:
        raise RuntimeError("container preservation requires a baseline and target Kconfig source")
    working = _config_values(baseline)
    types = {}
    for path in Path(source).rglob("Kconfig*"):
        blocks = re.split(r"^\s*(?:menuconfig|config)\s+(\w+)[^\n]*\n",
                          path.read_text(errors="replace"), flags=re.M)
        for name, body in zip(blocks[1::2], blocks[2::2]):
            kind = re.search(r"^\s*(bool|tristate|def_bool|def_tristate)\b", body, re.M)
            if kind:
                types[name] = kind[1].removeprefix("def_")
    unavailable = CONTAINER_REQUIRED - types.keys()
    if unavailable:
        raise RuntimeError("container requirements unavailable in target Kconfig: " +
                           ", ".join("CONFIG_" + k for k in sorted(unavailable)))
    seeds = {k for k in working if CONTAINER_BASELINE_PATTERN.search(k)}
    expected = networking_baseline(baseline, source, seeds)
    for symbol in CONTAINER_REQUIRED | (CONTAINER_VERSION_GATES & types.keys()):
        expected.setdefault(symbol, "y" if types[symbol] == "bool" else "m")
    # A target release may change a formerly tristate feature to bool.
    for symbol in expected:
        if types.get(symbol) == "bool":
            expected[symbol] = "y"
    return expected


def verify_containers(path, report, baseline, source):
    expected = container_requirements(report, baseline, source)
    values = _config_values(path)
    missing = sorted(k for k, v in expected.items()
                     if values.get(k) not in ({"y"} if v == "y" else {"y", "m"}))
    if missing:
        raise RuntimeError("container support rejected by Kconfig (" +
                           ", ".join(report.container_runtimes) + "): " +
                           ", ".join("CONFIG_" + k for k in missing))


def network_driver_symbols(source):
    """Resolve module output names through the target Kbuild files."""
    result = {}
    for path in Path(source).rglob("Makefile"):
        text = path.read_text(errors="replace").replace("\\\n", " ")
        for symbol, outputs in re.findall(
                r"obj-\$\(CONFIG_(\w+)\)\s*[:+]?=([^\n]+)", text):
            for output in re.findall(r"([\w-]+)\.o\b", outputs):
                result.setdefault(output.replace("-", "_"), set()).add(symbol)
    return result


def verify_network(path, report, baseline, source):
    values = _config_values(path)
    expected = networking_baseline(baseline, source)
    missing = sorted(k for k, v in expected.items()
                     if values.get(k) not in ({"y"} if v == "y" else {"y", "m"}))
    mapping = network_driver_symbols(source)
    failures = []
    for device in report.network_devices:
        symbols = set().union(*(mapping.get(m, set()) for m in device["drivers"]))
        if not symbols or not any(values.get(k) in ("y", "m") for k in symbols):
            failures.append(device["device"] + " (drivers: " + ", ".join(device["drivers"]) + ")")
    if missing or failures:
        raise RuntimeError("network support rejected by Kconfig: " +
                           ", ".join("CONFIG_" + k for k in missing) +
                           "; detected adapters without verified support: " + "; ".join(failures))


def update_config(path, report, baseline=None, source=None):
    """Apply required values without touching an installed kernel config."""
    config = Path(path)
    values = {x: "y" for x in SAFETY_BUILTIN}
    values.update({x: "m" for x in SAFETY_MODULES})
    if baseline:
        baseline_values = _config_values(baseline)
        for symbol in BOOT_BASELINE_SYMBOLS:
            value = baseline_values.get(symbol)
            if value in ("y", "m"):
                values[symbol] = value
    if source and baseline:
        values.update(networking_baseline(baseline, source))
    values.update(container_requirements(report, baseline, source))
    values[FS_CONFIG[report.root_filesystem]] = "y"
    for fs in report.boot_filesystems:
        if fs in FS_CONFIG: values[FS_CONFIG[fs]] = "y"
    lines = config.read_text().splitlines()
    wanted = {f"CONFIG_{k}": v for k, v in values.items()}
    output, seen = [], set()
    pattern = re.compile(r"^(?:# )?(CONFIG_[A-Za-z0-9_]+)(?:=| is not set)")
    for line in lines:
        match = pattern.match(line)
        if match and match.group(1) in wanted:
            key = match.group(1); output.append(f"{key}={wanted[key]}"); seen.add(key)
        else: output.append(line)
    output.extend(f"{key}={value}" for key, value in sorted(wanted.items()) if key not in seen)
    config.write_text("\n".join(output) + "\n")


def verify_config(path, report, baseline=None, source=None):
    text = Path(path).read_text()
    values = dict(re.findall(r"^(CONFIG_[A-Za-z0-9_]+)=([ym])$", text, re.M))
    required = {"CONFIG_MODULES", "CONFIG_BLOCK", "CONFIG_BLK_DEV_INITRD",
                "CONFIG_DEVTMPFS", "CONFIG_UNIX", "CONFIG_PRINTK",
                "CONFIG_RD_GZIP", "CONFIG_RD_ZSTD",
                "CONFIG_" + FS_CONFIG[report.root_filesystem]}
    if report.architecture in ("x86_64", "amd64"):
        required.update({"CONFIG_X86_64", "CONFIG_ACPI", "CONFIG_PCI",
                         "CONFIG_EFI", "CONFIG_EFI_STUB", "CONFIG_VT_CONSOLE",
                         "CONFIG_FRAMEBUFFER_CONSOLE"})
    if baseline:
        baseline_values = _config_values(baseline)
        required.update("CONFIG_" + symbol for symbol in BOOT_BASELINE_SYMBOLS
                        if baseline_values.get(symbol) == "y")
    missing = sorted(key for key in required if values.get(key) != "y")
    if baseline:
        mismatched = sorted(
            "CONFIG_" + symbol for symbol in BOOT_BASELINE_SYMBOLS
            if baseline_values.get(symbol) in ("y", "m")
            and values.get("CONFIG_" + symbol) != baseline_values[symbol]
        )
        missing = sorted(set(missing + mismatched))
    if missing:
        raise RuntimeError("critical settings were rejected by Kconfig: " + ", ".join(missing))

    verify_containers(path, report, baseline, source)
    if source and baseline:
        verify_network(path, report, baseline, source)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("scan", "lsmod", "apply", "verify"))
    parser.add_argument("path", nargs="?")
    parser.add_argument("--baseline")
    parser.add_argument("--source")
    parser.add_argument("--report", help="Read a saved hardware scan")
    parser.add_argument("--save-report", help="Save the hardware scan for all build stages")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = (HardwareReport(**json.loads(Path(args.report).read_text()))
              if args.report else Scanner().scan())
    if args.save_report:
        Path(args.save_report).write_text(json.dumps(asdict(report), indent=2))
    if args.action == "scan": print(json.dumps(asdict(report), indent=2) if args.json else render_report(report))
    elif not report.safe:
        raise SystemExit("Hardware optimisation refused: " + report.refusal_reason)
    elif args.action == "lsmod":
        print("Module Size Used by")
        for module in report.modules: print(f"{module} 0 0")
    elif args.action == "apply":
        if not args.path: parser.error("apply requires a config path")
        update_config(args.path, report, args.baseline, args.source)
    elif args.action == "verify":
        if not args.path: parser.error("verify requires a config path")
        try: verify_config(args.path, report, args.baseline, args.source)
        except RuntimeError as exc: raise SystemExit(f"Hardware optimisation refused: {exc}")


if __name__ == "__main__": main()
