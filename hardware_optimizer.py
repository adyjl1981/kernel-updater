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
    "MEDIA_USB_SUPPORT", "USB_VIDEO_CLASS", "USB_PRINTER", "USB_SERIAL",
    "USB_SERIAL_GENERIC", "USB_SERIAL_FTDI_SIO", "USB_SERIAL_PL2303",
    "USB_SERIAL_CP210X", "USB_ACM",
    # Normal desktop networking, Wi-Fi, printing and display connectors
    "NETDEVICES", "ETHERNET", "WLAN", "CFG80211", "MAC80211", "RFKILL",
    "PACKET", "UNIX", "NETFILTER", "BRIDGE", "VLAN_8021Q", "MDNS_RESOLVER",
    "USB_USBNET", "USB_NET_CDCETHER", "USB_NET_RNDIS_HOST", "PPP",
    "DRM", "DRM_KMS_HELPER", "DRM_DISPLAY_HELPER", "I2C", "I2C_ALGOBIT",
    "TYPEC", "USB_TYPEC", "THUNDERBOLT",
    # Common removable-drive filesystems and character sets
    "FAT_FS", "VFAT_FS", "EXFAT_FS", "NTFS3_FS", "FUSE_FS", "NLS",
    "NLS_CODEPAGE_437", "NLS_ISO8859_1", "NLS_UTF8",
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
                              sorted(modules), categories, warnings, not reason, reason)


def render_report(r):
    lines = [f"CPU: {r.cpu}", f"Architecture: {r.architecture}",
             f"Root: {r.root_source or 'unknown'} ({r.root_filesystem or 'unknown'})",
             f"Detected driver modules: {', '.join(r.modules) or 'none'}", ""]
    for name, items in r.categories.items():
        lines.append(f"{name}:" + ("\n  " + "\n  ".join(items) if items else " none detected"))
    lines += ["", "Compatibility retained:",
              "  USB hosts/hubs, HID/input, storage, Bluetooth, networking/Wi-Fi,",
              "  printers, USB audio/webcams/serial, removable media/filesystems,",
              "  optical media and graphics display infrastructure.", "",
              "Optimised build readiness: " + ("READY" if r.safe else "REFUSED — " + r.refusal_reason)]
    if r.warnings: lines += ["", "Scan notes:", *["  " + x for x in r.warnings]]
    return "\n".join(lines)


def update_config(path, report):
    """Apply required values without touching an installed kernel config."""
    config = Path(path)
    values = {x: "y" for x in SAFETY_BUILTIN}
    values.update({x: "m" for x in SAFETY_MODULES})
    values[FS_CONFIG[report.root_filesystem]] = "y"
    for fs in report.boot_filesystems:
        if fs in FS_CONFIG: values[FS_CONFIG[fs]] = "y"
    lines = config.read_text().splitlines()
    wanted = {f"CONFIG_{k}": v for k, v in values.items()}
    output, seen = [], set()
    pattern = re.compile(r"^(?:# )?(CONFIG_[A-Z0-9_]+)(?:=| is not set)")
    for line in lines:
        match = pattern.match(line)
        if match and match.group(1) in wanted:
            key = match.group(1); output.append(f"{key}={wanted[key]}"); seen.add(key)
        else: output.append(line)
    output.extend(f"{key}={value}" for key, value in sorted(wanted.items()) if key not in seen)
    config.write_text("\n".join(output) + "\n")


def verify_config(path, report):
    text = Path(path).read_text()
    values = dict(re.findall(r"^(CONFIG_[A-Z0-9_]+)=([ym])$", text, re.M))
    required = {"CONFIG_MODULES", "CONFIG_BLOCK", "CONFIG_BLK_DEV_INITRD",
                "CONFIG_DEVTMPFS", "CONFIG_" + FS_CONFIG[report.root_filesystem]}
    missing = sorted(key for key in required if values.get(key) != "y")
    if missing:
        raise RuntimeError("critical settings were rejected by Kconfig: " + ", ".join(missing))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("scan", "lsmod", "apply", "verify"))
    parser.add_argument("path", nargs="?")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = Scanner().scan()
    if args.action == "scan": print(json.dumps(asdict(report), indent=2) if args.json else render_report(report))
    elif not report.safe:
        raise SystemExit("Hardware optimisation refused: " + report.refusal_reason)
    elif args.action == "lsmod":
        print("Module Size Used by")
        for module in report.modules: print(f"{module} 0 0")
    elif args.action == "apply":
        if not args.path: parser.error("apply requires a config path")
        update_config(args.path, report)
    elif args.action == "verify":
        if not args.path: parser.error("verify requires a config path")
        try: verify_config(args.path, report)
        except RuntimeError as exc: raise SystemExit(f"Hardware optimisation refused: {exc}")


if __name__ == "__main__": main()
