"""Resource policy uses fixtures; never starts a real build or installs packages."""
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import build_resources as resources
import test_kernel_manager as existing


class PolicyTests(unittest.TestCase):
    def jobs(self, cpus, gib, **options):
        return resources.choose_jobs(cpus, None if gib is None else int(gib * resources.GIB), **options)[0]

    def test_cpu_and_memory_caps(self):
        for cpus, gib, expected in [(2, 32, 2), (16, 4, 3), (32, 16, 14),
                                    (1, 2, 1), (0, 32, 1), (8, 0, 1), (8, .5, 1), (8, None, 1)]:
            with self.subTest(cpus=cpus, gib=gib):
                self.assertEqual(self.jobs(cpus, gib), expected)

    def test_heavy_options_reduce_parallelism(self):
        self.assertEqual(self.jobs(16, 8), 7)
        self.assertEqual(self.jobs(16, 8, debug=True), 3)
        self.assertEqual(self.jobs(16, 8, lto=True), 2)
        self.assertEqual(self.jobs(16, 8, lto=True, debug=True), 1)
        self.assertIn('swap excluded', resources.choose_jobs(4, resources.GIB)[1])

    def test_monotonic_budget_and_bounds(self):
        for cpus in (1, 2, 8, 128):
            counts = [self.jobs(cpus, gib, lto=True) for gib in range(64)]
            self.assertEqual(counts, sorted(counts))
            self.assertTrue(all(1 <= jobs <= cpus for jobs in counts))


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.proc = self.root/'proc'; self.proc.mkdir()
        (self.proc/'self').mkdir()
        self.cg = self.root/'cgroup'; self.cg.mkdir()
        (self.proc/'meminfo').write_text('MemTotal: 64000000 kB\nMemAvailable: 8388608 kB\nSwapFree: 64000000 kB\n')

    def detect(self):
        with mock.patch.object(resources.os, 'sched_getaffinity', return_value=set(range(8))):
            return resources.detect_resources(self.proc, self.cg)

    def test_affinity_and_available_not_total_or_swap(self):
        self.assertEqual(self.detect(), (8, 8 * resources.GIB))
        with mock.patch.object(resources.os, 'sched_getaffinity', side_effect=OSError), mock.patch.object(resources.os, 'cpu_count', return_value=None):
            self.assertEqual(resources.detect_resources(self.proc, self.cg)[0], 1)

    def test_parent_and_child_cgroup_limits(self):
        (self.proc/'self/cgroup').write_text('0::/parent/child\n')
        parent = self.cg/'parent'; child = parent/'child'; child.mkdir(parents=True)
        (parent/'cpu.max').write_text('250000 100000')
        (child/'cpu.max').write_text('600000 100000')
        (parent/'memory.max').write_text(str(4 * resources.GIB))
        (parent/'memory.current').write_text(str(resources.GIB))
        (child/'memory.max').write_text('max')
        self.assertEqual(self.detect(), (2, 3 * resources.GIB))
        (parent/'cpu.max').write_text('50000 100000')
        (parent/'memory.current').write_text(str(5 * resources.GIB))
        self.assertEqual(self.detect(), (1, 0))

    def test_missing_malformed_and_namespace_root(self):
        (self.proc/'meminfo').write_text('MemAvailable: broken\n')
        (self.cg/'cpu.max').write_text('100000 0')
        self.assertEqual(self.detect(), (8, None))
        (self.proc/'self/cgroup').write_text('0::/../../outside\n')
        (self.cg/'memory.max').write_text(str(2 * resources.GIB))
        (self.cg/'memory.current').write_text(str(resources.GIB))
        self.assertEqual(self.detect(), (8, resources.GIB))


class AutomaticGuiTests(unittest.TestCase):
    def setUp(self):
        self.app = existing.gui.KernelManagerApp.__new__(existing.gui.KernelManagerApp)
        for name, value in [('auto_jobs_var', True), ('jobs_var', '0'), ('toolchain_var', 'clang'),
                            ('lto_var', True), ('debug_var', True), ('force_var', False), ('build_mode_var', 'standard')]:
            var = mock.Mock(); var.get.return_value = value; setattr(self.app, name, var)
        self.app._dependencies_available = mock.Mock(return_value=True)
        self.app._start_stream = mock.Mock()
        self.app._append_log = mock.Mock()
        self.app.jobs_spinbox = mock.Mock()

    def test_auto_ignores_manual_field_and_delegates_fresh_selection(self):
        with mock.patch.object(resources, 'recommend_jobs', return_value=(2, 'memory limit')) as recommend:
            self.app.start_build()
        recommend.assert_called_once_with(lto=True, debug=True)
        command = self.app._start_stream.call_args.args[0]
        self.assertNotIn('--jobs', command)
        self.assertIn('--lto', command)
        self.assertIn('--full-debug-info', command)
        self.assertIn('2 parallel jobs', self.app._jobs_estimate)

    def test_manual_and_control_state(self):
        self.app._sync_jobs_state()
        self.app.jobs_spinbox.configure.assert_called_with(state='disabled')
        self.app.auto_jobs_var.get.return_value = False
        self.app._sync_jobs_state()
        self.app.jobs_spinbox.configure.assert_called_with(state='normal')
        self.app.jobs_var.get.return_value = '7'
        with mock.patch.object(resources, 'recommend_jobs') as recommend:
            self.app.start_build()
        recommend.assert_not_called()
        command = self.app._start_stream.call_args.args[0]
        self.assertEqual(command[command.index('--jobs') + 1], '7')

    def test_each_build_rechecks_options_and_memory(self):
        with mock.patch.object(resources, 'recommend_jobs', side_effect=[(4, 'first'), (1, 'second')]) as recommend:
            self.app.start_build()
            self.app.toolchain_var.get.return_value = 'gcc'
            self.app.debug_var.get.return_value = False
            self.app.start_build()
        self.assertEqual(recommend.call_args_list[-1].kwargs, {'lto': False, 'debug': False})
        self.assertIn('1 parallel jobs', self.app._jobs_estimate)

    def test_manual_and_auto_preferences_saved_independently(self):
        self.app.busy = None
        self.app.root = mock.Mock()
        self.app.jobs_var.get.return_value = '7'
        with mock.patch.object(existing.gui, 'save_presets') as save:
            self.app._on_close()
        self.assertEqual(save.call_args.args[0]['jobs'], '7')
        self.assertIs(save.call_args.args[0]['auto_jobs'], True)


class LinkerCommandTests(unittest.TestCase):
    def test_thinlto_limit_preserves_makefile_linker_flags(self):
        import shutil
        import subprocess
        for tool in ('clang', 'ld.lld', 'make'):
            if not shutil.which(tool):
                self.skipTest(f'{tool} unavailable for linker smoke test')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'tiny.c').write_text('void _start(void) {}\n')
            subprocess.run(['clang', '-flto=thin', '-c', 'tiny.c', '-o', 'tiny.o'], cwd=root, check=True, capture_output=True)
            (root/'Makefile').write_text('LDFLAGS_vmlinux := -z max-page-size=0x200000\nall:\n\t$(LD) $(LDFLAGS_vmlinux) tiny.o -o tiny\n')
            result = subprocess.run(['make', 'LD=ld.lld --thinlto-jobs=1'], cwd=root, check=True, capture_output=True, text=True)
            self.assertIn('--thinlto-jobs=1 -z max-page-size=0x200000', result.stdout)
            self.assertTrue((root/'tiny').is_file())
