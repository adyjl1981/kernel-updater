#!/usr/bin/python3
"""Standalone root-owned helper. Install explicitly; launch with python3 -I.

No repository imports, user-selected commands, filenames for writes, or bulk swap.
The entry point accepts one small JSON request on stdin after authentication.
Tests inject a fixture root and a fake runner; the production entry point cannot.
"""
import contextlib
import fcntl
import fnmatch
import grp
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

PROTOCOL = 1
HELPER = Path('/usr/local/libexec/kernel-manager-memory.py')
CONFIG = 'etc/sysctl.d/99-kernel-manager-memory.conf'
STATE = 'var/lib/kernel-manager-memory'
MANAGED = '# Managed by Kernel Manager Memory\n'
TOOLS = {'swapon': '/usr/sbin/swapon', 'swapoff': '/usr/sbin/swapoff'}
SAFE_ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'}


class Refused(RuntimeError):
    pass


def integer(value):
    if type(value) is not int or not 0 <= value <= 200:
        raise Refused('Swappiness must be an integer from 0 to 200.')
    return value


def validate_request(request):
    if type(request) is not dict or type(request.get('protocol')) is not int or request.get('protocol') != PROTOCOL:
        raise Refused('Unsupported memory request protocol; update the installed helper.')
    action = request.get('action')
    schemas = {
        'swappiness': {'protocol', 'action', 'value', 'persistent', 'expected'},
        'restore_swappiness': {'protocol', 'action', 'expected'},
        'swap': {'protocol', 'action', 'path', 'enable', 'expected_active', 'identity'},
    }
    if type(action) is not str or action not in schemas or set(request) != schemas[action]:
        raise Refused('Unknown action or unexpected request fields.')
    if action in ('swappiness', 'restore_swappiness'):
        integer(request['expected'])
    if action == 'swappiness':
        integer(request['value'])
        if type(request['persistent']) is not bool:
            raise Refused('Choose temporary or persistent explicitly.')
    if action == 'swap':
        path = request['path']
        if (type(path) is not str or not path.startswith('/') or len(path) > 1024
                or any(ord(c) < 32 for c in path) or '..' in Path(path).parts
                or str(Path(path)) != path):
            raise Refused('Swap target must be a canonical absolute path.')
        if (type(request['enable']) is not bool or type(request['expected_active']) is not bool):
            raise Refused('Invalid swap state.')
        identity = request['identity']
        if type(identity) is not list or len(identity) != 4 or any(type(n) is not int or n < 0 for n in identity):
            raise Refused('Missing or invalid device identity. Refresh the snapshot.')
    return request


def trusted(path, directory=False, block=False):
    """No symlinks, user ownership, or writable directory ancestry."""
    path = Path(path)
    for parent in reversed(path.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise Refused(f'Untrusted directory: {parent}')
    info = path.lstat()
    kind_ok = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISBLK(info.st_mode) if block else stat.S_ISREG(info.st_mode)
    # Conventional /dev block devices are root:disk 0660; the privileged disk
    # group already has raw-device access. Directory ancestry still forbids writes.
    forbidden = 0o002 if block else 0o022
    if block and info.st_mode & stat.S_IWGRP:
        try:
            disk_gid = grp.getgrnam('disk').gr_gid
        except KeyError:
            disk_gid = 0
        if info.st_gid not in (0, disk_gid):
            raise Refused(f'Block device is writable by an untrusted group: {path}')
    if not kind_ok or info.st_uid != 0 or info.st_mode & forbidden:
        raise Refused(f'Untrusted ownership, permissions, type or symlink: {path}')
    if not directory and not block and info.st_nlink != 1:
        raise Refused(f'Hard-linked file is unsupported: {path}')
    return info


def identity(info):
    return [info.st_dev, info.st_ino, info.st_rdev, info.st_size]


def unescape(value):
    return re.sub(r'\\([0-7]{3})', lambda match: chr(int(match[1], 8)), value)


class Operations:
    def __init__(self, root=Path('/'), runner=None, checker=trusted):
        self.root = Path(root)
        self.runner = runner or self.run
        self.checker = checker

    def path(self, name):
        return self.root / name.lstrip('/')

    def read(self, name, optional=False):
        p = self.path(name)
        try:
            return p.read_text()
        except FileNotFoundError:
            if optional:
                return ''
            raise Refused(f'Required safety information unavailable: /{name}')
        except (OSError, UnicodeError) as error:
            raise Refused(f'Cannot read /{name}: {error}') from error

    def config_read(self, name, optional=False):
        p = self.path(name)
        if optional and not p.exists() and not p.is_symlink():
            return None
        self.checker(p)
        return self.read(name)

    @staticmethod
    def run(command):
        executable = Path(command[0]).resolve(strict=True)
        trusted(executable)
        result = subprocess.run([str(executable), *command[1:]], env=SAFE_ENV,
                                stdin=subprocess.DEVNULL, capture_output=True, text=True)
        if result.returncode:
            raise Refused(f'{Path(command[0]).name} failed ({result.returncode}): '
                          f'{result.stderr.strip() or result.stdout.strip() or "no diagnostic"}')

    def current(self):
        value = self.read('proc/sys/vm/swappiness').strip()
        if not re.fullmatch(r'[0-9]{1,3}', value):
            raise Refused('Current swappiness is unreadable.')
        return integer(int(value))

    def set_runtime(self, value):
        # One fixed kernel knob; never accepts an arbitrary procfs pathname.
        p = self.path('proc/sys/vm/swappiness')
        with p.open('w') as stream:
            stream.write(str(integer(value)) + '\n')
        if self.current() != value:
            raise Refused('Kernel did not retain the requested swappiness; refresh before retrying.')

    def atomic(self, name, data, mode=0o600):
        target = self.path(name)
        self.checker(target.parent, directory=True)
        if target.exists() or target.is_symlink():
            self.checker(target)
        fd, temp = tempfile.mkstemp(prefix='.memory-', dir=target.parent)
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, target)
            self.sync_directory(target.parent)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    @staticmethod
    def sync_directory(path):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def remove(self, name):
        p = self.path(name)
        if p.exists() or p.is_symlink():
            self.checker(p)
            p.unlink()
            self.sync_directory(p.parent)

    def save(self, name, record):
        self.atomic(f'{STATE}/{name}', json.dumps(record, sort_keys=True).encode())

    def load(self, name):
        text = self.config_read(f'{STATE}/{name}', optional=True)
        if text is None:
            return None
        try:
            return json.loads(text)
        except ValueError as error:
            raise Refused(f'Invalid recovery record: {name}; inspect as administrator.') from error

    def conflicts(self):
        # Fail closed on other definitions, including the procps /etc/sysctl.conf
        # compatibility file. Do not edit other managers' settings or run --system.
        candidates = [self.path('etc/sysctl.conf')]
        for folder in ('etc/sysctl.d', 'run/sysctl.d', 'usr/local/lib/sysctl.d', 'usr/lib/sysctl.d', 'lib/sysctl.d'):
            candidates.extend(sorted(self.path(folder).glob('*.conf')))
        for p in candidates:
            if p == self.path(CONFIG) or not p.exists():
                continue
            # /etc/sysctl.d/99-sysctl.conf is commonly a root-managed symlink.
            resolved = p.resolve(strict=True)
            self.checker(resolved)
            for line in resolved.read_text().splitlines():
                content = line.split('#', 1)[0].split(';', 1)[0].strip()
                assignment = re.split(r'\s*=\s*|\s+', content, maxsplit=1)
                key = assignment[0].lstrip('-').replace('/', '.')
                if len(assignment) == 2 and fnmatch.fnmatchcase('vm.swappiness', key):
                    raise Refused(f'Persistent swappiness conflicts with {p}. '
                                  'Use Temporary or resolve that definition as administrator; it was not edited.')

    def apply_swappiness(self, request):
        old_value = self.current()
        if old_value != request['expected']:
            raise Refused('Swappiness changed since the snapshot. Refresh and confirm again.')
        old_record = self.load('swappiness.json')
        if old_record and old_record.get('phase') == 'prepared':
            raise Refused('An interrupted change has a recovery record. Restore previous swappiness first.')
        persistent = request['persistent']
        old_text = self.config_read(CONFIG, optional=True) if persistent else None
        old_mode = stat.S_IMODE(self.path(CONFIG).stat().st_mode) if old_text is not None else 0o644
        if old_text is not None:
            definition = re.fullmatch(re.escape(MANAGED) + r'vm.swappiness=([0-9]{1,3})\n', old_text)
            if definition is None:
                raise Refused('The dedicated sysctl file contains unmanaged settings; it was not edited.')
            integer(int(definition[1]))
        new_text = MANAGED + f"vm.swappiness={request['value']}\n" if persistent else old_text
        if persistent:
            self.conflicts()
        record = {'phase': 'prepared', 'old_value': old_value, 'new_value': request['value'],
                  'persistent': persistent, 'old_text': old_text, 'new_text': new_text, 'old_mode': old_mode,
                  'boot_id': self.read('proc/sys/kernel/random/boot_id').strip()}
        self.save('swappiness.json', record)  # durable recovery before either write
        try:
            if persistent:
                self.atomic(CONFIG, new_text.encode(), old_mode)
            self.set_runtime(request['value'])
            record['phase'] = 'applied'
            self.save('swappiness.json', record)
        except Exception as error:
            recovery_errors = []
            for action in (lambda: self.restore_file(record), lambda: self.rollback_runtime(record)):
                try:
                    action()
                except Exception as recovery_error:
                    recovery_errors.append(str(recovery_error))
            if not recovery_errors:
                if old_record is None:
                    self.remove(f'{STATE}/swappiness.json')
                else:
                    self.save('swappiness.json', old_record)
            raise Refused(f'Change failed: {error}. ' + ('Previous setting restored.' if not recovery_errors else
                          'Recovery incomplete; use Restore Previous Swappiness. ' + '; '.join(recovery_errors))) from error
        return f"Swappiness is {request['value']}. " + (
            f'Persistent setting saved in /{CONFIG}.' if persistent else 'Runtime only; boot configuration unchanged.')

    def restore_file(self, record):
        if not record['persistent']:
            return
        current_text = self.config_read(CONFIG, optional=True)
        if current_text not in (record['new_text'], record['old_text']):
            raise Refused('Persistent configuration changed externally; recovery will not overwrite it.')
        if record['old_text'] is None:
            self.remove(CONFIG)
        else:
            self.atomic(CONFIG, record['old_text'].encode(), record['old_mode'])

    def rollback_runtime(self, record):
        if self.current() not in (record['new_value'], record['old_value']):
            raise Refused('Runtime swappiness changed externally; recovery will not overwrite it.')
        self.set_runtime(record['old_value'])

    def restore_swappiness(self, request):
        record = self.load('swappiness.json')
        if not record:
            raise Refused('No recorded previous swappiness setting is available. No factory default is assumed.')
        if record['boot_id'] != self.read('proc/sys/kernel/random/boot_id').strip():
            raise Refused('This backup belongs to an earlier boot; runtime recovery is not inferred. '
                          'Review the root-owned record as administrator.')
        current = self.current()
        if current != request['expected'] or current not in (record['new_value'], record['old_value']):
            raise Refused('Swappiness was changed externally. Refresh; automatic restore would overwrite it.')
        if record['persistent']:
            text = self.config_read(CONFIG, optional=True)
            if text not in (record['new_text'], record['old_text']):
                raise Refused('Persistent configuration changed externally; automatic restore refused.')
        # Keep the record if either step fails; the recovery action is retryable.
        self.restore_file(record)
        self.set_runtime(record['old_value'])
        self.remove(f'{STATE}/swappiness.json')
        return f"Restored recorded previous swappiness: {record['old_value']}."

    def active_swaps(self):
        lines = self.read('proc/swaps').splitlines()
        if not lines or lines[0].split() != ['Filename', 'Type', 'Size', 'Used', 'Priority']:
            raise Refused('Active swap inventory unavailable.')
        swaps = {}
        for line in lines[1:]:
            fields = line.split()
            if len(fields) != 5 or fields[1] not in ('file', 'partition'):
                raise Refused('Malformed active swap inventory.')
            try:
                size, used, priority = map(int, fields[2:])
            except ValueError as error:
                raise Refused('Malformed active swap counters.') from error
            if size < 0 or used < 0 or used > size:
                raise Refused('Invalid active swap counters.')
            source = unescape(fields[0])
            if not source.startswith('/') or source.endswith(' (deleted)'):
                raise Refused('Unsupported active swap source.')
            key = self.path(source).resolve(strict=True)
            swaps[key] = {'used': used * 1024, 'size': size * 1024, 'priority': priority}
        return swaps

    def allowlisted(self, target):
        entries = self.config_read('etc/fstab', optional=True) or ''
        for line in entries.splitlines():
            fields = line.split('#', 1)[0].split()
            if len(fields) < 4 or fields[2] != 'swap':
                continue
            source = unescape(fields[0])
            if source.startswith(('UUID=', 'LABEL=')):
                prefix, value = source.split('=', 1)
                if not re.fullmatch(r'[A-Za-z0-9_.-]+', value):
                    continue
                source = '/dev/disk/by-' + prefix.lower() + '/' + value
            if not source.startswith('/'):
                continue
            if self.path(source).resolve() == target:
                # Formatting, discard and fstab options are never executed here.
                priorities = [option[4:] for option in fields[3].split(',') if option.startswith('pri=')]
                if priorities:
                    if len(priorities) != 1 or not priorities[0].isdigit() or not 0 <= int(priorities[0]) <= 32767:
                        raise Refused('Unsupported fstab swap priority.')
                    return int(priorities[0])
                return -1
        return None

    def safe_swapoff(self, used):
        mem = {}
        for line in self.read('proc/meminfo').splitlines():
            match = re.fullmatch(r'(MemTotal|MemAvailable):\s+(\d+) kB', line)
            if match:
                mem[match[1]] = int(match[2]) * 1024
        if set(mem) != {'MemTotal', 'MemAvailable'} or not 0 <= mem['MemAvailable'] <= mem['MemTotal'] or mem['MemTotal'] <= 0:
            raise Refused('Reliable memory headroom unavailable; swapoff refused.')
        needed = 2 * used + max(512 * 1048576, mem['MemTotal'] // 4)
        if mem['MemAvailable'] < needed:
            raise Refused(f'Insufficient RAM for safe swapoff: {mem["MemAvailable"]:,} B available, '
                          f'{needed:,} B required (twice swap used plus RAM reserve). Close applications and refresh.')
        resume = self.read('sys/power/resume', optional=True).strip()
        if not resume:
            if 'disk' in self.read('sys/power/state').split():
                raise Refused('Hibernation resume state unavailable; swapoff refused.')
        elif resume != '0:0':
            raise Refused('A hibernation resume device is configured; swapoff refused.')
        if any(token.startswith(('resume=', 'resume_offset=')) for token in self.read('proc/cmdline').split()):
            raise Refused('Hibernation boot parameters found; swapoff refused.')
        resume_config = self.config_read('etc/initramfs-tools/conf.d/resume', optional=True)
        if resume_config and any(line.strip() and not line.lstrip().startswith('#')
                                 and line.strip() not in ('RESUME=none', 'RESUME="none"', "RESUME='none'")
                                 for line in resume_config.splitlines()):
            raise Refused('Initramfs hibernation configuration found; swapoff refused.')

    def toggle(self, request):
        p = self.path(request['path'])
        if p.resolve(strict=True) != p:
            raise Refused('Target is an alias; refresh to use its canonical path.')
        info = p.lstat()
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISBLK(info.st_mode)):
            raise Refused('Swap target is neither a regular file nor a block device.')
        self.checker(p, block=stat.S_ISBLK(info.st_mode))
        if identity(info) != request['identity']:
            raise Refused('Swap target changed since discovery. Refresh and confirm again.')
        active = self.active_swaps()
        if (p in active) != request['expected_active']:
            raise Refused('Swap state changed since the snapshot. Refresh and confirm again.')
        enable = request['enable']
        if enable == (p in active):
            raise Refused('Selected swap is already in the requested state.')
        disabled = self.load('disabled.json') or {}
        if enable:
            priority = None
            if request['path'] in disabled:
                previous = disabled[request['path']]
                if previous['identity'] != request['identity']:
                    raise Refused('Previously disabled device identity changed.')
                priority = previous['priority']
            if priority is None:
                priority = self.allowlisted(p)
            if priority is None and re.fullmatch(r'/dev/zram\d+', request['path']):
                size = self.read(f'sys/block/{p.name}/disksize').strip()
                if not size.isdigit() or int(size) <= 0:
                    raise Refused('Zram is not initialized; no formatting or allocation is performed.')
                priority = -1
            if priority is None:
                raise Refused('Enable supports fstab swap, initialized zram, or a device disabled by this helper. '
                              'Other managers must enable their own devices.')
            command = [TOOLS['swapon']]
            if priority >= 0:
                command += ['--priority', str(priority)]
        else:
            # Fresh counters and hibernation checks in the privileged process.
            self.safe_swapoff(active[p]['used'])
            disabled[request['path']] = {'identity': request['identity'], 'priority': active[p]['priority']}
            self.save('disabled.json', disabled)  # allow re-enable even without fstab
            command = [TOOLS['swapoff']]
        if identity(p.lstat()) != request['identity']:
            raise Refused('Target identity changed immediately before the operation.')
        self.runner([*command, '--', str(p)])
        if (p in self.active_swaps()) != enable:
            raise Refused('The requested swap state did not remain active; another manager may be controlling it. Refresh.')
        return f"{'Enabled' if enable else 'Disabled'} {request['path']}. Runtime only; boot configuration unchanged."


    @contextlib.contextmanager
    def locked(self):
        # Installer creates the directory; never follows or creates a user path.
        folder = self.path(STATE)
        self.checker(folder, directory=True)
        lock = folder / 'operation.lock'
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            self.checker(lock)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise Refused('Another memory change is running; wait and refresh.') from error
            yield
        finally:
            os.close(fd)

    def execute(self, request):
        request = validate_request(request)
        with self.locked():
            if request['action'] == 'swappiness':
                return self.apply_swappiness(request)
            if request['action'] == 'restore_swappiness':
                return self.restore_swappiness(request)
            return self.toggle(request)


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Refused('Duplicate JSON fields are forbidden.')
        result[key] = value
    return result


def main():
    try:
        if os.geteuid() != 0 or not sys.flags.isolated or not sys.flags.no_site or Path(__file__) != HELPER or len(sys.argv) != 1:
            raise Refused('Use the installed root-owned helper with /usr/bin/python3 -I -S and no arguments.')
        trusted(HELPER)
        os.chdir('/')
        os.environ.clear()
        os.environ.update(SAFE_ENV)
        os.umask(0o077)
        raw = sys.stdin.buffer.read(4097)
        if len(raw) > 4096:
            raise Refused('Request too large.')
        request = json.loads(raw, object_pairs_hook=unique_pairs)
        print(Operations().execute(request))
        return 0
    except Exception as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
