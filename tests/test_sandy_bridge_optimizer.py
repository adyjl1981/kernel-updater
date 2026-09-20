"""Regression of the real i3-2310M/tg3/ath9k configuration failure.

All generated configs and Kconfig outputs stay in temporary directories.
No test compiles a kernel, installs anything, or touches GRUB.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import hardware_optimizer as hw

FIXTURE = Path(__file__).parent / 'fixtures/sandy_bridge'
SOURCE = Path('/home/adrian/kernel-build/linux-7.2.6')
PROJECT = Path(__file__).resolve().parents[1]


def native_conf(test):
    candidates = list(Path('/home/adrian/kernel-build').glob('linux-*/scripts/kconfig/conf'))
    if not candidates:
        test.skipTest('Target Kconfig conf executable unavailable')
    return candidates[0]


class SandyBridgeRequirementsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        shutil.copytree(FIXTURE / 'drivers', self.root / 'drivers')
        self.report = hw.HardwareReport(**json.loads((FIXTURE / 'hardware.json').read_text()))
        self.baseline = self.root / 'baseline.config'
        self.config = self.root / '.config'
        # Include all deliberately retained compatibility definitions, but
        # keep unrelated networking options in the fixture's network file.
        network = (self.root / 'drivers/net/Kconfig').read_text()
        defined = set(re.findall(r'^config (\w+)', network, re.M))
        names = hw.SAFETY_BUILTIN | hw.SAFETY_MODULES | {
            'X86_64', 'EXT4_FS', 'HAS_DMA', 'PCMCIA', 'DCA', 'QED',
            'REGMAP_SOUNDWIRE', 'SOUNDWIRE', 'MT792x_LIB', 'EXTERNAL_HELPER'}
        text = ''
        for name in sorted(names - defined):
            kind = 'bool' if name in hw.SAFETY_BUILTIN | {'X86_64', 'HAS_DMA'} else 'tristate'
            text += f'config {name}\n {kind} "{name}"\n'
            if name == 'MODULES':
                text += ' modules\n'
        (self.root / 'Kconfig').write_text(text + 'source "drivers/net/Kconfig"\n')
        values = hw._config_values(FIXTURE / 'working.config')
        values.update({'LEDS_CLASS': 'm', 'REGMAP': 'y', 'LAST_NETWORK_ENTRY': 'y'})
        self.baseline.write_text(''.join(f'CONFIG_{k}={v}\n' for k, v in values.items()
                                         if k in names | defined))
        self.config.write_text('')

    def test_actual_inventory_and_driver_mapping(self):
        self.assertTrue(self.report.safe)
        self.assertIn('i3-2310M', self.report.cpu)
        self.assertTrue(self.report.root_source.startswith('/dev/mapper/'))
        mapping = hw.network_driver_symbols(self.root)
        self.assertEqual(mapping['tg3'], {'TIGON3'})
        self.assertEqual(mapping['ath9k'], {'ATH9K'})
        self.assertNotIn('pci', mapping)  # A composite object is not a module.
        required = hw.network_requirements(self.report, self.baseline, self.root)
        for name in ('TIGON3', 'PHYLIB', 'ATH9K', 'ATH9K_HW', 'ATH9K_COMMON',
                     'ATH_COMMON', 'MAC80211', 'CFG80211', 'NET_VENDOR_BROADCOM'):
            self.assertIn(name, required)
        self.assertEqual(required['ATH9K_PCI'], 'y')

    def test_unrelated_distribution_options_are_not_network_requirements(self):
        required = hw.network_requirements(self.report, self.baseline, self.root)
        for name in ('CAN_EMS_PCMCIA', 'PCMCIA', 'CHELSIO_LIB', 'DCA', 'QED_RDMA',
                     'SUNRPC', 'SOUNDWIRE', 'REGMAP_SOUNDWIRE', 'SND_SOC_SDCA',
                     'PTP_1588_CLOCK', 'LEDS_CLASS', 'MAC80211_LEDS'):
            with self.subTest(symbol=name):
                self.assertNotIn(name, required)
        hw.update_config(self.config, self.report, self.baseline, self.root)
        values = hw._config_values(self.config)
        self.assertNotIn('CAN_EMS_PCMCIA', values)
        self.assertNotIn('SUNRPC', values)
        hw.verify_config(self.config, self.report, self.baseline, self.root)

    def test_defaults_implies_and_next_menu_are_not_edges(self):
        required = hw.capability_closure(self.baseline, self.root, {'REGMAP', 'LAST_NETWORK_ENTRY'})
        self.assertEqual(set(required), {'REGMAP', 'LAST_NETWORK_ENTRY'})
        for expression in ('FOO || !FOO', 'FOO || BAR && BAZ', '!FOO', 'FOO = m'):
            self.assertEqual(hw._positive_conjunction(expression), set())

    def test_other_architecture_definitions_cannot_add_dependencies(self):
        foreign = self.root / 'arch/s390'
        foreign.mkdir(parents=True)
        (foreign / 'Kconfig').write_text('config PCI\n bool "PCI"\n select PFAULT\n'
                                       'config PFAULT\n bool\n')
        self.assertNotIn('PFAULT', hw.network_requirements(self.report, self.baseline, self.root))

    def test_mixed_case_and_target_boolean_types(self):
        with (self.root / 'drivers/net/Kconfig').open('a') as stream:
            stream.write('config TEST_ADAPTER\n tristate "Adapter"\n select MT792x_LIB\n'
                         'config MT792x_LIB\n tristate\n select EXTERNAL_HELPER\n')
        with self.baseline.open('a') as stream:
            stream.write('CONFIG_TEST_ADAPTER=m\nCONFIG_MT792x_LIB=m\n'
                         'CONFIG_EXTERNAL_HELPER=m\nCONFIG_ATH9K_PCI=m\n')
        required = hw.networking_baseline(self.baseline, self.root, {'TEST_ADAPTER', 'ATH9K_PCI'})
        self.assertEqual(required['MT792x_LIB'], 'm')
        self.assertEqual(required['EXTERNAL_HELPER'], 'm')
        self.assertEqual(required['ATH9K_PCI'], 'y')

    def test_helper_can_be_modular_when_builtin_consumer_was_pruned(self):
        with (self.root / 'drivers/net/Kconfig').open('a') as stream:
            stream.write('config TEST_ADAPTER\n tristate "Adapter"\n select MT792x_LIB\n'
                         'config MT792x_LIB\n tristate\n')
        with self.baseline.open('a') as stream:
            stream.write('CONFIG_TEST_ADAPTER=m\nCONFIG_MT792x_LIB=y\n')
        required = hw.capability_closure(self.baseline, self.root, {'TEST_ADAPTER'})
        self.assertEqual(required['TEST_ADAPTER'], 'm')
        self.assertEqual(required['MT792x_LIB'], 'm')

    def test_missing_ethernet_wifi_bus_or_lvm_is_rejected(self):
        hw.update_config(self.config, self.report, self.baseline, self.root)
        good = self.config.read_text()
        for name in ('TIGON3', 'ATH9K', 'ATH9K_PCI', 'ATH9K_HW', 'PHYLIB', 'BLK_DEV_DM', 'EXT4_FS'):
            with self.subTest(symbol=name):
                self.config.write_text(re.sub(rf'^CONFIG_{name}=.*$', f'# CONFIG_{name} is not set', good, flags=re.M))
                with self.assertRaisesRegex(RuntimeError, 'CONFIG_' + name):
                    hw.verify_config(self.config, self.report, self.baseline, self.root)

    def test_native_resolution_preserves_required_capabilities(self):
        conf = native_conf(self)
        hw.update_config(self.config, self.report, self.baseline, self.root)
        proc = subprocess.run([str(conf), '--olddefconfig', str(self.root / 'Kconfig')],
                              cwd=self.root, env=dict(os.environ, KCONFIG_CONFIG=str(self.config)),
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn('Error in reading', proc.stdout + proc.stderr)
        hw.verify_config(self.config, self.report, self.baseline, self.root)


class NonInteractiveConfigTests(unittest.TestCase):
    def test_new_symbols_use_native_defaults_with_closed_stdin(self):
        conf = native_conf(self)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kconfig = root / 'Kconfig'
            kconfig.write_text('''config MODULES
 bool "Modules"
 modules
 default y
config GPIO_BT8XX
 tristate "NEW GPIO"
 default n
config SND_SE6X
 tristate "NEW sound"
 default m
config NEW_BOOLEAN
 bool "NEW boolean"
 default y
config NEW_STRING
 string "NEW string"
 default "native default"
''')
            config = root / '.config'
            env = dict(os.environ, KCONFIG_CONFIG=str(config))
            config.write_text('CONFIG_MODULES=y\n')
            old = subprocess.run([str(conf), '--oldconfig', str(kconfig)], cwd=root, env=env,
                                 stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
            self.assertIn('Error in reading or end of file', old.stdout + old.stderr)
            config.write_text('CONFIG_MODULES=y\n')
            new = subprocess.run([str(conf), '--olddefconfig', str(kconfig)], cwd=root, env=env,
                                 stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
            self.assertEqual(new.returncode, 0, new.stderr)
            self.assertNotIn('(NEW)', new.stdout)
            self.assertNotIn('Error in reading', new.stdout + new.stderr)
            values = hw._config_values(config)
            self.assertNotIn('GPIO_BT8XX', values)
            self.assertEqual(values['SND_SE6X'], 'm')
            self.assertEqual(values['NEW_BOOLEAN'], 'y')
            self.assertEqual(values['NEW_STRING'], '"native default"')

    def test_build_script_never_uses_interactive_localmodconfig_target(self):
        script = (PROJECT / 'build-custom-kernel.sh').read_text()
        commands = [line.strip() for line in script.splitlines() if not line.lstrip().startswith('#')]
        self.assertFalse(any('make ' in line and ' localmodconfig' in line for line in commands))
        self.assertIn('perl scripts/kconfig/streamline_config.pl --localmodconfig . Kconfig', script)
        prune = script.index('perl scripts/kconfig/streamline_config.pl --localmodconfig')
        resolve = script.index('olddefconfig < /dev/null', prune)
        self.assertLess(resolve, script.index('hardware_optimizer.py" apply', prune))


class TargetSandyBridgeConfigTests(unittest.TestCase):
    def test_linux_726_clang_config_only(self):
        conf = SOURCE / 'scripts/kconfig/conf'
        if not conf.exists() or not shutil.which('clang') or not shutil.which('ld.lld'):
            self.skipTest('Linux 7.2.6 conf or Clang/LLD unavailable')
        report = hw.HardwareReport(**json.loads((FIXTURE / 'hardware.json').read_text()))
        with tempfile.TemporaryDirectory(prefix='kernel-sandy-config-') as tmp:
            root = Path(tmp)
            config, baseline = root / '.config', root / 'baseline.config'
            shutil.copyfile(FIXTURE / 'working.config', config)
            env = dict(os.environ, KCONFIG_CONFIG=str(config), srctree=str(SOURCE), objtree=tmp,
                       ARCH='x86', SRCARCH='x86', CC='clang', LD='ld.lld', HOSTCC='gcc',
                       LLVM='1', LLVM_IAS='1', CLANG_FLAGS='-fintegrated-as', RUSTC='rustc', PAHOLE_VERSION='0')
            def run(args):
                proc = subprocess.run(args, cwd=root, env=env, stdin=subprocess.DEVNULL,
                                      capture_output=True, text=True, timeout=90)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertNotIn('Error in reading', proc.stdout + proc.stderr)
                self.assertNotIn('(NEW)', proc.stdout)
                return proc
            def resolve():
                return run([str(conf), '--olddefconfig', str(SOURCE / 'Kconfig')])
            resolve()
            shutil.copyfile(config, baseline)
            lsmod = root / 'lsmod'
            lsmod.write_text('Module Size Used by\n' + ''.join(m + ' 0 0\n' for m in report.modules))
            env['LSMOD'] = str(lsmod)
            pruned = run(['perl', str(SOURCE / 'scripts/kconfig/streamline_config.pl'),
                          '--localmodconfig', str(SOURCE), 'Kconfig'])
            config.write_text(pruned.stdout)
            resolve()
            hw.update_config(config, report, baseline, SOURCE)
            resolve()
            hw.verify_config(config, report, baseline, SOURCE)
            values = hw._config_values(config)
            for name in ('TIGON3', 'ATH9K', 'ATH9K_HW', 'ATH9K_COMMON', 'ATH_COMMON',
                         'MAC80211', 'CFG80211', 'SATA_AHCI', 'DRM_I915', 'USB_EHCI_PCI', 'USB_XHCI_PCI'):
                self.assertIn(values.get(name), ('y', 'm'), name)
            for name in ('ATH9K_PCI', 'BLK_DEV_DM', 'BLK_DEV_DM_BUILTIN', 'EXT4_FS'):
                self.assertEqual(values.get(name), 'y', name)
            for name in ('CAN_EMS_PCMCIA', 'CHELSIO_LIB', 'NTB_NETDEV', 'QED', 'SUNRPC', 'SOUNDWIRE'):
                self.assertNotIn(values.get(name), ('y', 'm'), name)
            before = sum(v == 'm' for v in hw._config_values(baseline).values())
            after = sum(v == 'm' for v in values.values())
            self.assertLess(after, before // 2)
            mapping = hw.network_driver_symbols(SOURCE)
            self.assertEqual(mapping['tg3'], {'TIGON3'})
            self.assertEqual(mapping['ath9k'], {'ATH9K'})
            tg3_header = (SOURCE / 'drivers/net/ethernet/broadcom/tg3.h').read_text()
            self.assertRegex(tg3_header, r'TG3PCI_DEVICE_TIGON3_57785\s+0x16b5')
            tg3_source = (SOURCE / 'drivers/net/ethernet/broadcom/tg3.c').read_text()
            self.assertIn('PCI_DEVICE(PCI_VENDOR_ID_BROADCOM, TG3PCI_DEVICE_TIGON3_57785)', tg3_source)
            ath_pci = (SOURCE / 'drivers/net/wireless/ath/ath9k/pci.c').read_text()
            self.assertIn('PCI_VDEVICE(ATHEROS, 0x002E)', ath_pci)
            ath_make = (SOURCE / 'drivers/net/wireless/ath/ath9k/Makefile').read_text()
            self.assertRegex(ath_make, r'ath9k-\$\(CONFIG_ATH9K_PCI\)\s*\+=\s*pci.o')
            # Validate the existing container floor on this same target too.
            report.container_runtimes = {'Docker': ['fixture: installed']}
            hw.update_config(config, report, baseline, SOURCE)
            resolve()
            hw.verify_config(config, report, baseline, SOURCE)
