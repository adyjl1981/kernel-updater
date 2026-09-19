"""All writes use temporary fixtures. Privileged GUI commands are mocked."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import grub_menu as menu

spec = importlib.util.spec_from_file_location('menu_gui', Path(__file__).resolve().parents[1] / 'kernel-manager-gui.py')
gui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gui)


class ParsingTests(unittest.TestCase):
    def test_current_choices_and_legacy(self):
        cases = [('', '5'), ('GRUB_TIMEOUT=15\n', '15'),
                 ('GRUB_TIMEOUT_STYLE=hidden\nGRUB_TIMEOUT=0\n', 'hidden'),
                 ('GRUB_TIMEOUT_STYLE=hidden\nGRUB_TIMEOUT=3\n', 'hidden'),
                 ('GRUB_TIMEOUT=0\n', 'hidden'),
                 ('export GRUB_TIMEOUT_STYLE="menu" # menu\nGRUB_TIMEOUT=\'5\'\n', '5'),
                 ('GRUB_HIDDEN_TIMEOUT=0\nGRUB_HIDDEN_TIMEOUT_QUIET=true\nGRUB_TIMEOUT=0\n', 'hidden'),
                 ('GRUB_TIMEOUT_STYLE=countdown\nGRUB_TIMEOUT=5\n', None),
                 ('GRUB_TIMEOUT=7\n', None), ('GRUB_TIMEOUT=-1\n', None)]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(menu.current_choice(text), expected)

    def test_all_choices_preserve_saved_default_comments_and_other_settings(self):
        before = ('# machine settings\nGRUB_DEFAULT=saved # persistent kernel\n'
                  'GRUB_SAVEDEFAULT=true\nGRUB_RECORDFAIL_TIMEOUT=30\n'
                  'GRUB_CMDLINE_LINUX_DEFAULT="quiet splash"\n\n'
                  '  export GRUB_TIMEOUT_STYLE="hidden"  # style\n'
                  "GRUB_TIMEOUT='0' # delay\nGRUB_HIDDEN_TIMEOUT=0\nGRUB_HIDDEN_TIMEOUT_QUIET=true\n")
        for choice, (style, timeout) in menu.CHOICES.items():
            with self.subTest(choice=choice):
                after = menu.menu_config(before, choice)
                self.assertEqual(menu.current_choice(after), choice)
                self.assertTrue(after.startswith(before.split('  export')[0]))
                self.assertIn(f'  export GRUB_TIMEOUT_STYLE="{style}"  # style\n', after)
                self.assertIn(f"GRUB_TIMEOUT='{timeout}' # delay\n", after)
                self.assertIn('# Disabled by Kernel Manager (legacy): GRUB_HIDDEN_TIMEOUT=0', after)
                self.assertEqual(menu.menu_config(after, choice), after)

    def test_missing_keys_and_no_final_newline(self):
        for text in ('', '# comment', 'GRUB_DEFAULT=saved', 'GRUB_TIMEOUT=15'):
            self.assertEqual(menu.current_choice(menu.menu_config(text, '5')), '5')

    def test_malformed_duplicate_and_dynamic_settings_refused(self):
        for text in ('GRUB_TIMEOUT =5\n', 'GRUB_TIMEOUT="5\n',
                     'GRUB_TIMEOUT=5\nexport GRUB_TIMEOUT=15\n',
                     'GRUB_TIMEOUT=$(touch /tmp/never)\n', 'GRUB_TIMEOUT=5; echo bad\n',
                     'GRUB_TIMEOUT=banana\n', 'GRUB_TIMEOUT=5#not-a-comment\n',
                     'if false; then\nGRUB_TIMEOUT=5\nfi\n',
                     'GRUB_HIDDEN_TIMEOUT=0\nGRUB_HIDDEN_TIMEOUT=5\n',
                     'GRUB_TIMEOUT_STYLE=menu\nGRUB_TIMEOUT_STYLE=hidden\n'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                menu.menu_config(text, '5')
        with self.assertRaises(ValueError):
            menu.menu_config('', 'invalid')

    def test_drop_in_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'override.cfg'
            file.write_text('# GRUB_TIMEOUT=0\nGRUB_CMDLINE_LINUX_DEFAULT="quiet"\n')
            menu.check_overrides(directory)
            file.write_text('GRUB_TIMEOUT=10\n')
            with self.assertRaisesRegex(ValueError, 'overrides'):
                menu.check_overrides(directory)


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.defaults = self.root / 'grub'
        self.generated = self.root / 'grub.cfg'
        self.original = '# Keep\nGRUB_DEFAULT=saved\nGRUB_TIMEOUT_STYLE=hidden\nGRUB_TIMEOUT=0\n'
        self.defaults.write_text(self.original)
        self.defaults.chmod(0o640)
        self.generated.write_text('original generated menu\n')
        self.env = self.root / 'grubenv'
        self.env.write_text('saved_entry=kernel-123\n')

    def apply(self, updater=lambda: None):
        return menu.apply_setting(self.defaults, self.generated, self.original, '15', updater)

    def test_backup_and_all_choices(self):
        for choice in menu.CHOICES:
            with self.subTest(choice=choice):
                self.defaults.write_text(self.original)
                updater = mock.Mock()
                result = menu.apply_setting(self.defaults, self.generated, self.original, choice, updater)
                self.assertIn('Backups:', result)
                updater.assert_called_once_with()
                self.assertEqual(menu.current_choice(self.defaults.read_text()), choice)
                self.assertIn('GRUB_DEFAULT=saved\n', self.defaults.read_text())
                self.assertEqual(self.env.read_text(), 'saved_entry=kernel-123\n')
                self.assertEqual(self.defaults.stat().st_mode & 0o777, 0o640)
        backups = list(self.root.glob('grub.kernel-manager-backup-*'))
        self.assertEqual(len(backups), 3)
        self.assertTrue(all(path.read_text() == self.original for path in backups))

    def test_update_failure_restores_both_files(self):
        def fail():
            self.generated.write_text('partial generated output')
            raise subprocess.CalledProcessError(1, 'update-grub')
        with self.assertRaisesRegex(RuntimeError, 'Previous defaults and generated menu restored'):
            self.apply(fail)
        self.assertEqual(self.defaults.read_text(), self.original)
        self.assertEqual(self.generated.read_text(), 'original generated menu\n')

    def test_backup_failure_never_writes_or_updates(self):
        updater = mock.Mock()
        with mock.patch.object(menu, 'backup_file', side_effect=PermissionError('backup denied')):
            with self.assertRaises(PermissionError):
                self.apply(updater)
        updater.assert_not_called()
        self.assertEqual(self.defaults.read_text(), self.original)

    def test_modification_failure_rolls_back(self):
        real_write = menu.atomic_write
        calls = []
        def write(*args):
            calls.append(args)
            if len(calls) == 1:
                raise OSError('write failed')
            real_write(*args)
        updater = mock.Mock()
        with mock.patch.object(menu, 'atomic_write', side_effect=write), self.assertRaisesRegex(RuntimeError, 'restored'):
            self.apply(updater)
        updater.assert_not_called()
        self.assertEqual(self.defaults.read_text(), self.original)

    def test_generated_backup_failure_never_modifies_defaults(self):
        real_backup = menu.backup_file
        def backup(path, suffix):
            if path == self.generated:
                raise OSError('generated backup failed')
            return real_backup(path, suffix)
        updater = mock.Mock()
        with mock.patch.object(menu, 'backup_file', side_effect=backup), self.assertRaises(OSError):
            self.apply(updater)
        self.assertEqual(self.defaults.read_text(), self.original)
        updater.assert_not_called()

    def test_rollback_failure_identifies_backups(self):
        with mock.patch.object(menu, 'atomic_write', side_effect=OSError('disk failed')):
            with self.assertRaisesRegex(RuntimeError, 'Rollback incomplete:.*Backups:'):
                self.apply()
        self.assertTrue(list(self.root.glob('grub.kernel-manager-backup-*')))

    def test_concurrent_edit_is_not_overwritten(self):
        self.defaults.write_text('external edit')
        with self.assertRaisesRegex(RuntimeError, 'changed since confirmation'):
            self.apply()
        self.assertEqual(self.defaults.read_text(), 'external edit')
        self.assertFalse(list(self.root.glob('*backup*')))

    def test_edit_during_update_refuses_rollback_over_external_changes(self):
        def update():
            self.defaults.write_text('external edit')
        with self.assertRaisesRegex(RuntimeError, 'changed externally'):
            self.apply(update)
        self.assertEqual(self.defaults.read_text(), 'external edit')

    def test_symlink_refused(self):
        link = self.root / 'link'
        link.symlink_to(self.defaults)
        with self.assertRaisesRegex(RuntimeError, 'regular file'):
            menu.apply_setting(link, self.generated, self.original, '5', mock.Mock())


class GuiTests(unittest.TestCase):
    def setUp(self):
        self.app = gui.KernelManagerApp.__new__(gui.KernelManagerApp)
        self.app.grub_menu_original = 'GRUB_DEFAULT=saved\nGRUB_TIMEOUT=5\n'
        self.app.grub_menu_choice = mock.Mock()
        self.app.grub_menu_choice.get.return_value = '15'
        self.app.grub_menu_apply_btn = mock.Mock()
        self.app.grub_menu_status = mock.Mock()
        self.app._run_operation = mock.Mock()

    def test_radio_choices_share_one_variable_and_have_no_write_callback(self):
        self.app.tools_tab = mock.Mock()
        self.app.refresh_grub_menu = mock.Mock()
        with mock.patch.object(gui.ttk, 'LabelFrame'), mock.patch.object(gui.ttk, 'Frame'), \
                mock.patch.object(gui.ttk, 'Label'), mock.patch.object(gui.ttk, 'Button'), \
                mock.patch.object(gui.ttk, 'Radiobutton') as radio, \
                mock.patch.object(gui.tk, 'StringVar'):
            self.app._build_tools_tab()
        self.assertEqual([call.kwargs['value'] for call in radio.call_args_list], ['hidden', '5', '15'])
        for call in radio.call_args_list:
            self.assertIs(call.kwargs['variable'], self.app.grub_menu_choice)
            self.assertNotIn('command', call.kwargs)
        self.app.refresh_grub_menu.assert_called_once_with()
        self.app._run_operation.assert_not_called()

    def test_failed_refresh_clears_old_selection_and_disables_apply(self):
        self.app._read_async = lambda title, work, done: done(work())
        with mock.patch.object(menu, 'check_overrides', side_effect=ValueError('conflicting settings')):
            self.app.refresh_grub_menu()
        self.app.grub_menu_choice.set.assert_called_with('')
        self.app.grub_menu_apply_btn.configure.assert_called_with(state='disabled')
        self.assertIsNone(self.app.grub_menu_original)

    def test_confirmation_precedes_authentication_and_lists_exact_changes(self):
        with mock.patch.object(gui.messagebox, 'askyesno', return_value=False) as confirm:
            self.app.on_apply_grub_menu()
        self.assertIn('-GRUB_TIMEOUT=5', confirm.call_args.args[1])
        self.assertIn('+GRUB_TIMEOUT=15', confirm.call_args.args[1])
        self.assertIn('+GRUB_TIMEOUT_STYLE=menu', confirm.call_args.args[1])
        self.app._run_operation.assert_not_called()

    def test_auth_failure_uses_askpass_without_direct_file_writes(self):
        with mock.patch.object(gui.messagebox, 'askyesno', return_value=True):
            self.app.on_apply_grub_menu()
        worker = self.app._run_operation.call_args.args[1]
        def denied(command, env, log):
            self.assertEqual(command[:2], ['sudo', '-A'])
            self.assertEqual(env, {'SUDO_ASKPASS': '/mock/askpass'})
            request = json.loads(Path(command[-1]).read_text())
            self.assertEqual(request['choice'], '15')
            self.assertEqual(request['original'], self.app.grub_menu_original)
            raise RuntimeError('authentication failed')
        with mock.patch.object(gui, 'gui_env', return_value={'SUDO_ASKPASS': '/mock/askpass'}), \
                mock.patch.object(gui, 'run_command', side_effect=denied), \
                mock.patch.object(menu, 'apply_setting') as apply:
            with self.assertRaisesRegex(RuntimeError, 'authentication failed'):
                worker(mock.Mock())
        apply.assert_not_called()

    def test_refresh_parses_configuration_without_authentication(self):
        self.app._read_async = lambda title, work, done: done(work())
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'grub'
            for choice in menu.CHOICES:
                config.write_text(menu.menu_config('', choice))
                with mock.patch.object(gui, 'GRUB_DEFAULTS_FILE', config), \
                        mock.patch.object(menu, 'check_overrides'), mock.patch.object(gui, 'run_command') as run:
                    self.app.refresh_grub_menu()
                self.app.grub_menu_choice.set.assert_called_with(choice)
                run.assert_not_called()
        self.app._run_operation.assert_not_called()

    def test_success_and_failure_are_reported_and_refreshed(self):
        with mock.patch.object(gui.messagebox, 'askyesno', return_value=True):
            self.app.on_apply_grub_menu()
        completed = self.app._run_operation.call_args.args[2]
        self.app.refresh_grub_menu = mock.Mock()
        with mock.patch.object(gui.messagebox, 'showinfo') as info, mock.patch.object(gui.messagebox, 'showerror') as error:
            completed(True)
            info.assert_called_once()
            completed(False)
            error.assert_called_once()
        self.assertEqual(self.app.refresh_grub_menu.call_count, 2)


if __name__ == '__main__':
    unittest.main()
