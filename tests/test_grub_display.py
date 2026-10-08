"""No system writes or real authentication/regeneration in these tests."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import grub_menu as grub
import test_grub_menu as existing
gui = existing.gui


class DisplayParsingTests(unittest.TestCase):
    def test_modes(self):
        for value, expected in [('auto', 'auto'), (' 1920x1080 ', '1920x1080,auto'),
                                ('800x600x16', '800x600x16,auto'), ('3840x2160', '3840x2160,auto')]:
            self.assertEqual(grub.validate_mode(value), expected)
        for value in ['', '0x1080', '1920X1080', '1920x0', '-1x600', '1920x1080,auto',
                      'auto;reboot', '$(id)', '800x600x99', '100000x100000', None, '800x600\nreboot']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                grub.validate_mode(value)

    def test_preservation_and_payload_default(self):
        original = '# GRUB_GFXMODE=640x480\nGRUB_DEFAULT=saved\n  export GRUB_GFXMODE=\'800x600\' # mode\nGRUB_GFXPAYLOAD_LINUX="text" # payload\n'
        updated = grub.display_config(original, ('1920x1080', True))
        self.assertTrue(updated.startswith(original.split('  export')[0]))
        self.assertIn('  export GRUB_GFXMODE="1920x1080,auto" # mode', updated)
        self.assertEqual(grub.display_values(updated), {'GRUB_GFXMODE': '1920x1080,auto', 'GRUB_GFXPAYLOAD_LINUX': 'keep'})
        self.assertEqual(grub.display_config(updated, ('1920x1080', True)), updated)
        default = grub.display_config(updated, ('auto', False))
        self.assertNotIn('GRUB_GFXPAYLOAD_LINUX', grub.display_values(default))
        self.assertIn('# payload', default)
        self.assertEqual(grub.display_config(default, ('auto', False)), default)

    def test_custom_current_values_and_missing_newline(self):
        self.assertEqual(grub.display_values('GRUB_GFXMODE="800x600;640x480,auto"'), {'GRUB_GFXMODE': '800x600;640x480,auto'})
        self.assertEqual(grub.display_values(grub.display_config('# comment', ('auto', False))), {'GRUB_GFXMODE': 'auto'})
        self.assertEqual(grub.display_values(''), {})

    def test_unsafe_and_duplicate_definitions(self):
        for text in ['GRUB_GFXMODE=auto\nexport GRUB_GFXMODE=auto\n', 'GRUB_GFXMODE=$(id)\n',
                     'GRUB_GFXMODE=auto;reboot\n', 'GRUB_GFXMODE="${MODE}"\n', 'GRUB_GFXMODE =auto\n',
                     'source /other\n', 'GRUB_GFXPAYLOAD_LINUX=keep\nGRUB_GFXPAYLOAD_LINUX=text\n']:
            with self.subTest(text=text), self.assertRaises(ValueError):
                grub.display_config(text, ('auto', False))
        with self.assertRaises(ValueError):
            grub.display_config('', ('auto', 'false'))

    def test_dropin_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)/'custom.cfg'
            p.write_text('# GRUB_GFXMODE=auto\nGRUB_TIMEOUT=5\n')
            grub.check_overrides(tmp, grub.DISPLAY_PATTERN)
            p.write_text('GRUB_GFXMODE=800x600\n')
            with self.assertRaises(ValueError):
                grub.check_overrides(tmp, grub.DISPLAY_PATTERN)


class DisplayTransactionTests(existing.TransactionTests):
    def apply(self, updater=lambda: None):
        return grub.apply_setting(self.defaults, self.generated, self.original,
                                  ('1920x1080', True), updater, grub.display_config)

    def test_display_backup_and_metadata(self):
        self.apply()
        self.assertEqual(grub.display_values(self.defaults.read_text())['GRUB_GFXMODE'], '1920x1080,auto')
        self.assertEqual(self.defaults.stat().st_mode & 0o777, 0o640)
        self.assertEqual(next(self.root.glob('grub.kernel-manager-backup-*')).read_text(), self.original)

    def test_invalid_request_before_backup(self):
        updater = mock.Mock()
        with self.assertRaises(ValueError):
            grub.apply_setting(self.defaults, self.generated, self.original, ('auto;reboot', True), updater, grub.display_config)
        updater.assert_not_called()
        self.assertFalse(list(self.root.glob('*backup*')))

    def test_atomic_replace_failure_leaves_original_and_cleans_stage(self):
        with mock.patch.object(grub.os, 'replace', side_effect=OSError('replace failed')):
            with self.assertRaises(OSError):
                grub.atomic_write(self.defaults, b'new', self.generated)
        self.assertEqual(self.defaults.read_text(), self.original)
        self.assertFalse(list(self.root.glob('.grub.kernel-manager-*')))


class RegenerationTests(unittest.TestCase):
    def test_known_layouts_and_missing_tools(self):
        for folder, tool in [('grub', 'update-grub'), ('grub', 'grub-mkconfig'), ('grub2', 'grub2-mkconfig')]:
            with tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp)/folder/'grub.cfg'
                target.parent.mkdir(); target.write_text('menu')
                path, command = grub.regeneration_plan(Path(tmp), lambda name: '/usr/sbin/'+name if name == tool else None)
                self.assertEqual(path, target)
                self.assertEqual(command, ['/usr/sbin/'+tool] + ([] if tool == 'update-grub' else ['-o', str(target)]))
                with self.assertRaises(RuntimeError):
                    grub.regeneration_plan(Path(tmp), lambda name: None)

    def test_ambiguous_and_efi_only_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'efi/EFI/fedora').mkdir(parents=True)
            (root/'efi/EFI/fedora/grub.cfg').write_text('forwarding stub')
            with self.assertRaises(RuntimeError):
                grub.regeneration_plan(root, lambda name: name)
            for folder in ('grub', 'grub2'):
                (root/folder).mkdir(); (root/folder/'grub.cfg').write_text('menu')
            with self.assertRaises(RuntimeError):
                grub.regeneration_plan(root, lambda name: name)


class DisplayGuiTests(unittest.TestCase):
    def setUp(self):
        self.app = gui.KernelManagerApp.__new__(gui.KernelManagerApp)
        for name in ('grub_display_mode', 'grub_display_keep', 'grub_display_status', 'grub_display_apply_btn', '_run_operation'):
            setattr(self.app, name, mock.Mock())
        self.app.grub_display_original = 'GRUB_GFXMODE="1920x1080,auto"\nGRUB_GFXPAYLOAD_LINUX=keep\n'
        self.app.grub_display_mode.get.return_value = '1920x1080'
        self.app.grub_display_keep.get.return_value = True

    def test_layout_separate_payload_and_explicit_action(self):
        self.app.refresh_grub_display = mock.Mock()
        with mock.patch.object(gui.ttk, 'LabelFrame') as frame, mock.patch.object(gui.ttk, 'Frame'), \
                mock.patch.object(gui.ttk, 'Label') as labels, mock.patch.object(gui.ttk, 'Combobox') as entry, \
                mock.patch.object(gui.ttk, 'Checkbutton') as check, mock.patch.object(gui.ttk, 'Button') as button, \
                mock.patch.object(gui.tk, 'StringVar'), mock.patch.object(gui.tk, 'BooleanVar'):
            self.app._build_grub_display(mock.Mock())
        self.assertEqual(frame.call_args.kwargs['text'], 'GRUB Display Resolution')
        self.assertIs(entry.call_args.kwargs['textvariable'], self.app.grub_display_mode)
        self.assertEqual(entry.call_args.kwargs['state'], 'readonly')
        self.assertEqual(entry.call_args.kwargs['values'], ('auto', '1024x768', '1280x720', '1920x1080'))
        self.assertIs(check.call_args.kwargs['variable'], self.app.grub_display_keep)
        self.assertNotIn('command', check.call_args.kwargs)
        self.assertEqual(button.call_args_list[0].kwargs['command'], self.app.on_apply_grub_display)
        self.assertIn('does not prove', ' '.join(c.kwargs.get('text', '') for c in labels.call_args_list))
        self.app._run_operation.assert_not_called()

    def test_cancel_and_invalid_do_not_authenticate(self):
        with mock.patch.object(gui.messagebox, 'askyesno', return_value=False):
            self.app.on_apply_grub_display()
        self.app._run_operation.assert_not_called()
        self.app.grub_display_mode.get.return_value = '$(id)'
        with mock.patch.object(gui.messagebox, 'showerror'), mock.patch.object(gui.messagebox, 'askyesno') as confirm:
            self.app.on_apply_grub_display()
        confirm.assert_not_called()
        self.app._run_operation.assert_not_called()

    def test_auth_request_failure_and_cleanup(self):
        with mock.patch.object(gui.messagebox, 'askyesno', return_value=True):
            self.app.on_apply_grub_display()
        paths = []
        def denied(command, env, log):
            self.assertEqual(command[:2], ['sudo', '-A'])
            paths.append(Path(command[-1]))
            request = json.loads(paths[-1].read_text())
            self.assertEqual(request['action'], 'display')
            self.assertEqual(request['mode'], '1920x1080')
            self.assertIs(request['keep'], True)
            raise RuntimeError('authentication failed')
        with mock.patch.object(gui, 'run_command', side_effect=denied), mock.patch.object(gui, 'gui_env', return_value={}):
            with self.assertRaises(RuntimeError):
                self.app._run_operation.call_args.args[1](mock.Mock())
        self.assertFalse(paths[0].exists())
        self.app.refresh_grub_display = mock.Mock()
        with mock.patch.object(gui.messagebox, 'showinfo') as info, mock.patch.object(gui.messagebox, 'showerror') as error:
            done = self.app._run_operation.call_args.args[2]
            done(True); done(False)
            info.assert_called_once(); error.assert_called_once()

    def test_refresh_active_values_and_error_clears_snapshot(self):
        self.app._read_async = lambda title, work, done: done(work())
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)/'grub'; p.write_text(self.app.grub_display_original)
            with mock.patch.object(gui, 'GRUB_DEFAULTS_FILE', p), mock.patch.object(grub, 'check_overrides'):
                self.app.refresh_grub_display()
            self.app.grub_display_mode.set.assert_called_with('1920x1080')
            self.app.grub_display_keep.set.assert_called_with(True)
            self.assertIn('1920x1080,auto', self.app.grub_display_status.configure.call_args.kwargs['text'])
            p.write_text('GRUB_GFXMODE=1600x900\n')
            with mock.patch.object(gui, 'GRUB_DEFAULTS_FILE', p), mock.patch.object(grub, 'check_overrides'):
                self.app.refresh_grub_display()
            self.app.grub_display_mode.set.assert_called_with('auto')
            self.assertIn('1600x900', self.app.grub_display_status.configure.call_args.kwargs['text'])
        with mock.patch.object(grub, 'check_overrides', side_effect=ValueError('override')):
            self.app.refresh_grub_display()
        self.assertIsNone(self.app.grub_display_original)
        self.app.grub_display_apply_btn.configure.assert_called_with(state='disabled')


class DisplayRealLayoutTests(unittest.TestCase):
    def test_scroll_reaches_apply_at_small_and_default_sizes(self):
        try:
            root = gui.tk.Tk()
        except gui.tk.TclError as error:
            self.skipTest(f'GUI display unavailable: {error}')
        self.addCleanup(root.destroy)
        app = gui.KernelManagerApp.__new__(gui.KernelManagerApp)
        app.tools_tab = gui.ttk.Frame(root)
        app.tools_tab.pack(fill='both', expand=True)
        app._read_async = mock.Mock()
        app._build_tools_tab()
        canvas = next(w for w in app.tools_tab.winfo_children() if isinstance(w, gui.tk.Canvas))
        for size in ('960x600', '800x450'):
            root.geometry(size)
            root.update()
            canvas.yview_moveto(1)
            root.update()
            button = app.grub_display_apply_btn
            self.assertTrue(button.winfo_ismapped())
            top = button.winfo_rooty()
            self.assertGreaterEqual(top, canvas.winfo_rooty())
            self.assertLessEqual(top + button.winfo_height(), canvas.winfo_rooty() + canvas.winfo_height())
