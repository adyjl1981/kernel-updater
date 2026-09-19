"""Conservative GRUB menu configuration and atomic, backed-up transactions.

Never source configuration while inspecting it. CLI writes only the fixed Ubuntu
paths; tests call apply_setting with temporary paths and an injected updater.
"""
import difflib
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid

CHOICES = {"hidden": ("hidden", "0"), "5": ("menu", "5"), "15": ("menu", "15")}
LABELS = {"hidden": "Hidden", "5": "Show for 5 seconds", "15": "Show for 15 seconds"}
LEGACY = {"GRUB_HIDDEN_TIMEOUT", "GRUB_HIDDEN_TIMEOUT_QUIET"}
KEYS = {"GRUB_TIMEOUT_STYLE", "GRUB_TIMEOUT"} | LEGACY
KEY_PATTERN = "(?:" + "|".join(sorted(KEYS)) + ")"
ASSIGNMENT = re.compile(r"^(?P<prefix>[ \t]*(?:export[ \t]+)?)(?P<key>" + KEY_PATTERN +
                        r")=(?P<value>'[^']*'|\"[^\"$`\\]*\"|[a-zA-Z0-9_-]*)(?P<tail>[ \t]+#.*|[ \t]*)(?P<end>\r?\n)?$")


def assignments(text):
    found = {}
    for index, line in enumerate(text.splitlines(keepends=True)):
        if line.lstrip().startswith('#'):
            continue
        if re.match(r'\s*(?:if|then|else|elif|fi|for|while|case|esac|source|eval|\.)\s', line) or line.rstrip().endswith('\\'):
            raise ValueError('Conditional, sourced or multiline GRUB configuration requires manual editing.')
        if not re.search(r"\b" + KEY_PATTERN + r"\b", line):
            continue
        match = ASSIGNMENT.fullmatch(line)
        if not match:
            raise ValueError(f"Unsupported or malformed GRUB menu setting on line {index + 1}; edit it manually first.")
        key = match['key']
        if key in found:
            raise ValueError(f"Duplicate {key}; resolve duplicate settings before applying.")
        value = match['value'].strip("\"'")
        if key in ('GRUB_TIMEOUT', 'GRUB_HIDDEN_TIMEOUT') and value and not re.fullmatch(r'-?\d+', value):
            raise ValueError(f'Malformed numeric {key}.')
        found[key] = (index, match, value)
    return found


def current_choice(text):
    found = assignments(text)
    values = {key: item[2] for key, item in found.items()}
    timeout = values.get('GRUB_TIMEOUT') or '5'
    style = values.get('GRUB_TIMEOUT_STYLE', '')
    if not style:
        if values.get('GRUB_HIDDEN_TIMEOUT', ''):
            style = 'hidden' if values.get('GRUB_HIDDEN_TIMEOUT_QUIET') == 'true' else 'countdown'
            timeout = values['GRUB_HIDDEN_TIMEOUT']
        else:
            style = 'menu'
    if style == 'hidden' and timeout.isdigit():
        return 'hidden'
    if style == 'menu' and timeout == '0':
        return 'hidden'
    return timeout if style == 'menu' and timeout in ('5', '15') else None


def menu_config(text, choice):
    if choice not in CHOICES:
        raise ValueError('Select a GRUB menu option first.')
    found = assignments(text)
    lines = text.splitlines(keepends=True)
    style, timeout = CHOICES[choice]
    for key, value in (("GRUB_TIMEOUT_STYLE", style), ("GRUB_TIMEOUT", timeout)):
        if key in found:
            index, match, _ = found[key]
            old = match['value']
            quote = old[0] if old.startswith(('"', "'")) else ''
            lines[index] = f"{match['prefix']}{key}={quote}{value}{quote}{match['tail']}{match['end'] or ''}"
    for key in LEGACY & found.keys():
        index = found[key][0]
        lines[index] = '# Disabled by Kernel Manager (legacy): ' + lines[index]
    for key, value in (("GRUB_TIMEOUT_STYLE", style), ("GRUB_TIMEOUT", timeout)):
        if key not in found:
            if lines and not lines[-1].endswith('\n'):
                lines[-1] += '\n'
            lines.append(f'{key}={value}\n')
    return ''.join(lines)


def preview(text, choice):
    updated = menu_config(text, choice)
    return ''.join(difflib.unified_diff(text.splitlines(True), updated.splitlines(True),
                                      fromfile='/etc/default/grub (current)',
                                      tofile='/etc/default/grub (proposed)')) or 'No file changes; regenerate GRUB only.'


def check_overrides(directory):
    """Refuse competing drop-in definitions instead of claiming a false state."""
    for path in sorted(Path(directory).glob('*.cfg')):
        for line in path.read_text().splitlines():
            if not line.lstrip().startswith('#') and re.search(r'\b' + KEY_PATTERN + r'\b', line):
                raise ValueError(f'{path} overrides GRUB menu settings; resolve this override first.')


def regular(path):
    if not stat.S_ISREG(path.lstat().st_mode):
        raise RuntimeError(f'{path} must be a regular file (not a symlink).')


def atomic_write(path, data, metadata):
    descriptor, name = tempfile.mkstemp(prefix='.' + path.name + '.kernel-manager-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as staged:
            staged.write(data)
            staged.flush()
            os.fsync(staged.fileno())
        shutil.copystat(metadata, name)
        info = metadata.stat()
        os.chown(name, info.st_uid, info.st_gid)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def backup_file(path, suffix):
    backup = path.with_name(path.name + suffix)
    descriptor = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, 'wb') as dest, path.open('rb') as source:
        shutil.copyfileobj(source, dest)
        dest.flush()
        os.fsync(dest.fileno())
    shutil.copystat(path, backup)
    info = path.stat()
    os.chown(backup, info.st_uid, info.st_gid)
    return backup


def apply_setting(defaults, generated, expected, choice, updater):
    """Caller holds transaction lock. Snapshot both files before any mutation."""
    regular(defaults)
    regular(generated)
    original = defaults.read_bytes()
    if original.decode('utf-8') != expected:
        raise RuntimeError('GRUB configuration changed since confirmation. Refresh and try again.')
    updated = menu_config(expected, choice).encode('utf-8')
    suffix = '.kernel-manager-backup-' + uuid.uuid4().hex
    backup = backup_file(defaults, suffix)
    generated_backup = backup_file(generated, suffix)
    attempted = False
    try:
        if defaults.read_bytes() != original:
            raise RuntimeError('GRUB configuration changed while backing up; refresh and retry.')
        attempted = True
        atomic_write(defaults, updated, backup)
        updater()
        if defaults.read_bytes() != updated:
            raise RuntimeError('GRUB configuration changed during update-grub.')
    except Exception as error:
        if not attempted:
            raise
        failures = []
        # If another writer changed the defaults, do not overwrite their work.
        if defaults.is_symlink() or (defaults.exists() and defaults.read_bytes() not in (original, updated)):
            failures.append('defaults changed externally; automatic restoration refused')
        else:
            for target, saved in ((defaults, backup), (generated, generated_backup)):
                try:
                    if target.is_symlink():
                        raise RuntimeError(f'{target} became a symlink')
                    atomic_write(target, saved.read_bytes(), saved)
                except Exception as rollback_error:
                    failures.append(str(rollback_error))
        detail = 'Previous defaults and generated menu restored.' if not failures else 'Rollback incomplete: ' + '; '.join(failures)
        raise RuntimeError(f'{error}. {detail} Backups: {backup}, {generated_backup}') from error
    return f'GRUB menu setting applied. Backups: {backup}, {generated_backup}'


def main():
    request = json.loads(Path(sys.argv[1]).read_text())
    defaults = Path('/etc/default/grub')
    generated = Path('/boot/grub/grub.cfg')
    # Stable separate lock: replacing the defaults inode must not release it.
    with open('/run/lock/kernel-manager-grub-menu.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        check_overrides('/etc/default/grub.d')
        command = shutil.which('update-grub')
        if not command:
            raise RuntimeError('update-grub is unavailable; no settings changed.')
        def update():
            subprocess.run([command], check=True)
        print(apply_setting(defaults, generated, request['original'], request['choice'], update))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
