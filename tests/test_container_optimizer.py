"""Container regressions, including the two installed September 19 configs."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest import mock

import hardware_optimizer as hw

FIXTURES = Path(__file__).parent / 'fixtures/container'


class RuntimeDetectionTests(unittest.TestCase):
    def detect(self, binaries=(), outputs=None):
        outputs = outputs or {}
        scanner = hw.Scanner(runner=lambda args: outputs.get(tuple(args), ''))
        with mock.patch.object(hw.shutil, 'which', side_effect=lambda exe:
                               '/test/' + exe if exe in (*binaries, 'dpkg-query', 'systemctl') else None):
            return scanner.detect_container_runtimes([])

    def test_local_runtime_executables_even_when_daemon_stopped(self):
        for name, (binaries, _, _) in hw.RUNTIMES.items():
            with self.subTest(name=name):
                self.assertIn(name, self.detect(binaries[:1]))

    def test_docker_client_alone_is_not_local_daemon(self):
        self.assertEqual(self.detect(('docker',)), {})

    def test_installed_package_outside_path(self):
        self.assertIn('Docker', self.detect(outputs={
            ('dpkg-query', '-W', '-f=${binary:Package} ${db:Status-Status}\n'):
                'docker-ce:amd64 installed\npodman config-files\n'}))

    def test_enabled_unit_without_executable_or_bus(self):
        self.assertIn('Docker', self.detect(outputs={
            ('systemctl', '--root=/', 'is-enabled', 'docker.socket'): 'enabled\n'}))

    def test_no_runtime_and_disabled_units(self):
        self.assertEqual(self.detect(), {})

    def test_report_roundtrip_retains_evidence_and_old_reports_load(self):
        report = hw.HardwareReport('arm64', 'CPU', container_runtimes={'Docker': ['package: docker-ce']})
        restored = hw.HardwareReport(**json.loads(json.dumps(asdict(report))))
        self.assertEqual(restored.container_runtimes, report.container_runtimes)
        self.assertIn('Container runtimes: Docker', hw.render_report(restored))
        self.assertEqual(hw.HardwareReport('arm64', 'CPU').container_runtimes, {})


class ContainerConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'net').mkdir()
        shutil.copyfile(FIXTURES / 'Kconfig', self.root / 'net/Kconfig')
        self.config = self.root / '.config'
        shutil.copyfile(FIXTURES / '7.2.6-optimized.config', self.config)
        self.baseline = self.root / 'baseline.config'
        shutil.copyfile(FIXTURES / '7.2.0-custom.config', self.baseline)
        self.report = hw.HardwareReport('arm64', 'CPU', root_filesystem='ext4',
                                       container_runtimes={'Docker': ['package: docker-ce']})

    def apply(self):
        hw.update_config(self.config, self.report, self.baseline, self.root)

    def test_real_failure_is_inherited_not_a_baseline_loss(self):
        a, b = hw._config_values(self.baseline), hw._config_values(self.config)
        self.assertFalse({k for k,v in a.items() if v in ('y', 'm') and b.get(k) not in ('y', 'm')})
        for name in ('NETFILTER_XT_MATCH_ADDRTYPE', 'NFT_COMPAT', 'VETH', 'OVERLAY_FS'):
            self.assertNotIn(name, a)
            self.assertNotIn(name, b)
        with self.assertRaisesRegex(RuntimeError, 'CONFIG_NETFILTER_XT_MATCH_ADDRTYPE'):
            hw.verify_containers(self.config, self.report, self.baseline, self.root)
        self.apply()
        hw.verify_config(self.config, self.report, self.baseline, self.root)

    def test_each_required_feature_loss_is_rejected(self):
        self.apply()
        good = self.config.read_text()
        values = hw._config_values(self.config)
        for name in hw.CONTAINER_REQUIRED:
            with self.subTest(symbol=name):
                self.config.write_text(good.replace(f'CONFIG_{name}={values[name]}', f'# CONFIG_{name} is not set'))
                with self.assertRaisesRegex(RuntimeError, 'CONFIG_' + name):
                    hw.verify_containers(self.config, self.report, self.baseline, self.root)

    def test_preserves_additional_baseline_feature_and_external_dependency(self):
        with (self.root / 'Kconfig').open('w') as stream:
            stream.write('config CGROUP_FUTURE\n bool "future controller"\n depends on HELPER\n'
                         'config HELPER\n tristate "external helper"\n')
        with self.baseline.open('a') as stream:
            stream.write('CONFIG_CGROUP_FUTURE=y\nCONFIG_HELPER=m\n')
        self.apply()
        values = hw._config_values(self.config)
        self.assertEqual(values['CGROUP_FUTURE'], 'y')
        self.assertEqual(values['HELPER'], 'm')
        self.config.write_text(self.config.read_text().replace('CONFIG_HELPER=m', '# CONFIG_HELPER is not set'))
        with self.assertRaisesRegex(RuntimeError, 'CONFIG_HELPER'):
            hw.verify_containers(self.config, self.report, self.baseline, self.root)

    def test_remote_processor_name_service_and_hidden_fs_helper_are_not_container_features(self):
        with (self.root / 'net/Kconfig').open('a') as stream:
            stream.write('config RPMSG\n tristate\n'
                         'config RPMSG_NS\n tristate "Remote processor name service"\n depends on RPMSG\n'
                         'config BLK_CGROUP_UNUSED_FS_HELPER\n bool\n')
        with self.baseline.open('a') as stream:
            stream.write('CONFIG_RPMSG=m\nCONFIG_RPMSG_NS=m\nCONFIG_BLK_CGROUP_UNUSED_FS_HELPER=y\n')
        required = hw.container_requirements(self.report, self.baseline, self.root)
        self.assertNotIn('RPMSG_NS', required)
        self.assertNotIn('RPMSG', required)
        self.assertNotIn('BLK_CGROUP_UNUSED_FS_HELPER', required)
        self.assertTrue(hw.CONTAINER_REQUIRED <= required.keys())

    def test_missing_source_or_baseline_fails_closed(self):
        for baseline, source in ((None, self.root), (self.baseline, None)):
            with self.assertRaisesRegex(RuntimeError, 'requires a baseline'):
                hw.update_config(self.config, self.report, baseline, source)

    def test_unknown_required_target_symbol_fails_closed(self):
        path = self.root / 'net/Kconfig'
        path.write_text(path.read_text().replace('config VETH\n', 'config RENAMED_VETH\n'))
        with self.assertRaisesRegex(RuntimeError, 'CONFIG_VETH'):
            self.apply()

    def test_no_runtime_does_not_add_container_floor(self):
        self.report.container_runtimes = {}
        self.apply()
        self.assertNotIn('OVERLAY_FS', hw._config_values(self.config))
        self.assertNotIn('VETH', hw._config_values(self.config))

    def test_target_bool_type_and_builtin_baseline_are_respected(self):
        path = self.root / 'net/Kconfig'
        path.write_text(path.read_text().replace('tristate "VETH"', 'bool "VETH"'))
        with self.baseline.open('a') as stream:
            stream.write('CONFIG_VETH=m\nCONFIG_OVERLAY_FS=y\n')
        self.apply()
        values = hw._config_values(self.config)
        self.assertEqual(values['VETH'], 'y')
        self.assertEqual(values['OVERLAY_FS'], 'y')

    def test_real_kconfig_resolution_and_dependency_rejection(self):
        candidates = list(Path('/home/adrian/kernel-build').glob('linux-*/scripts/kconfig/conf'))
        if not candidates:
            self.skipTest('Kconfig conf executable unavailable')
        self.apply()
        env = dict(os.environ, KCONFIG_CONFIG=str(self.config))
        def resolve():
            return subprocess.run([str(candidates[0]), '--olddefconfig', str(self.root / 'net/Kconfig')],
                                  cwd=self.root, env=env, check=True, capture_output=True, text=True)
        self.assertEqual(resolve().stderr, '')
        hw.verify_containers(self.config, self.report, self.baseline, self.root)
        self.config.write_text(self.config.read_text().replace('CONFIG_NETFILTER=y', '# CONFIG_NETFILTER is not set'))
        resolve()
        with self.assertRaisesRegex(RuntimeError, 'CONFIG_NETFILTER_XT_MATCH_ADDRTYPE'):
            hw.verify_containers(self.config, self.report, self.baseline, self.root)


class InstalledKconfigIntegrationTests(unittest.TestCase):
    def test_actual_726_config_resolves_without_building_kernel(self):
        source = Path('/home/adrian/kernel-build/linux-7.2.6')
        baseline = Path('/boot/config-7.2.0-custom')
        optimized = Path('/boot/config-7.2.6-optimized')
        conf = source / 'scripts/kconfig/conf'
        if not all(p.exists() for p in (conf, baseline, optimized)):
            self.skipTest('Installed 7.2.6 Kconfig/conf and comparison configs unavailable')
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / '.config'
            shutil.copyfile(optimized, config)
            report = hw.HardwareReport('x86_64', 'CPU', root_filesystem='ext4',
                                       container_runtimes={'Docker': ['installed']})
            hw.update_config(config, report, baseline, source)
            env = dict(os.environ, KCONFIG_CONFIG=str(config), srctree=str(source),
                       ARCH='x86', SRCARCH='x86', CC='gcc', LD='ld', HOSTCC='gcc',
                       RUSTC='rustc', PAHOLE_VERSION='0')
            resolved = subprocess.run([str(conf), '--olddefconfig', str(source / 'Kconfig')],
                                      cwd=tmp, env=env, check=True, capture_output=True, text=True)
            self.assertEqual(resolved.stderr, '')
            hw.verify_config(config, report, baseline, source)


if __name__ == '__main__':
    unittest.main()
