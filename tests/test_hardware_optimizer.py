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
    def test_usb_feature_gates_are_bool_with_modular_parents(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / ".config"
            config.write_text("CONFIG_MEDIA_USB_SUPPORT=m\nCONFIG_USB_SERIAL_GENERIC=m\n")
            report = hw.HardwareReport("arm64", "CPU", "/dev/x", "ext4")
            hw.update_config(config, report)
            values = hw._config_values(config)
            for symbol in ("MEDIA_USB_SUPPORT", "USB_SERIAL_GENERIC"):
                self.assertEqual(values[symbol], "y")
            for symbol in ("MEDIA_SUPPORT", "USB_SERIAL"):
                self.assertEqual(values[symbol], "m")

    def test_usb_bool_gates_survive_kconfig_and_respect_dependencies(self):
        import os
        import subprocess
        candidates = list(Path('/home/adrian/kernel-build').glob('linux-*/scripts/kconfig/conf'))
        if not candidates:
            self.skipTest('Kconfig conf executable unavailable')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / '.config'
            config.write_text('')
            hw.update_config(config, hw.HardwareReport("arm64", "CPU", "/dev/x", "ext4"))
            wanted = hw._config_values(config)
            names = ('MODULES', 'USB', 'TTY', 'MEDIA_SUPPORT', 'USB_SERIAL',
                     'MEDIA_USB_SUPPORT', 'USB_SERIAL_GENERIC')
            correct = ''.join(f'CONFIG_{k}={wanted.get(k, "y")}\n' for k in names)
            kconfig = root / 'Kconfig'
            # Mirror the 7.2.6 types and enclosing parent conditions.
            kconfig.write_text('''config MODULES
 bool "Modules"
 modules
config USB
 tristate "USB"
config TTY
 bool "TTY"
config MEDIA_SUPPORT
 tristate "Media"
if USB && MEDIA_SUPPORT
config MEDIA_USB_SUPPORT
 bool "Media USB adapters"
endif
if USB
config USB_SERIAL
 tristate "USB serial"
 depends on TTY
if USB_SERIAL
config USB_SERIAL_GENERIC
 bool "Generic serial"
endif
endif
''')
            env = dict(os.environ, KCONFIG_CONFIG=str(config))
            def resolve():
                return subprocess.run([str(candidates[0]), '--olddefconfig', str(kconfig)],
                                      cwd=root, env=env, check=True, capture_output=True, text=True)
            config.write_text(correct.replace('CONFIG_MEDIA_USB_SUPPORT=y', 'CONFIG_MEDIA_USB_SUPPORT=m')
                              .replace('CONFIG_USB_SERIAL_GENERIC=y', 'CONFIG_USB_SERIAL_GENERIC=m'))
            rejected = resolve()
            for symbol in ('MEDIA_USB_SUPPORT', 'USB_SERIAL_GENERIC'):
                self.assertIn("symbol value 'm' invalid for " + symbol, rejected.stderr)
                self.assertNotIn(symbol, hw._config_values(config))
            config.write_text(correct)
            self.assertEqual(resolve().stderr, '')
            values = hw._config_values(config)
            for symbol in ('MEDIA_USB_SUPPORT', 'USB_SERIAL_GENERIC'):
                self.assertEqual(values[symbol], 'y')
            for symbol in ('MEDIA_SUPPORT', 'USB_SERIAL'):
                self.assertEqual(values[symbol], 'm')
            config.write_text(correct.replace('CONFIG_USB=y', '# CONFIG_USB is not set'))
            resolve()
            for symbol in ('MEDIA_USB_SUPPORT', 'USB_SERIAL_GENERIC'):
                self.assertNotIn(symbol, hw._config_values(config))

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

class NetworkRetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'drivers/net').mkdir(parents=True)
        (self.root / 'drivers/net/Kconfig').write_text('''config NETDEVICES
 bool "Network devices"
config ETHERNET
 bool "Ethernet"
 depends on NETDEVICES
config WLAN
 bool "Wireless"
 depends on NETDEVICES
config OTHER_VENDOR_PCI
 tristate "An arbitrary vendor adapter"
 depends on WLAN && PCI
 select MT792x_LIB
config MT792x_LIB
 tristate
 select EXTERNAL_HELPER
''')
        (self.root / 'Kconfig').write_text('''config PCI
 bool "PCI"
config EXTERNAL_HELPER
 tristate
''')
        (self.root / 'drivers/net/Makefile').write_text(
            'obj-$(CONFIG_OTHER_VENDOR_PCI) += arbitrary-pci.o\n')
        self.baseline = self.root / 'working.config'
        self.baseline.write_text('CONFIG_NETDEVICES=y\nCONFIG_ETHERNET=y\n'
                                 'CONFIG_WLAN=y\nCONFIG_PCI=y\n'
                                 'CONFIG_OTHER_VENDOR_PCI=m\nCONFIG_MT792x_LIB=m\n'
                                 'CONFIG_EXTERNAL_HELPER=m\n')
        self.config = self.root / '.config'
        self.config.write_text('# CONFIG_NETDEVICES is not set\n')
        self.report = hw.HardwareReport('arm64', 'CPU', '/dev/x', 'ext4',
            network_devices=[{'device': 'test PCI adapter', 'drivers': ['arbitrary_pci']}])

    def test_working_network_dependency_chain_restored(self):
        hw.update_config(self.config, self.report, self.baseline, self.root)
        values = hw._config_values(self.config)
        for symbol, value in hw._config_values(self.baseline).items():
            self.assertEqual(values[symbol], value)
        hw.verify_config(self.config, self.report, self.baseline, self.root)

    def test_bool_safety_options_are_not_requested_as_modules(self):
        hw.update_config(self.config, self.report)
        values = hw._config_values(self.config)
        for symbol in ('NETDEVICES', 'ETHERNET', 'WLAN', 'WIRELESS', 'NETFILTER'):
            self.assertEqual(values[symbol], 'y')

    def test_post_kconfig_driver_loss_refuses_build(self):
        hw.update_config(self.config, self.report, self.baseline, self.root)
        self.config.write_text(self.config.read_text().replace(
            'CONFIG_OTHER_VENDOR_PCI=m', '# CONFIG_OTHER_VENDOR_PCI is not set'))
        with self.assertRaisesRegex(RuntimeError, 'test PCI adapter'):
            hw.verify_config(self.config, self.report, self.baseline, self.root)

    def test_unresolved_detected_device_fails_closed(self):
        hw.update_config(self.config, self.report, self.baseline, self.root)
        self.report.network_devices[0]['drivers'] = []
        with self.assertRaisesRegex(RuntimeError, 'test PCI adapter'):
            hw.verify_config(self.config, self.report, self.baseline, self.root)

    def test_dependency_demotion_is_rejected(self):
        hw.update_config(self.config, self.report, self.baseline, self.root)
        self.config.write_text(self.config.read_text().replace('CONFIG_PCI=y', 'CONFIG_PCI=m'))
        with self.assertRaisesRegex(RuntimeError, 'CONFIG_PCI'):
            hw.verify_config(self.config, self.report, self.baseline, self.root)

    def test_actual_kconfig_rejects_old_module_value_for_bool(self):
        import os
        import subprocess
        candidates = list(Path('/home/adrian/kernel-build').glob('linux-*/scripts/kconfig/conf'))
        if not candidates:
            self.skipTest('Kconfig conf executable unavailable')
        kconfig = self.root / 'minimal.Kconfig'
        kconfig.write_text('''config MODULES
 bool "Modules"
 modules
 default y
source "drivers/net/Kconfig"
source "Kconfig"
''')
        self.config.write_text(self.baseline.read_text().replace('CONFIG_NETDEVICES=y', 'CONFIG_NETDEVICES=m'))
        env = dict(os.environ, KCONFIG_CONFIG=str(self.config))
        subprocess.run([str(candidates[0]), '--olddefconfig', str(kconfig)],
                       cwd=self.root, env=env, check=True, capture_output=True)
        self.assertNotIn('OTHER_VENDOR_PCI', hw._config_values(self.config))
        hw.update_config(self.config, self.report, self.baseline, self.root)
        subprocess.run([str(candidates[0]), '--olddefconfig', str(kconfig)],
                       cwd=self.root, env=env, check=True, capture_output=True)
        hw.verify_network(self.config, self.report, self.baseline, self.root)


class NetworkInventoryTests(unittest.TestCase):
    fixture = HardwareScannerTests.fixture

    @mock.patch.object(hw.shutil, "which", return_value="/fixture/tool")
    def test_unbound_pci_adapter_retains_candidate_module(self, unused):
        scanner = self.fixture({
            ("lspci", "-nnk"): "03:00.0 Network controller [0280]: Example [1234:5678]\n"
                                  "\tKernel modules: arbitrary_pci\n",
        }, with_devices=False)
        report = scanner.scan()
        self.assertEqual(report.network_devices[0]["drivers"], ["arbitrary_pci"])
        self.assertIn("arbitrary_pci", report.modules)

    @mock.patch.object(hw.shutil, "which", return_value="/fixture/tool")
    def test_sysfs_network_device_without_driver_is_still_recorded(self, unused):
        scanner = self.fixture({}, with_devices=False)
        device = scanner.sys / 'bus/pci/devices/0000:03:00.0'
        device.mkdir()
        (device / 'class').write_text('0x028000')
        report = scanner.scan()
        self.assertEqual(len(report.network_devices), 1)
        self.assertEqual(report.network_devices[0]['drivers'], [])


if __name__ == "__main__":
    unittest.main()
