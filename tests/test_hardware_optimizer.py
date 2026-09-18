import tempfile
import unittest
from pathlib import Path
from unittest import mock

import hardware_optimizer as hw


class HardwareScannerTests(unittest.TestCase):
    def fixture(self, outputs, with_devices=True):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "proc").mkdir(); (root / "sys/bus/pci/devices").mkdir(parents=True)
        (root / "sys/bus/usb/devices").mkdir(parents=True)
        (root / "boot").mkdir()
        (root / "proc/cpuinfo").write_text("model name : Test CPU\n")
        if with_devices:
            modules = root / "sys/module"
            modules.mkdir()
            for address, name in (("0000:00:17.0", "ahci"), ("0000:03:00.0", "iwlwifi")):
                target = modules / name; target.mkdir()
                device = root / "sys/bus/pci/devices" / address
                (device / "driver").mkdir(parents=True)
                (device / "driver/module").symlink_to(target)
        def runner(args):
            return outputs.get(tuple(args), "")
        scanner = hw.Scanner(root / "sys", root / "proc", root / "boot", runner)
        return scanner

    @mock.patch.object(hw.shutil, "which", return_value="/fixture/tool")
    def test_detects_loaded_and_unloaded_sysfs_hardware(self, unused):
        scanner = self.fixture({
            ("findmnt", "-nro", "SOURCE,FSTYPE", "/"): "/dev/sda2 ext4\n",
            ("findmnt", "-nro", "TARGET,FSTYPE"): "/boot/efi vfat\n",
            ("lspci", "-nnk"): "00:17.0 SATA controller\n03:00.0 Network controller\n",
            ("lsusb",): "Bus 001 Device 002: USB keyboard\n",
            ("lsmod",): "Module Size Used by\nahci 1 0\n",
        })
        report = scanner.scan()
        self.assertTrue(report.safe)
        self.assertEqual(report.root_filesystem, "ext4")
        self.assertIn("iwlwifi", report.modules)  # bound but not in lsmod fixture
        self.assertTrue(report.categories["USB controllers/devices"])

    @mock.patch.object(hw.shutil, "which", return_value=None)
    def test_missing_tools_refuses_incomplete_scan(self, unused):
        report = self.fixture({}, with_devices=False).scan()
        self.assertFalse(report.safe)
        self.assertIn("Standard", report.refusal_reason)
        self.assertTrue(any("findmnt" in warning for warning in report.warnings))

    @mock.patch.object(hw.shutil, "which", return_value="/fixture/tool")
    def test_unknown_root_filesystem_refuses_optimisation(self, unused):
        report = self.fixture({
            ("findmnt", "-nro", "SOURCE,FSTYPE", "/"): "/dev/x mysteryfs\n",
            ("lsmod",): "Module Size Used by\nnvme 1 0\n",
        }, with_devices=False).scan()
        self.assertFalse(report.safe)
        self.assertIn("mysteryfs", report.refusal_reason)


class ConfigSafetyTests(unittest.TestCase):
    def test_compatibility_and_boot_features_are_restored(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / ".config"
            config.write_text("# CONFIG_USB_STORAGE is not set\n# CONFIG_BT is not set\n# CONFIG_EXT4_FS is not set\n")
            report = hw.HardwareReport("x86_64", "CPU", "/dev/nvme0n1p2", "ext4", ["vfat"], modules=["nvme"], safe=True)
            hw.update_config(config, report)
            text = config.read_text()
            for setting in ("CONFIG_USB_STORAGE=m", "CONFIG_BT=m", "CONFIG_USB_PRINTER=m",
                            "CONFIG_EXFAT_FS=m", "CONFIG_NTFS3_FS=m", "CONFIG_EXT4_FS=y",
                            "CONFIG_VFAT_FS=y", "CONFIG_BLK_DEV_INITRD=y",
                            "CONFIG_UNIX=y", "CONFIG_RD_ZSTD=y"):
                self.assertIn(setting, text)

    def test_working_early_boot_choices_survive_localmodconfig(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / ".config"
            baseline = Path(tmp) / "working.config"
            # This reproduces the 7.2.6 omission: localmodconfig disabled UNIX
            # and demoted the boot display stack despite the working config.
            config.write_text("# CONFIG_UNIX is not set\nCONFIG_DRM_SIMPLEDRM=m\n"
                              "# CONFIG_BLK_DEV_DM is not set\n")
            baseline.write_text("CONFIG_UNIX=y\nCONFIG_DRM_SIMPLEDRM=y\n"
                                "CONFIG_BLK_DEV_DM=y\nCONFIG_RD_ZSTD=y\n")
            report = hw.HardwareReport("x86_64", "CPU", "/dev/nvme0n1p5",
                                       "ext4", modules=["nvme"], safe=True)
            hw.update_config(config, report, baseline)
            text = config.read_text()
            for setting in ("CONFIG_UNIX=y", "CONFIG_DRM_SIMPLEDRM=y",
                            "CONFIG_BLK_DEV_DM=y", "CONFIG_RD_ZSTD=y"):
                self.assertIn(setting, text)

    def test_validation_refuses_unix_socket_support_omission(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / ".config"
            config.write_text("CONFIG_MODULES=y\nCONFIG_BLOCK=y\nCONFIG_BLK_DEV_INITRD=y\n"
                              "CONFIG_DEVTMPFS=y\nCONFIG_PRINTK=y\nCONFIG_RD_GZIP=y\n"
                              "CONFIG_RD_ZSTD=y\nCONFIG_EXT4_FS=y\n")
            report = hw.HardwareReport("arm64", "CPU", "/dev/x", "ext4",
                                       modules=["test"], safe=True)
            with self.assertRaisesRegex(RuntimeError, "CONFIG_UNIX"):
                hw.verify_config(config, report)

    def test_report_explains_broad_retained_categories(self):
        report = hw.HardwareReport("x86_64", "CPU", refusal_reason="incomplete")
        text = hw.render_report(report)
        for word in ("Bluetooth", "printers", "webcams", "removable", "REFUSED"):
            self.assertIn(word, text)

    def test_validation_refuses_dropped_critical_boot_setting(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / ".config"
            config.write_text("CONFIG_MODULES=y\nCONFIG_BLOCK=y\nCONFIG_DEVTMPFS=y\nCONFIG_EXT4_FS=y\n")
            report = hw.HardwareReport("x86_64", "CPU", "/dev/sda2", "ext4", modules=["ahci"], safe=True)
            with self.assertRaisesRegex(RuntimeError, "BLK_DEV_INITRD"):
                hw.verify_config(config, report)


if __name__ == "__main__":
    unittest.main()
