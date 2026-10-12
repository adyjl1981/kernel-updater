"""Fixture-only reads and mocked GUI. No privileged actions or host writes."""
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import memory_management as memory
from test_grub_menu import gui


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, name, text):
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def fixture(self):
        self.write('proc/meminfo', 'MemTotal: 4096 kB\nMemAvailable: 1024 kB\nSwapTotal: 2048 kB\nSwapFree: 1536 kB\n')
        self.write('proc/sys/vm/swappiness', '60\n')
        self.write('proc/swaps', 'Filename Type Size Used Priority\n/swap\\040file file 1024 512 -2\n/dev/sda2 partition 512 0 -3\n/dev/zram0 partition 512 0 100\n')
        self.write('etc/fstab', '/swap\\040file none swap defaults 0 0\nUUID=abc none swap noauto 0 0\n/inactive none swap defaults 0 0\n')
        self.write('inactive', 'not assumed managed')
        self.write('sys/block/zram1/disksize', '1048576\n')
        self.write('etc/systemd/system/extra.swap', '[Swap]\nWhat=/extra\n')
        self.write('sys/power/resume', '8:2\n')
        self.write('sys/power/resume_offset', '32\n')
        self.write('proc/cmdline', 'quiet resume=UUID=abc resume_offset=32')

    def test_discovery_and_exact_units(self):
        self.fixture()
        result = memory.snapshot(self.root)
        self.assertEqual(result.memory['MemAvailable'], 1048576)
        self.assertEqual(result.swappiness, 60)
        devices = {d.path: d for d in result.devices}
        self.assertEqual(devices['/swap file'].size, 1048576)
        self.assertEqual(devices['/swap file'].used, 524288)
        self.assertTrue(devices['/swap file'].active)
        self.assertEqual(devices['/dev/sda2'].kind, 'partition')
        self.assertEqual(devices['/dev/zram0'].kind, 'zram')
        self.assertFalse(devices['/dev/zram1'].active)
        self.assertEqual(devices['/dev/zram1'].size, 1048576)
        self.assertEqual(devices['/inactive'].kind, 'file')
        self.assertIn('noauto', devices['UUID=abc'].configured[0])
        self.assertIsNone(devices['UUID=abc'].active)
        self.assertIn('enablement unknown', devices['/extra'].configured[0])
        self.assertIn('8:2', result.resume)
        self.assertIn('resume_offset=32', result.resume)
        self.assertEqual(memory.exact_size(1048576), '1,048,576 B (1.00 MiB)')
        self.assertEqual(memory.exact_size(None), 'unknown')

    def test_uuid_alias_merges_active_and_inactive_with_extra_observed_paths(self):
        self.fixture()
        self.write('dev/sda2', 'fixture block representation')
        alias = self.root/'dev/disk/by-uuid/abc'
        alias.parent.mkdir(parents=True)
        alias.symlink_to('../../sda2')
        result = memory.snapshot(self.root)
        self.assertNotIn('UUID=abc', [d.path for d in result.devices])
        item = next(d for d in result.devices if d.path == '/dev/sda2')
        self.assertTrue(item.active)
        self.assertIn('noauto', item.configured[0])
        self.write('proc/swaps', 'Filename Type Size Used Priority\n')
        self.write('custom-swap', 'fixture swap file')
        result = memory.snapshot(self.root, extra_paths=('/custom-swap',))
        item = next(d for d in result.devices if d.path == '/dev/sda2')
        self.assertFalse(item.active)
        self.assertIsNotNone(item.identity)
        custom = next(d for d in result.devices if d.path == '/custom-swap')
        self.assertFalse(custom.active)
        self.assertEqual(custom.kind, 'file')

    def test_missing_malformed_and_unreadable_are_explicit(self):
        result = memory.snapshot(self.root)
        self.assertIsNone(result.swappiness)
        self.assertNotIn('MemAvailable', result.memory)
        self.assertTrue(result.warnings)
        self.fixture()
        self.write('proc/swaps', 'Filename Type Size Used Priority\n/x file 1 2 -2\n/y unexpected 1 0 0\n')
        self.write('proc/sys/vm/swappiness', 'invalid')
        self.write('etc/systemd/system/dynamic.swap', '[Swap]\nWhat=/dev/%i\n')
        result = memory.snapshot(self.root)
        self.assertTrue(any('Malformed' in w for w in result.warnings))
        self.assertTrue(any('not resolved' in w for w in result.warnings))
        self.assertIsNone(result.swappiness)
        with mock.patch.object(Path, 'read_text', side_effect=PermissionError('denied')):
            self.assertTrue(any('denied' in w for w in memory.snapshot(self.root).warnings))

    def test_validation(self):
        for value in ('0', '60', '100', '200'):
            self.assertEqual(memory.validate_swappiness(value), int(value))
        for value in ('-1', '201', '1.0', '60;reboot', '', ' 60', None, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                memory.validate_swappiness(value)

    def test_no_writes_or_subprocesses(self):
        self.fixture()
        with mock.patch.object(Path, 'write_text', side_effect=AssertionError('write')), \
                mock.patch.object(gui.subprocess, 'run', side_effect=AssertionError('command')):
            memory.snapshot(self.root)


class GuiTests(unittest.TestCase):
    def app(self):
        app = gui.KernelManagerApp.__new__(gui.KernelManagerApp)
        app.root = mock.Mock()
        app.memory_tab = mock.Mock()
        app.memory_summary = mock.Mock()
        app.memory_details = mock.Mock()
        app.memory_devices = mock.Mock()
        app.memory_devices.get_children.return_value = ('old',)
        app.memory_swappiness = mock.Mock()
        app.memory_scope = mock.Mock()
        app.memory_scope.get.return_value = "Temporary (until reboot)"
        app.memory_snapshot = None
        app.memory_seen_paths = set()
        app.memory_current = None
        app.memory_helper_problem = None
        for name in ('memory_enable', 'memory_disable', 'memory_apply', 'memory_restore', 'memory_action_status', '_run_operation'):
            setattr(app, name, mock.Mock())
        app._read_async = lambda key, work, done: done(work())
        return app

    def test_layout_actions_disabled_and_accent_refresh(self):
        app = self.app()
        app.refresh_memory = mock.Mock()
        with mock.patch.object(gui.tk, 'Canvas'), mock.patch.object(gui.tk, 'StringVar'), \
                mock.patch.object(gui.ttk, 'Frame'), mock.patch.object(gui.ttk, 'Scrollbar'), \
                mock.patch.object(gui.ttk, 'Label'), mock.patch.object(gui.ttk, 'Entry') as entry, \
                mock.patch.object(gui.ttk, 'Combobox') as choice, mock.patch.object(gui.ttk, 'Button') as buttons, \
                mock.patch.object(gui, 'scrolled_tree', return_value=(mock.Mock(), mock.Mock())):
            app._build_memory_tab()
        calls = buttons.call_args_list
        self.assertEqual(calls[0].kwargs['style'], 'Accent.TButton')
        self.assertEqual(calls[0].kwargs['command'], app.refresh_memory)
        for call in calls[1:]:
            if call.kwargs['text'] in ('Helper Setup', 'Add Device to List…', 'Check Swap Settings'):
                self.assertIn(call.kwargs['command'], (app.on_memory_setup, app.on_memory_add_device,
                              app.on_check_swap_settings))
                continue
            self.assertEqual(call.kwargs['state'], 'disabled')
            self.assertIn('command', call.kwargs)
        self.assertEqual(entry.call_args.kwargs['state'], 'normal')
        self.assertEqual(len(choice.call_args.kwargs['values']), 2)
        app.refresh_memory.assert_called_once()

    def test_refresh_and_failure_clear_stale_snapshot(self):
        app = self.app()
        result = memory.Snapshot(memory={'MemTotal': 1048576}, swappiness=42,
                                 devices=[memory.SwapDevice('/dev/zram0', 'zram', True)], warnings=['partial'])
        with mock.patch.object(memory, 'snapshot', return_value=result), mock.patch.object(memory, 'helper_error', return_value=None):
            app.refresh_memory()
        app.memory_swappiness.set.assert_called_with('42')
        self.assertIn('1,048,576 B', app.memory_summary.configure.call_args.kwargs['text'])
        self.assertEqual(app.memory_devices.insert.call_args.kwargs['values'][0], 'zram')
        with mock.patch.object(memory, 'snapshot', side_effect=OSError('unavailable')):
            app.refresh_memory()
        app.memory_devices.delete.assert_called_with('old')
        app.memory_swappiness.set.assert_called_with('')
        app.memory_details.configure.assert_called_with(text='unavailable')

    def ready(self):
        app = self.app()
        app.memory_current = 60
        app.memory_snapshot = memory.Snapshot(swappiness=60, devices=[
            memory.SwapDevice('/swapfile', 'file', True, identity=[1, 2, 0, 1024])])
        app.memory_swappiness.get.return_value = '80'
        app.memory_devices.selection.return_value = ('0',)
        return app

    def test_cancel_invalid_input_and_missing_helper_do_not_authenticate(self):
        app = self.ready()
        with mock.patch.object(memory, 'helper_error', return_value=None), mock.patch.object(gui.messagebox, 'askyesno', return_value=False):
            app.on_memory_swappiness()
            app.on_memory_toggle(False)
            app.on_memory_restore()
        app._run_operation.assert_not_called()
        app.memory_swappiness.get.return_value = '80;id'
        with mock.patch.object(gui.messagebox, 'showerror'), mock.patch.object(gui.messagebox, 'askyesno') as confirm:
            app.on_memory_swappiness()
        confirm.assert_not_called()
        app.memory_swappiness.get.return_value = '80'
        with mock.patch.object(memory, 'helper_error', return_value='Setup required'), mock.patch.object(gui.messagebox, 'showerror'), \
                mock.patch.object(gui.messagebox, 'askyesno') as confirm:
            app.on_memory_swappiness()
        confirm.assert_not_called()
        app._run_operation.assert_not_called()

    def test_explicit_scopes_and_restore_requests(self):
        app = self.ready()
        app._run_memory_change = mock.Mock()
        with mock.patch.object(memory, 'helper_error', return_value=None), mock.patch.object(gui.messagebox, 'askyesno', return_value=True):
            app.on_memory_swappiness()
            self.assertFalse(app._run_memory_change.call_args.args[1]['persistent'])
            app.memory_scope.get.return_value = 'Persistent (future boots)'
            app.on_memory_swappiness()
            request = app._run_memory_change.call_args.args[1]
            self.assertTrue(request['persistent'])
            self.assertEqual(request['expected'], 60)
            self.assertEqual(request['value'], 80)
            app.on_memory_restore()
            self.assertEqual(app._run_memory_change.call_args.args[1]['action'], 'restore_swappiness')

    def test_selected_device_request_and_unknown_state_rejection(self):
        app = self.ready()
        app._run_memory_change = mock.Mock()
        with mock.patch.object(memory, 'helper_error', return_value=None), mock.patch.object(gui.messagebox, 'askyesno', return_value=True):
            app.on_memory_toggle(False)
        request = app._run_memory_change.call_args.args[1]
        self.assertEqual(request, {'protocol': 1, 'action': 'swap', 'path': '/swapfile',
                                  'enable': False, 'expected_active': True, 'identity': [1, 2, 0, 1024]})
        app._run_memory_change.reset_mock()
        app.memory_snapshot.devices[0].active = None
        with mock.patch.object(gui.messagebox, 'showerror'), mock.patch.object(gui.messagebox, 'askyesno') as confirm:
            app.on_memory_toggle(False)
        confirm.assert_not_called()
        app._run_memory_change.assert_not_called()

    def test_authentication_failure_refreshes_and_reports_error(self):
        app = self.ready()
        request = {'protocol': 1, 'action': 'swappiness', 'expected': 60, 'value': 80, 'persistent': False}
        app._run_memory_change('Change Swappiness', request)
        work, done = app._run_operation.call_args.args[1:]
        with mock.patch.object(gui, 'gui_env', return_value={}), \
                mock.patch.object(memory, 'request_change', side_effect=RuntimeError('Authentication failed')):
            with self.assertRaisesRegex(RuntimeError, 'Authentication failed'):
                work(mock.Mock())
        app.refresh_memory = mock.Mock()
        with mock.patch.object(gui.messagebox, 'showerror') as error:
            done(False)
        error.assert_called_once()
        app.refresh_memory.assert_called_once()

    def test_selection_gates_each_device_action(self):
        app = self.ready()
        app._memory_selection_changed()
        app.memory_enable.configure.assert_called_with(state='disabled')
        app.memory_disable.configure.assert_called_with(state='normal')
        app.memory_helper_problem = 'Setup required'
        app._memory_selection_changed()
        app.memory_disable.configure.assert_called_with(state='disabled')

    def test_manual_discovery_is_unprivileged_and_validated(self):
        app = self.ready()
        app.refresh_memory = mock.Mock()
        with mock.patch.object(gui.simpledialog, 'askstring', return_value='/custom-swap'):
            app.on_memory_add_device()
        self.assertIn('/custom-swap', app.memory_seen_paths)
        app.refresh_memory.assert_called_once()
        app._run_operation.assert_not_called()
        with mock.patch.object(gui.simpledialog, 'askstring', return_value='/../etc/passwd'), \
                mock.patch.object(gui.messagebox, 'showerror') as error:
            app.on_memory_add_device()
        error.assert_called_once()
        self.assertNotIn('/../etc/passwd', app.memory_seen_paths)

    def test_setup_dialog_requires_install_click(self):
        app = self.app()
        app._install_memory_helper = mock.Mock()
        with mock.patch.object(gui.tk, 'Toplevel') as window, \
                mock.patch.object(gui.ttk, 'Frame'), mock.patch.object(gui.ttk, 'Label'), \
                mock.patch.object(gui.ttk, 'Button') as buttons:
            app.on_memory_setup()
        app._install_memory_helper.assert_not_called()
        actions = {call.kwargs['text']: call.kwargs['command'] for call in buttons.call_args_list}
        actions['Cancel']()
        app._install_memory_helper.assert_not_called()
        actions['Install Helper']()
        app._install_memory_helper.assert_called_once()
        window.return_value.destroy.assert_called()

    def test_setup_runs_authenticated_installer_and_verifies(self):
        app = self.app()
        app.refresh_memory = mock.Mock()
        app._install_memory_helper()
        work, done = app._run_operation.call_args.args[1:]
        log = mock.Mock()
        with mock.patch.object(gui, 'run_command') as command, \
                mock.patch.object(gui, 'gui_env', return_value={'SUDO_ASKPASS': '/fixture'}), \
                mock.patch.object(memory, 'helper_error', return_value=None):
            work(log)
        command.assert_called_once_with(['/usr/bin/sudo', '-A', '--', '/usr/bin/bash',
                                        str(gui.SCRIPT_DIR / 'install-memory-helper.sh')],
                                       {'SUDO_ASKPASS': '/fixture'}, log)
        with mock.patch.object(gui.messagebox, 'showinfo') as success:
            done(True)
        success.assert_called_once()
        app.refresh_memory.assert_called_once()
        with mock.patch.object(gui, 'run_command', side_effect=RuntimeError('Authentication failed')):
            with self.assertRaisesRegex(RuntimeError, 'Authentication failed'):
                work(log)
        with mock.patch.object(gui.messagebox, 'showerror') as error:
            done(False)
        error.assert_called_once()
        with mock.patch.object(gui, 'run_command'), mock.patch.object(gui, 'gui_env', return_value={}), \
                mock.patch.object(memory, 'helper_error', return_value='Wrong installed copy'):
            with self.assertRaisesRegex(RuntimeError, 'verification failed'):
                work(log)

    def test_setup_missing_script_never_authenticates(self):
        app = self.app()
        with mock.patch.object(gui.Path, 'is_file', return_value=False), \
                mock.patch.object(gui.messagebox, 'showerror') as error:
            app._install_memory_helper()
        error.assert_called_once()
        app._run_operation.assert_not_called()

    def test_real_layout(self):
        try:
            root = gui.tk.Tk()
        except gui.tk.TclError as error:
            self.skipTest(f'GUI display unavailable: {error}')
        self.addCleanup(root.destroy)
        app = gui.KernelManagerApp.__new__(gui.KernelManagerApp)
        app.memory_tab = gui.ttk.Frame(root)
        app.memory_tab.pack(fill='both', expand=True)
        app._read_async = mock.Mock()
        app._build_memory_tab()
        for size in ('960x700', '820x600'):
            root.geometry(size)
            root.update()
            canvas = next(w for w in app.memory_tab.winfo_children() if isinstance(w, gui.tk.Canvas))
            canvas.yview_moveto(1)
            root.update()
            self.assertTrue(app.memory_details.winfo_ismapped())
            self.assertLessEqual(app.memory_details.winfo_rooty() + app.memory_details.winfo_height(),
                                 canvas.winfo_rooty() + canvas.winfo_height())
