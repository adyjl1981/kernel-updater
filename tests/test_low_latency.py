"""Overlay and refusal tests; no installation or live configuration changes."""
from pathlib import Path
import contextlib
import io
import tempfile
import unittest
from unittest import mock
import subprocess
import low_latency as latency

class OverlayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / '.config'
        self.config.write_text('CONFIG_MODULES=y\nCONFIG_EXT4_FS=y\nCONFIG_NET_NS=y\nCONFIG_OVERLAY_FS=m\n')
        symbols = set(latency.REQUIRED) | set(latency.DISABLED) | {'PREEMPT_DYNAMIC'}
        (self.root / 'Kconfig').write_text('\n'.join('config ' + s for s in symbols))
        self.requested = {}

    def run_command(self, command, **kwargs):
        if command[0] == 'make':
            with self.config.open('a') as stream:
                for key, value in self.requested.items():
                    stream.write(f'CONFIG_{key}={value}\n')
        else:
            self.requested[command[-2]] = command[-1]

    def configure(self, side_effect=None):
        with mock.patch.object(latency.subprocess, 'run', side_effect=side_effect or self.run_command), contextlib.redirect_stdout(io.StringIO()):
            return latency.configure(self.root, ['CC=gcc'])

    def test_preserves_compatibility_and_selects_full_preemption(self):
        report = self.configure()
        self.assertEqual(report['errors'], [])
        self.assertEqual(report['lost_baseline_features'], [])
        self.assertEqual(report['effective']['PREEMPT_DYNAMIC'], 'y')
        self.assertEqual(report['effective']['PREEMPT_LAZY'], 'n')
        self.assertEqual(report['effective']['PREEMPT_RT'], 'n')
        self.assertEqual(latency.values(self.config)['OVERLAY_FS'], 'm')

    def test_missing_required_option_refused_before_mutation(self):
        (self.root / 'Kconfig').write_text('config PREEMPT\n')
        with self.assertRaisesRegex(ValueError, 'Unsupported'):
            self.configure()
        self.assertEqual(self.requested, {})

    def test_dependency_rejection_and_baseline_loss_fail_closed(self):
        for lost in ('PREEMPT', 'HIGH_RES_TIMERS', 'HZ_1000', 'NO_HZ_IDLE', 'NET_NS', 'MODULES'):
            with self.subTest(lost=lost):
                def resolve(command, **kwargs):
                    self.run_command(command, **kwargs)
                    if command[0] == 'make':
                        self.config.write_text(self.config.read_text() + f'CONFIG_{lost}=n\n')
                with self.assertRaisesRegex(ValueError, 'validation failed'):
                    self.configure(resolve)
                self.setUp()

    def test_dynamic_unavailable_falls_back_to_full(self):
        def resolve(command, **kwargs):
            self.run_command(command, **kwargs)
            if command[0] == 'make':
                self.config.write_text(self.config.read_text() + 'CONFIG_PREEMPT_DYNAMIC=n\n')
        self.assertTrue(self.configure(resolve)['dynamic_fallback'])

    def test_old_kernels_without_lazy_rt_or_dynamic(self):
        (self.root / 'Kconfig').write_text('\n'.join('config ' + s for s in latency.REQUIRED))
        report = self.configure()
        self.assertNotIn('PREEMPT_LAZY', report['requested'])
        self.assertTrue(report['dynamic_fallback'])

    def test_retains_rcu_offload_dependency_without_full_tickless(self):
        with self.config.open('a') as stream:
            stream.write('CONFIG_RCU_NOCB_CPU=y\n')
        with (self.root / 'Kconfig').open('a') as stream:
            stream.write('\nconfig RCU_EXPERT\n')
        report = self.configure()
        self.assertEqual(report['effective']['RCU_EXPERT'], 'y')
        self.assertEqual(report['lost_baseline_features'], [])

    def test_native_make_failure_propagates(self):
        with self.assertRaises(subprocess.CalledProcessError):
            self.configure(mock.Mock(side_effect=subprocess.CalledProcessError(2, 'make')))

class SelectionTests(unittest.TestCase):
    def test_profile_exclusion_and_naming_before_any_build(self):
        script = Path(__file__).resolve().parents[1] / 'build-custom-kernel.sh'
        prefix = script.read_text().split('log()  {', 1)[0]
        prefix = prefix.replace('SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"', 'SCRIPT_DIR=/tmp')
        for args, expected in [([], '-custom'), (['--hardware-optimised'], '-optimized'),
                               (['--low-latency'], '-custom-lowlatency'),
                               (['--low-latency', '--localversion', '-lab'], '-lab-lowlatency'),
                               (['--low-latency', '--localversion', '-lab-lowlatency'], '-lab-lowlatency')]:
            result = subprocess.run(['bash', '-c', prefix + '\nprintf "%s" "$LOCALVERSION"', 'test', *args], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, expected)
        result = subprocess.run(['bash', '-c', prefix, 'test', '--low-latency', '--hardware-optimised'], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
