"""Real Linux 7.2.6 configuration only; never invoke a kernel build/install."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import hardware_optimizer as hw

FIXTURES = Path(__file__).parent / 'fixtures'
SOURCE = Path(os.environ.get('KERNEL_SOURCE_726', '/home/adrian/kernel-build/linux-7.2.6'))
CONF = Path(os.environ.get('KERNEL_KCONFIG_CONF', str(SOURCE / 'scripts/kconfig/conf')))
GENERIC = Path(os.environ.get('KERNEL_GENERIC_CONFIG', '/boot/config-7.0.0-34-generic'))


class TargetPlatformTests(unittest.TestCase):
    def test_both_machines_platform_and_docker_regression(self):
        if not CONF.is_file() or not (SOURCE / 'Kconfig').is_file() or not GENERIC.is_file():
            self.skipTest('Set KERNEL_SOURCE_726, KERNEL_KCONFIG_CONF and KERNEL_GENERIC_CONFIG for native validation')
        results = {}
        for machine in ('acer', 'sandy_bridge'):
            with self.subTest(machine=machine), tempfile.TemporaryDirectory(prefix='platform-config-') as tmp:
                root = Path(tmp)
                config, baseline = root / '.config', root / 'baseline.config'
                report = hw.HardwareReport(**json.loads((FIXTURES / machine / 'hardware.json').read_text()))
                if machine == 'sandy_bridge':
                    # Original saved scan predates the all-bus inventory field.
                    report.hardware_devices = [
                        {'device': 'Sandy Bridge Intel display', 'drivers': ['i915']},
                        {'device': 'Sandy Bridge AHCI storage', 'drivers': ['ahci']}]
                start = GENERIC if machine == 'acer' else FIXTURES / machine / 'working.config'
                shutil.copyfile(start, config)
                env = dict(os.environ, KCONFIG_CONFIG=str(config), srctree=str(SOURCE), objtree=tmp,
                           ARCH='x86', SRCARCH='x86', CC='gcc', LD='ld', HOSTCC='gcc',
                           RUSTC='rustc', PAHOLE_VERSION='0')
                def run(args):
                    p = subprocess.run(args, cwd=root, env=env, stdin=subprocess.DEVNULL,
                                       capture_output=True, text=True, timeout=120)
                    self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                    self.assertNotIn('Error in reading', p.stdout + p.stderr)
                    self.assertNotIn('(NEW)', p.stdout)
                    return p
                def resolve():
                    run([str(CONF), '--olddefconfig', str(SOURCE / 'Kconfig')])
                def counts(path):
                    values = hw._config_values(path)
                    return {k: sum(v == k for v in values.values()) for k in ('y', 'm')}
                resolve()
                shutil.copyfile(config, baseline)
                lsmod = root / 'lsmod'
                lsmod.write_text('Module Size Used by\n' + ''.join(m + ' 0 0\n' for m in report.modules))
                env['LSMOD'] = str(lsmod)
                pruned = run(['perl', str(SOURCE / 'scripts/kconfig/streamline_config.pl'),
                              '--localmodconfig', str(SOURCE), 'Kconfig'])
                config.write_text(pruned.stdout)
                resolve()
                pruned_counts = counts(config)
                hw.update_config(config, report, baseline, SOURCE)
                resolve()
                hw.verify_config(config, report, baseline, SOURCE)
                values = hw._config_values(config)
                hardware = ('R8169', 'MT7921E', 'BLK_DEV_NVME', 'DRM_I915') if machine == 'acer' else ('TIGON3', 'ATH9K', 'SATA_AHCI', 'DRM_I915')
                for symbol in hardware + ('FUSE_FS', 'TUN', 'USB_STORAGE', 'USB_PRINTER', 'OVERLAY_FS', 'VETH', 'NETFILTER_XT_MATCH_ADDRTYPE'):
                    self.assertIn(values.get(symbol), ('y', 'm'), symbol)
                for symbol in ('CHELSIO_LIB', 'QED', 'DRM_AMDGPU', 'SND_HDSP', 'MEGARAID_SAS', 'NVME_FC'):
                    self.assertNotIn(values.get(symbol), ('y', 'm'), symbol)
                results[machine] = dict(input=counts(start), normalized=counts(baseline),
                                        pruned=pruned_counts, final=counts(config))
                self.assertLess(counts(config)['m'], counts(baseline)['m'])
                if machine == 'acer':
                    groups = hw.software_requirements(report, baseline, SOURCE)
                    docker = groups['Docker']
                    results[machine]['docker'] = {k: values[k] for k in sorted(docker)}
                    # The actual Acer defect must be rejected after native Kconfig.
                    good = config.read_text()
                    config.write_text(re.sub(r'^CONFIG_NETFILTER_XT_MATCH_ADDRTYPE=.*$',
                                             '# CONFIG_NETFILTER_XT_MATCH_ADDRTYPE is not set', good, flags=re.M))
                    resolve()
                    with self.assertRaisesRegex(RuntimeError, 'Docker compatibility failed.*ADDRTYPE'):
                        hw.verify_config(config, report, baseline, SOURCE)
                    config.write_text(good)
                    resolve()
                    hw.verify_config(config, report, baseline, SOURCE)
                    # Exercise every supported profile through actual target
                    # Kconfig, including software absent from this machine.
                    report.software = {name: ['fixture: installed but stopped']
                                       for name in hw.SOFTWARE_PROFILES}
                    hw.update_config(config, report, baseline, SOURCE)
                    resolve()
                    hw.verify_config(config, report, baseline, SOURCE)
                    all_profiles = hw._config_values(config)
                    for symbol in ('NFS_FS', 'NFS_V3', 'NFS_V4', 'NFSD', 'NFSD_V4',
                                   'CIFS', 'FUSE_FS', 'KVM', 'KVM_INTEL', 'WIREGUARD'):
                        self.assertIn(all_profiles.get(symbol), ('y', 'm'), symbol)
        print('PLATFORM_CONFIG_RESULTS=' + json.dumps(results, sort_keys=True), flush=True)
