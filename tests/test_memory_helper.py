"""Privileged logic runs only against temporary fixtures with a fake runner."""
import contextlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

import memory_helper as helper
import memory_management as memory


class HelperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.calls = []
        self.write('proc/sys/vm/swappiness', '60\n')
        self.write('proc/sys/kernel/random/boot_id', 'fixture-boot\n')
        self.write('proc/swaps', 'Filename Type Size Used Priority\n/swapfile file 1048576 131072 -2\n')
        self.write('proc/meminfo', 'MemTotal: 8388608 kB\nMemAvailable: 6291456 kB\n')
        self.write('proc/cmdline', 'quiet')
        self.write('sys/power/resume', '0:0\n')
        self.write('sys/power/state', 'freeze mem disk\n')
        self.write('etc/fstab', '/swapfile none swap defaults 0 0\n')
        self.write('etc/sysctl.conf', '# administrator settings\n')
        (self.root/'etc/sysctl.d').mkdir(mode=0o755)
        (self.root/helper.STATE).mkdir(parents=True, mode=0o700)
        self.write('swapfile', 'mock swap data')
        self.ops = helper.Operations(self.root, runner=self.fake_swap, checker=self.check)

    def write(self, name, text):
        p = self.root/name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.parent.chmod(0o755)
        p.write_text(text)
        p.chmod(0o644)

    def check(self, p, directory=False, block=False):
        # Fixture ownership differs from root. Keep type, symlink, mode checks.
        self.assertTrue(p.is_relative_to(self.root))
        info = p.lstat()
        if p.is_symlink() or info.st_mode & (0o002 if block else 0o022) or (directory and not p.is_dir()):
            raise helper.Refused('untrusted fixture')
        return info

    def fake_swap(self, command):
        self.calls.append(command)
        self.assertIn(command[0], ('/usr/sbin/swapon', '/usr/sbin/swapoff'))
        self.assertEqual(command[-2:], ['--', str(self.root/'swapfile')])
        self.write('proc/swaps', 'Filename Type Size Used Priority\n' +
                   ('/swapfile file 1048576 0 -2\n' if command[0].endswith('swapon') else ''))

    def swappiness(self, value=80, persistent=False, expected=60):
        return {'protocol': 1, 'action': 'swappiness', 'value': value,
                'persistent': persistent, 'expected': expected}

    def toggle(self, enable=False, expected=True):
        return {'protocol': 1, 'action': 'swap', 'path': '/swapfile', 'enable': enable,
                'expected_active': expected, 'identity': helper.identity((self.root/'swapfile').stat())}

    def restore(self, expected=80):
        return {'protocol': 1, 'action': 'restore_swappiness', 'expected': expected}

    def test_temporary_change_backup_restore_and_no_config_write(self):
        self.ops.execute(self.swappiness())
        self.assertEqual(self.ops.current(), 80)
        self.assertFalse((self.root/helper.CONFIG).exists())
        self.assertEqual(self.ops.load('swappiness.json')['old_value'], 60)
        self.ops.execute(self.restore())
        self.assertEqual(self.ops.current(), 60)
        self.assertIsNone(self.ops.load('swappiness.json'))
        self.assertEqual(self.calls, [])

    def test_persistent_create_update_and_restore_previous_value(self):
        self.ops.execute(self.swappiness(persistent=True))
        self.assertIn('vm.swappiness=80', self.ops.read(helper.CONFIG))
        self.assertEqual((self.root/helper.CONFIG).stat().st_mode & 0o777, 0o644)
        self.ops.execute(self.swappiness(value=90, persistent=True, expected=80))
        self.ops.execute(self.restore(expected=90))
        self.assertEqual(self.ops.current(), 80)
        self.assertIn('vm.swappiness=80', self.ops.read(helper.CONFIG))

    def test_persistent_restore_removes_only_new_managed_file(self):
        self.ops.execute(self.swappiness(persistent=True))
        self.ops.execute(self.restore())
        self.assertFalse((self.root/helper.CONFIG).exists())
        self.assertEqual(self.ops.current(), 60)
        self.assertEqual(self.ops.read('etc/sysctl.conf'), '# administrator settings\n')

    def test_conflicts_stale_state_and_unmanaged_file_refuse_before_backup(self):
        self.write('etc/sysctl.d/50-custom.conf', 'vm.swappiness = 40\n')
        with self.assertRaisesRegex(helper.Refused, 'conflicts'):
            self.ops.execute(self.swappiness(persistent=True))
        self.assertIsNone(self.ops.load('swappiness.json'))
        self.assertEqual(self.ops.current(), 60)
        self.ops.execute(self.swappiness())  # runtime-only still works
        with self.assertRaisesRegex(helper.Refused, 'changed since'):
            self.ops.execute(self.swappiness())
        self.write(helper.CONFIG, 'vm.swappiness=100\n')
        with self.assertRaisesRegex(helper.Refused, 'unmanaged'):
            self.ops.execute(self.swappiness(expected=80, persistent=True))

    def test_persistence_refuses_slash_and_glob_overrides(self):
        for definition in ('vm/swappiness=10', 'vm.swapp*=20', '-vm.* = 30'):
            with self.subTest(definition=definition):
                self.write('etc/sysctl.conf', definition+'\n')
                with self.assertRaisesRegex(helper.Refused, 'conflicts'):
                    self.ops.execute(self.swappiness(persistent=True))
                self.assertEqual(self.ops.current(), 60)
                self.assertIsNone(self.ops.load('swappiness.json'))

    def test_failed_runtime_write_rolls_back_config_and_retains_prior_backup(self):
        self.ops.execute(self.swappiness())
        old_record = self.ops.load('swappiness.json')
        original = self.ops.set_runtime
        def fail(value):
            if value == 90:
                raise OSError('kernel rejected request')
            return original(value)
        with mock.patch.object(self.ops, 'set_runtime', side_effect=fail):
            with self.assertRaisesRegex(helper.Refused, 'Previous setting restored'):
                self.ops.execute(self.swappiness(value=90, persistent=True, expected=80))
        self.assertEqual(self.ops.current(), 80)
        self.assertFalse((self.root/helper.CONFIG).exists())
        self.assertEqual(self.ops.load('swappiness.json'), old_record)

    def test_temporary_change_does_not_read_or_edit_unmanaged_dedicated_config(self):
        self.write(helper.CONFIG, '# custom configuration\nvm.swappiness=55\n')
        before = self.ops.read(helper.CONFIG)
        self.ops.execute(self.swappiness())
        self.ops.execute(self.restore())
        self.assertEqual(self.ops.read(helper.CONFIG), before)
        self.assertEqual(self.ops.current(), 60)

    def test_managed_configuration_mode_is_preserved(self):
        self.write(helper.CONFIG, helper.MANAGED+'vm.swappiness=60\n')
        (self.root/helper.CONFIG).chmod(0o640)
        self.ops.execute(self.swappiness(persistent=True))
        self.assertEqual((self.root/helper.CONFIG).stat().st_mode & 0o777, 0o640)
        self.ops.execute(self.restore())
        self.assertEqual((self.root/helper.CONFIG).stat().st_mode & 0o777, 0o640)

    def test_rollback_does_not_clobber_concurrent_external_change(self):
        def concurrent_change(value):
            self.write('proc/sys/vm/swappiness', '35\n')
            self.write(helper.CONFIG, '# external change\n')
            raise OSError('concurrent writer')
        with mock.patch.object(self.ops, 'set_runtime', side_effect=concurrent_change):
            with self.assertRaisesRegex(helper.Refused, 'Recovery incomplete'):
                self.ops.execute(self.swappiness(persistent=True))
        self.assertEqual(self.ops.current(), 35)
        self.assertEqual(self.ops.read(helper.CONFIG), '# external change\n')
        self.assertEqual(self.ops.load('swappiness.json')['phase'], 'prepared')

    def test_atomic_failure_leaves_original_and_cleans_stage(self):
        self.ops.execute(self.swappiness(persistent=True))
        before = self.ops.read(helper.CONFIG)
        with mock.patch.object(helper.os, 'replace', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                self.ops.atomic(helper.CONFIG, b'bad', 0o644)
        self.assertEqual(self.ops.read(helper.CONFIG), before)
        self.assertFalse(list((self.root/'etc/sysctl.d').glob('.memory-*')))

    def test_incomplete_rollback_retains_retryable_recovery_record(self):
        with mock.patch.object(self.ops, 'set_runtime', side_effect=OSError('write denied')):
            with self.assertRaisesRegex(helper.Refused, 'Recovery incomplete'):
                self.ops.execute(self.swappiness(persistent=True))
        self.assertEqual(self.ops.load('swappiness.json')['phase'], 'prepared')
        with self.assertRaisesRegex(helper.Refused, 'interrupted'):
            self.ops.execute(self.swappiness())
        self.ops.execute(self.restore(expected=60))
        self.assertIsNone(self.ops.load('swappiness.json'))

    def test_restore_refuses_external_changes_and_previous_boot(self):
        self.ops.execute(self.swappiness(persistent=True))
        self.write('proc/sys/vm/swappiness', '75\n')
        with self.assertRaisesRegex(helper.Refused, 'externally'):
            self.ops.execute(self.restore(expected=75))
        self.write('proc/sys/vm/swappiness', '80\n')
        self.write(helper.CONFIG, helper.MANAGED+'vm.swappiness=99\n')
        with self.assertRaisesRegex(helper.Refused, 'configuration changed'):
            self.ops.execute(self.restore())
        self.write(helper.CONFIG, helper.MANAGED+'vm.swappiness=80\n')
        self.write('proc/sys/kernel/random/boot_id', 'later-boot')
        with self.assertRaisesRegex(helper.Refused, 'earlier boot'):
            self.ops.execute(self.restore())

    def test_disable_enable_individual_no_fstab_edits_and_retained_priority(self):
        before = self.ops.read('etc/fstab')
        self.ops.execute(self.toggle())
        self.assertNotIn(self.root/'swapfile', self.ops.active_swaps())
        self.ops.execute(self.toggle(enable=True, expected=False))
        self.assertEqual(self.calls[0][0], '/usr/sbin/swapoff')
        self.assertEqual(self.calls[1][0], '/usr/sbin/swapon')
        self.assertEqual(self.ops.read('etc/fstab'), before)
        self.assertEqual((self.root/'swapfile').read_text(), 'mock swap data')

    def test_reenable_previously_disabled_unconfigured_swap(self):
        self.write('etc/fstab', '# unconfigured active swap\n')
        self.ops.execute(self.toggle())
        self.ops.execute(self.toggle(enable=True, expected=False))
        self.assertEqual(len(self.calls), 2)

    def test_saved_priority_takes_precedence_on_reenable(self):
        self.write('proc/swaps', 'Filename Type Size Used Priority\n/swapfile file 1048576 0 100\n')
        self.ops.execute(self.toggle())
        self.ops.execute(self.toggle(enable=True, expected=False))
        self.assertEqual(self.calls[-1][1:3], ['--priority', '100'])

    def test_swap_partition_and_initialized_zram_use_only_swap_tools(self):
        for source in ('/dev/sda2', '/dev/zram0'):
            with self.subTest(source=source):
                self.write(source.lstrip('/'), 'fixture block representation')
                target = self.ops.path(source)
                real_info = target.stat()
                block_info = mock.Mock(st_dev=real_info.st_dev, st_ino=real_info.st_ino,
                                       st_rdev=1234, st_size=0, st_mode=stat.S_IFBLK | 0o660,
                                       st_uid=0, st_nlink=1)
                old_lstat = Path.lstat
                def lstat(path):
                    return block_info if path == target else old_lstat(path)
                self.write('proc/swaps', 'Filename Type Size Used Priority\n')
                self.write('etc/fstab', '/dev/sda2 none swap pri=50 0 0\n')
                self.write('sys/block/zram0/disksize', '1048576')
                commands = []
                def fake(command):
                    commands.append(command)
                    self.write('proc/swaps', 'Filename Type Size Used Priority\n' +
                               (source+' partition 1024 0 50\n' if command[0].endswith('swapon') else ''))
                request = {'protocol': 1, 'action': 'swap', 'path': source, 'enable': True,
                           'expected_active': False, 'identity': helper.identity(block_info)}
                with mock.patch.object(Path, 'lstat', autospec=True, side_effect=lstat), \
                        mock.patch.object(self.ops, 'runner', side_effect=fake):
                    self.ops.execute(request)
                    self.ops.execute({**request, 'enable': False, 'expected_active': True})
                self.assertEqual([c[0] for c in commands], ['/usr/sbin/swapon', '/usr/sbin/swapoff'])
                self.assertTrue(all(c[-2:] == ['--', str(target)] for c in commands))
                self.assertEqual(target.read_text(), 'fixture block representation')

    def test_unknown_inactive_file_refused(self):
        self.write('etc/fstab', '# no configured swap\n')
        self.write('proc/swaps', 'Filename Type Size Used Priority\n')
        with self.assertRaisesRegex(helper.Refused, 'Enable supports'):
            self.ops.execute(self.toggle(enable=True, expected=False))
        self.assertEqual(self.calls, [])

    def test_insufficient_or_unknown_memory_never_calls_swapoff(self):
        for data in ('MemTotal: 8388608 kB\nMemAvailable: 1000 kB\n',
                     'MemTotal: 8388608 kB\n', 'MemTotal: 1 kB\nMemAvailable: 2 kB\n'):
            self.write('proc/meminfo', data)
            with self.assertRaises(helper.Refused):
                self.ops.execute(self.toggle())
            self.assertEqual(self.calls, [])

    def test_hibernation_hints_and_unknown_state_never_call_swapoff(self):
        for name, value in (('sys/power/resume', '8:2'), ('proc/cmdline', 'quiet resume=UUID=abc'),
                            ('etc/initramfs-tools/conf.d/resume', 'RESUME=UUID=abc')):
            original = self.ops.read(name, optional=True)
            self.write(name, value)
            with self.assertRaisesRegex(helper.Refused, 'hibernation|Hibernation'):
                self.ops.execute(self.toggle())
            self.write(name, original)
            self.assertEqual(self.calls, [])
        (self.root/'sys/power/resume').unlink()
        with self.assertRaisesRegex(helper.Refused, 'unavailable'):
            self.ops.execute(self.toggle())

    def test_stale_identity_state_and_untrusted_files_refuse(self):
        request = self.toggle()
        request['identity'][1] += 1
        with self.assertRaisesRegex(helper.Refused, 'changed since'):
            self.ops.execute(request)
        with self.assertRaisesRegex(helper.Refused, 'state changed'):
            self.ops.execute(self.toggle(expected=False))
        (self.root/'swapfile').chmod(0o666)
        with self.assertRaisesRegex(helper.Refused, 'untrusted'):
            self.ops.execute(self.toggle())
        self.assertEqual(self.calls, [])

    def test_symlink_config_and_target_refuse(self):
        (self.root/helper.CONFIG).symlink_to(self.root/'etc/sysctl.conf')
        with self.assertRaisesRegex(helper.Refused, 'untrusted'):
            self.ops.execute(self.swappiness(persistent=True))
        (self.root/'alias').symlink_to(self.root/'swapfile')
        request = self.toggle()
        request['path'] = '/alias'
        with self.assertRaisesRegex(helper.Refused, 'alias'):
            self.ops.execute(request)
        self.assertEqual(self.calls, [])

    def test_lock_contention_refuses_before_changes(self):
        with self.ops.locked():
            with self.assertRaisesRegex(helper.Refused, 'Another memory'):
                self.ops.execute(self.swappiness())
        self.assertEqual(self.ops.current(), 60)

    def test_failed_swap_command_retains_reenable_record(self):
        with mock.patch.object(self.ops, 'runner', side_effect=helper.Refused('swapoff: out of memory')):
            with self.assertRaisesRegex(helper.Refused, 'out of memory'):
                self.ops.execute(self.toggle())
        self.assertIn(self.root/'swapfile', self.ops.active_swaps())
        self.assertIn('/swapfile', self.ops.load('disabled.json'))

    def test_fixed_runner_errors_are_clear(self):
        with mock.patch.object(helper, 'trusted'), mock.patch.object(helper.subprocess, 'run',
                return_value=mock.Mock(returncode=2, stderr='Cannot allocate memory', stdout='')) as command:
            with self.assertRaisesRegex(helper.Refused, 'Cannot allocate memory'):
                self.ops.run(['/usr/sbin/swapoff', '--', '/fixture'])
        self.assertNotIn('shell', command.call_args.kwargs)
        self.assertEqual(command.call_args.kwargs['env'], helper.SAFE_ENV)


class ProtocolAndClientTests(unittest.TestCase):
    def test_request_schema_and_injection(self):
        good = {'protocol': 1, 'action': 'swappiness', 'value': 60, 'persistent': False, 'expected': 80}
        self.assertEqual(helper.validate_request(good), good)
        for update in ({'value': True}, {'value': 201}, {'value': '60;id'}, {'persistent': 'false'},
                       {'action': 'resize'}, {'filename': '/etc/passwd'}, {'protocol': 2}, {'protocol': True}, {'action': []}):
            with self.subTest(update=update), self.assertRaises(helper.Refused):
                helper.validate_request({**good, **update})
        for target in ('/../etc/passwd', '/x\nreboot', 'UUID=abc', '/x//y'):
            with self.subTest(target=target), self.assertRaises(helper.Refused):
                helper.validate_request({'protocol': 1, 'action': 'swap', 'path': target, 'enable': True,
                                         'expected_active': False, 'identity': [1, 2, 0, 100]})
        with self.assertRaisesRegex(helper.Refused, 'Duplicate'):
            json.loads('{"action":"swap","action":"other"}', object_pairs_hook=helper.unique_pairs)

    def test_fixed_client_stdin_authentication_error_and_missing_helper(self):
        request = {'protocol': 1, 'action': 'swappiness', 'value': 60, 'persistent': False, 'expected': 80}
        with mock.patch.object(memory, 'helper_error', return_value=None), mock.patch.object(memory.subprocess, 'run',
                return_value=mock.Mock(returncode=0, stdout='updated', stderr='')) as command:
            self.assertEqual(memory.request_change(request, {'SUDO_ASKPASS': '/fixture'}, mock.Mock()), 'updated')
            self.assertEqual(command.call_args.args[0], ['/usr/bin/sudo', '-A', '--', '/usr/bin/python3', '-I', '-S', str(memory.HELPER)])
            self.assertEqual(json.loads(command.call_args.kwargs['input']), request)
        with mock.patch.object(memory, 'helper_error', return_value=None), mock.patch.object(memory.subprocess, 'run',
                return_value=mock.Mock(returncode=1, stdout='', stderr='Authentication failed')):
            with self.assertRaisesRegex(RuntimeError, 'Authentication failed'):
                memory.request_change(request, {}, mock.Mock())
        with mock.patch.object(memory, 'helper_error', return_value='Setup required'), mock.patch.object(memory.subprocess, 'run') as command:
            with self.assertRaisesRegex(RuntimeError, 'Setup required'):
                memory.request_change(request, {}, mock.Mock())
            command.assert_not_called()

    def test_entry_point_cannot_execute_from_checkout(self):
        with mock.patch.object(helper.os, 'geteuid', return_value=0), mock.patch.object(helper.sys, 'stderr'):
            self.assertEqual(helper.main(), 1)

    def test_block_trust_accepts_only_privileged_write_groups(self):
        directory = mock.Mock(st_uid=0, st_mode=stat.S_IFDIR | 0o755)
        block = mock.Mock(st_uid=0, st_gid=6, st_mode=stat.S_IFBLK | 0o660)
        def info(path):
            return block if str(path) == '/dev/fixture' else directory
        with mock.patch.object(Path, 'lstat', autospec=True, side_effect=info), \
                mock.patch.object(helper.grp, 'getgrnam', return_value=mock.Mock(gr_gid=6)):
            helper.trusted(Path('/dev/fixture'), block=True)
            block.st_gid = 1000
            with self.assertRaisesRegex(helper.Refused, 'untrusted group'):
                helper.trusted(Path('/dev/fixture'), block=True)
            block.st_gid = 6
            block.st_mode = stat.S_IFBLK | 0o666
            with self.assertRaises(helper.Refused):
                helper.trusted(Path('/dev/fixture'), block=True)

    def test_production_trust_checks_reject_writable_symlink_and_user_ownership(self):
        fake = mock.Mock(st_uid=0, st_mode=stat.S_IFDIR | 0o755, st_nlink=1)
        regular = mock.Mock(st_uid=0, st_mode=stat.S_IFREG | 0o644, st_nlink=1)
        def info(path):
            return regular if str(path) == '/trusted/helper' else fake
        with mock.patch.object(Path, 'lstat', autospec=True, side_effect=info):
            helper.trusted(Path('/trusted/helper'))
            regular.st_uid = 1000
            with self.assertRaises(helper.Refused):
                helper.trusted(Path('/trusted/helper'))
            regular.st_uid = 0
            regular.st_mode = stat.S_IFREG | 0o666
            with self.assertRaises(helper.Refused):
                helper.trusted(Path('/trusted/helper'))
            regular.st_mode = stat.S_IFLNK | 0o777
            with self.assertRaises(helper.Refused):
                helper.trusted(Path('/trusted/helper'))
            regular.st_mode = stat.S_IFREG | 0o644
            fake.st_mode = stat.S_IFDIR | 0o777
            with self.assertRaises(helper.Refused):
                helper.trusted(Path('/trusted/helper'))
