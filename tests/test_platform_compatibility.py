"""Independent platform baseline, software evidence, and hardware validation."""
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import hardware_optimizer as hw


class SoftwareEvidenceTests(unittest.TestCase):
    def detect(self, binaries=(), outputs=None):
        outputs = outputs or {}
        scanner = hw.Scanner(runner=lambda args: outputs.get(tuple(args), ''),
                             etc_root='/nonexistent-kernel-manager-fixture')
        with mock.patch.object(hw.shutil, 'which', side_effect=lambda exe:
                               '/fixture/' + exe if exe in binaries else None):
            return scanner.detect_software([])

    def test_every_profile_detectable_by_executable_while_stopped(self):
        for name, (executables, _, _, _) in hw.SOFTWARE_PROFILES.items():
            with self.subTest(profile=name):
                self.assertIn(name, self.detect(executables[:1]))

    def test_enabled_failed_docker_does_not_query_running_state(self):
        calls = []
        def runner(args):
            calls.append(args)
            return 'enabled\n' if args[-1] == 'docker.service' else ''
        with mock.patch.object(hw.shutil, 'which', return_value='/fixture/tool'):
            self.assertIn('Docker', hw.Scanner(runner=runner).detect_software([]))
        self.assertFalse(any('is-active' in args or 'start' in args for args in calls))

    def test_rpm_pacman_apk_packages(self):
        for tool, args in [('rpm', ('rpm', '-qa', '--qf', '%{NAME}\n')),
                           ('pacman', ('pacman', '-Qq')), ('apk', ('apk', 'info'))]:
            with self.subTest(manager=tool):
                found = self.detect((tool,), {args: 'tailscale\nqemu-kvm\nfuse3\ncifs-utils\nnfs-utils\ncontainerd\n'})
                for name in ('Tailscale', 'QEMU/KVM', 'FUSE', 'Samba/CIFS', 'NFS', 'containerd'):
                    self.assertIn(name, found)

    def test_no_software(self):
        self.assertEqual(self.detect(), {})

    def test_configured_unmounted_network_and_fuse_filesystems(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'fstab').write_text('server:/share /mnt nfs4 defaults 0 0\n'
                                         '//server/share /srv cifs defaults 0 0\n'
                                         'remote /remote fuse.sshfs defaults 0 0\n')
            with mock.patch.object(hw.shutil, 'which', return_value=None):
                found = hw.Scanner(etc_root=tmp).detect_software([])
            self.assertEqual(set(found), {'NFS', 'Samba/CIFS', 'FUSE'})

    def test_acer_fixture_and_report(self):
        report = hw.HardwareReport(**json.loads((Path(__file__).parent / 'fixtures/acer/hardware.json').read_text()))
        self.assertIn('N4505', report.cpu)
        self.assertIn('Docker', report.software)
        self.assertIn('mt7921e', report.modules)
        self.assertIn('Installed software compatibility: Docker', hw.render_report(report))
        self.assertIn('General compatibility: containers', hw.render_report(report))


class PlatformRequirementTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.baseline = self.root / 'baseline'
        self.config = self.root / '.config'
        self.baseline.write_text('CONFIG_MODULES=y\nCONFIG_KVM_INTEL=m\n')
        self.config.write_text('')
        names = set().union(*hw.SOFTWARE_CAPABILITIES.values()) | hw.CONTAINER_VERSION_GATES | {'MODULES', 'KVM_INTEL', 'UNRELATED_GPU', 'DISPLAY', 'MT792x_LIB'}
        (self.root / 'Kconfig').write_text(''.join(f'config {n}\n tristate "{n}"\n' for n in sorted(names)))
        (self.root / 'drivers/net').mkdir(parents=True)
        (self.root / 'drivers/net/Kconfig').write_text('config NET\n bool "NET"\n')
        self.report = hw.HardwareReport('arm64', 'CPU', root_filesystem='ext4')

    def test_baseline_independent_of_software(self):
        groups = hw.compatibility_requirements(self.baseline, self.root)
        required = set().union(*(g.keys() for g in groups.values()))
        for name in ('VETH', 'OVERLAY_FS', 'NETFILTER_XT_MATCH_ADDRTYPE', 'FUSE_FS', 'BLK_DEV_LOOP', 'WIREGUARD', 'USB_STORAGE', 'USB_PRINTER', 'BT', 'SQUASHFS', 'SQUASHFS_XZ', 'UNIX98_PTYS', 'NETFILTER_XT_SET', 'IP_SET_HASH_NET'):
            self.assertIn(name, required)
        self.assertNotIn('UNRELATED_GPU', required)
        self.assertEqual(hw.software_requirements(self.report, self.baseline, self.root), {})

    def test_every_software_profile_has_independent_requirements(self):
        for name in hw.SOFTWARE_PROFILES:
            with self.subTest(profile=name):
                self.report.software = {name: ['installed but stopped']}
                groups = hw.software_requirements(self.report, self.baseline, self.root)
                self.assertTrue(groups[name])
                if name == 'QEMU/KVM':
                    self.assertIn('KVM_INTEL', groups[name])
                    self.assertNotIn('KVM_AMD', groups[name])

    def test_nfs_v3_server_legacy_gate_is_target_version_scoped(self):
        path = self.root / 'Kconfig'
        path.write_text(path.read_text().replace('config NFSD_V3\n', 'config HISTORICAL_NFSD_V3\n'))
        self.report.software = {'NFS': ['fstab filesystem: nfs4']}
        expected = hw.software_requirements(self.report, self.baseline, self.root)['NFS']
        self.assertIn('NFSD', expected)
        self.assertIn('NFSD_V4', expected)
        self.assertNotIn('NFSD_V3', expected)

    def test_virtualbox_does_not_require_kvm(self):
        self.report.software = {'VirtualBox': ['installed']}
        expected = hw.software_requirements(self.report, self.baseline, self.root)['VirtualBox']
        self.assertIn('MODULES', expected)
        self.assertNotIn('KVM', expected)
        self.assertNotIn('KVM_INTEL', expected)

    def test_known_cpu_restores_missing_kvm_backend(self):
        self.baseline.write_text('CONFIG_MODULES=y\n')
        self.report.architecture = 'x86_64'
        self.report.cpu = 'Intel Celeron N4505'
        self.report.software = {'QEMU/KVM': ['installed']}
        expected = hw.software_requirements(self.report, self.baseline, self.root)['QEMU/KVM']
        self.assertEqual(expected['KVM_INTEL'], 'm')
        self.config.write_text(''.join(f'CONFIG_{k}={v}\n' for k, v in expected.items()
                                      if k != 'KVM_INTEL'))
        with self.assertRaisesRegex(RuntimeError, 'QEMU/KVM.*CONFIG_KVM_INTEL'):
            hw.verify_capability_groups(self.config, 'Installed software', {'QEMU/KVM': expected})

    def test_software_without_target_source_fails_closed(self):
        self.report.software = {'FUSE': ['installed']}
        with self.assertRaisesRegex(RuntimeError, 'requires a baseline and target Kconfig'):
            hw.update_config(self.config, self.report)
        self.assertEqual(self.config.read_text(), '')

    def test_missing_core_baseline_symbol_fails_closed_without_software(self):
        path = self.root / 'Kconfig'
        path.write_text(path.read_text().replace('config VETH\n', 'config REMOVED_VETH\n'))
        with self.assertRaisesRegex(RuntimeError, 'General baseline.*CONFIG_VETH'):
            hw.compatibility_requirements(self.baseline, self.root)

    def test_docker_addrtype_failure_named_before_compilation(self):
        self.report.software = {'Docker': ['enabled; failed']}
        groups = hw.software_requirements(self.report, self.baseline, self.root)
        self.config.write_text(''.join(f'CONFIG_{k}={v}\n' for k,v in groups['Docker'].items()
                                      if k != 'NETFILTER_XT_MATCH_ADDRTYPE'))
        with self.assertRaisesRegex(RuntimeError, 'Docker compatibility failed.*CONFIG_NETFILTER_XT_MATCH_ADDRTYPE'):
            hw.verify_capability_groups(self.config, 'Installed software', groups)
        with self.config.open('a') as stream:
            stream.write('CONFIG_NETFILTER_XT_MATCH_ADDRTYPE=m\n')
        hw.verify_capability_groups(self.config, 'Installed software', groups)

    def test_baseline_rejected_capability(self):
        groups = hw.compatibility_requirements(self.baseline, self.root)
        expected = {k:v for g in groups.values() for k,v in g.items()}
        self.config.write_text(''.join(f'CONFIG_{k}={v}\n' for k,v in expected.items() if k != 'FUSE_FS'))
        with self.assertRaisesRegex(RuntimeError, 'General baseline / filesystems.*CONFIG_FUSE_FS'):
            hw.verify_capability_groups(self.config, 'General baseline', groups)

    def test_detected_display_dependency_and_unrelated_driver_removal(self):
        (self.root / 'drivers/Makefile').write_text('obj-$(CONFIG_DISPLAY) += display.o\nobj-$(CONFIG_UNRELATED_GPU) += unused.o\n')
        with (self.root / 'Kconfig').open('a') as stream:
            stream.write('config DISPLAY\n tristate "display"\n select MT792x_LIB\n')
        self.report.hardware_devices = [{'device': 'test display', 'drivers': ['display']}]
        groups = hw.hardware_requirements(self.report, self.baseline, self.root)
        self.assertEqual(set(groups['test display']), {'DISPLAY', 'MT792x_LIB'})
        with self.assertRaisesRegex(RuntimeError, 'Detected hardware / test display'):
            hw.verify_capability_groups(self.config, 'Detected hardware', groups)

    def test_missing_hardware_provider_fails_closed(self):
        self.report.hardware_devices = [{'device': 'unknown GPU', 'drivers': ['unknown']}]
        with self.assertRaisesRegex(RuntimeError, 'unknown GPU'):
            hw.hardware_requirements(self.report, self.baseline, self.root)
