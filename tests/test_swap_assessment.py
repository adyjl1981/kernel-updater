"""Fixture analysis and GUI actions; never invoke a privileged helper."""
import unittest
from unittest import mock
import memory_management as memory
import test_memory_management as fixtures
from test_grub_menu import gui


class AssessmentFixtures(unittest.TestCase):
    setUp = fixtures.SnapshotTests.setUp
    write = fixtures.SnapshotTests.write
    fixture = fixtures.SnapshotTests.fixture

    def test_pressure_and_disk_sample_is_read_only(self):
        self.fixture()
        self.write('proc/pressure/memory', 'some avg10=2.50 avg60=1.00 avg300=0.50 total=100\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n')
        space = mock.Mock(f_bavail=1000, f_frsize=4096)
        with mock.patch.object(memory.os, 'statvfs', return_value=space), \
                mock.patch.object(memory.subprocess, 'run', side_effect=AssertionError('command')), \
                mock.patch.object(memory.Path, 'write_text', side_effect=AssertionError('write')):
            sample = memory.snapshot(self.root)
            report = memory.check_settings(sample)
        self.assertEqual(sample.pressure, {'some': 2.5, 'full': 0.0})
        self.assertEqual(sample.disk_free['/inactive'], 4096000)
        self.assertIn('PSI exceeds', report)
        self.assertIn('configured or hinted', report)
        self.assertIn('Zram capacity', report)
        self.assertIn('not a RAM usage percentage', report)

    def test_bad_pressure_and_unknown_disk_are_not_healthy(self):
        self.fixture()
        self.write('proc/pressure/memory', 'some avg10=nan avg60=0 avg300=0 total=1\nfull avg10=101 avg60=0 avg300=0 total=1\n')
        with mock.patch.object(memory.os, 'statvfs', side_effect=PermissionError()):
            sample = memory.snapshot(self.root)
        self.assertEqual(sample.pressure, {})
        self.assertIsNone(sample.disk_free['/inactive'])
        self.assertIn('PSI unavailable', memory.check_settings(sample))
        self.assertIn('Malformed memory pressure', memory.check_settings(sample))


class RecommendationTests(unittest.TestCase):
    def test_swappiness_explanation_and_conditional_trials(self):
        sample = self.sample()
        for devices, phrase in (([], 'type is uncertain'),
                                ([memory.SwapDevice('/swapfile', 'file', True)], 'try a small decrease'),
                                ([memory.SwapDevice('/dev/zram0', 'zram', True)], 'entirely zram'),
                                ([memory.SwapDevice('/dev/zram0', 'zram', True), memory.SwapDevice('/swapfile', 'file', True)], 'Swappiness is global')):
            sample.devices = devices
            report = '\n'.join(memory.swappiness_advice(sample))
            self.assertIn(phrase, report)
            self.assertIn('Decrease it:', report)
            self.assertIn('Increase it:', report)
            self.assertIn('0 does not disable swap', report)
            self.assertIn('does not change swap capacity', report)
            if devices:
                self.assertIn('60 → 50', report)
                self.assertIn('60 → 70', report)
                self.assertIn('Temporary (until reboot)', report)
                self.assertIn('Restore Previous Swappiness', report)
        for value, present, absent in ((0, '0 → 10', '0 → 0'), (200, '200 → 190', '200 → 200')):
            sample.swappiness = value
            report = '\n'.join(memory.swappiness_advice(sample))
            self.assertIn(present, report)
            self.assertNotIn(absent, report)
        sample.swappiness = None
        self.assertIn('No numeric target', '\n'.join(memory.swappiness_advice(sample)))
        sample.memory['SwapTotal'] = 0
        report = '\n'.join(memory.swappiness_advice(sample))
        self.assertIn('no active swap capacity', report)
        self.assertNotIn('How to test', report)

    def sample(self, available=500, swap=100, free=80):
        return memory.Snapshot(memory={'MemTotal': 1000, 'MemAvailable': available,
                                     'SwapTotal': swap, 'SwapFree': free}, swappiness=60)

    def test_conditional_workload_advice(self):
        for sample, phrase in ((self.sample(), 'peak workloads'),
                               (self.sample(50, 100, 10), 'If this persists'),
                               (self.sample(50, 0, 0), 'swap is absent'),
                               (self.sample(50), 'swap has headroom'),
                               (self.sample(1001), 'advice is withheld'),
                               (self.sample(free=101), 'advice is withheld'),
                               (memory.Snapshot(), 'advice is withheld')):
            with self.subTest(phrase=phrase):
                report = memory.check_settings(sample)
                self.assertIn(phrase, report)
                self.assertIn('No guaranteed optimum', report)


class AssessmentGuiTests(unittest.TestCase):
    def test_check_reads_fresh_snapshot_without_authentication(self):
        app = fixtures.GuiTests().app()
        app._memory_report = mock.Mock()
        with mock.patch.object(memory, 'snapshot', return_value=memory.Snapshot()) as snapshot, \
                mock.patch.object(memory, 'request_change', side_effect=AssertionError('change')):
            app.on_check_swap_settings()
        snapshot.assert_called_once()
        app._memory_report.assert_called_once()
        app._run_operation.assert_not_called()
