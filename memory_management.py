"""Unprivileged Linux snapshots and client for a fixed installed memory helper."""
from dataclasses import dataclass, field
from pathlib import Path
import re
import shlex
import stat
import hashlib
import json
import subprocess
import math
import os

HELPER = Path("/usr/local/libexec/kernel-manager-memory.py")
PROTOCOL = 1
LIMITATION = ("Changes require confirmation and administrator authentication. "
              "Swap on/off affects the current session only; boot configuration stays unchanged. "
              "Other swap managers may override it. Restore uses the recorded previous swappiness, "
              "never an assumed factory default.")


def validate_swappiness(value):
    """Modern Linux range; a future helper must also verify kernel support."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,3}", value):
        raise ValueError("Swappiness must be an integer from 0 to 200.")
    number = int(value)
    if not 0 <= number <= 200:
        raise ValueError("Swappiness must be an integer from 0 to 200.")
    return number


def exact_size(value):
    if value is None:
        return "unknown"
    return f"{value:,} B ({value / 1048576:.2f} MiB)"


def unescape(value):
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), value)


@dataclass
class SwapDevice:
    path: str
    kind: str = "unknown"
    active: bool | None = False
    configured: list = field(default_factory=list)
    size: int | None = None
    used: int | None = None
    priority: int | None = None
    identity: list | None = None


@dataclass
class Snapshot:
    memory: dict = field(default_factory=dict)
    swappiness: int | None = None
    devices: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    resume: str = "unknown"
    pressure: dict = field(default_factory=dict)
    disk_free: dict = field(default_factory=dict)
    hibernation: str = "unknown"


def snapshot(root=Path("/"), extra_paths=()):
    """root permits fixture-only tests. Configured means discovered, not exhaustive."""
    root = Path(root)
    result = Snapshot()
    devices = {}

    def read(name, optional=False):
        try:
            return (root / name).read_text()
        except FileNotFoundError as error:
            if not optional:
                result.warnings.append(f"{name}: {error.strerror}")
        except (OSError, UnicodeError) as error:
            result.warnings.append(f"{name}: {error}")
        return ""

    def device(path):
        # Resolve local /dev aliases without following fixture paths onto the host.
        key = path
        alias = re.fullmatch(r"(UUID|LABEL)=([A-Za-z0-9_.-]+)", path)
        candidate = root / ("dev/disk/by-" + alias[1].lower() + "/" + alias[2]) if alias else root / path.lstrip("/")
        if path.startswith("/") or alias:
            try:
                resolved = candidate.resolve(strict=True)
                if root == Path("/"):
                    key = str(resolved)
                elif resolved.is_relative_to(root):
                    key = "/" + str(resolved.relative_to(root))
            except (OSError, RuntimeError):
                pass
        return devices.setdefault(key, SwapDevice(path=key))

    for line in read("proc/meminfo").splitlines():
        match = re.fullmatch(r"([A-Za-z_()]+):\s+(\d+) kB", line)
        if match:
            result.memory[match[1]] = int(match[2]) * 1024
    for name in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
        if name not in result.memory:
            result.warnings.append(f"{name} unavailable; no estimate substituted.")
    value = read("proc/sys/vm/swappiness").strip()
    try:
        result.swappiness = validate_swappiness(value)
    except ValueError as error:
        result.warnings.append(str(error))

    swaps = read("proc/swaps").splitlines()
    swaps_valid = bool(swaps) and swaps[0].split() == ["Filename", "Type", "Size", "Used", "Priority"]
    if not swaps_valid:
        result.warnings.append("Active swap inventory unavailable: missing or invalid proc/swaps header.")
    for line in swaps[1:] if swaps_valid else ():
        fields = line.split()
        try:
            if len(fields) != 5 or fields[1] not in ("file", "partition"):
                raise ValueError("unexpected columns")
            size, used, priority = map(int, fields[2:])
            if min(size, used) < 0 or used > size:
                raise ValueError("invalid size")
            item = device(unescape(fields[0]))
            item.active = True
            item.kind = "zram" if re.fullmatch(r"/dev/zram\d+", item.path) else fields[1]
            item.size, item.used, item.priority = size * 1024, used * 1024, priority
        except ValueError as error:
            swaps_valid = False
            result.warnings.append(f"Malformed proc/swaps row: {error}")

    for number, line in enumerate(read("etc/fstab").splitlines(), 1):
        try:
            fields = shlex.split(line, comments=True, posix=True)
            if len(fields) >= 3 and fields[2] == "swap":
                # shlex consumes backslashes: recover the original escaped source.
                source = unescape(line.strip().split()[0])
                item = device(source)
                item.configured.append(f"fstab line {number}" + (" (noauto)" if len(fields) > 3 and "noauto" in fields[3].split(",") else ""))
        except ValueError as error:
            result.warnings.append(f"fstab line {number}: {error}")

    for directory in ("etc/systemd/system", "run/systemd/generator", "run/systemd/generator.late", "usr/lib/systemd/system"):
        for unit in sorted((root / directory).glob("*.swap")):
            text = read(str(unit.relative_to(root)))
            section = ""
            for line in text.splitlines():
                line = line.strip()
                if line.startswith("["):
                    section = line
                elif section == "[Swap]" and line.startswith("What="):
                    source = line[5:].strip()
                    if source.startswith("/") and not any(c in source for c in "%$\\"):
                        device(source).configured.append(f"unit {unit.name} (enablement unknown)")
                    else:
                        result.warnings.append(f"{unit.name}: dynamic/escaped What= not resolved.")

    for block in sorted((root / "sys/block").glob("zram*")):
        if re.fullmatch(r"zram\d+", block.name):
            item = device("/dev/" + block.name)
            item.kind = "zram"
            if not item.active:
                try:
                    item.size = int(read(str((block / "disksize").relative_to(root))).strip())
                except ValueError:
                    result.warnings.append(f"{block.name}: disksize unavailable")

    for source in extra_paths:
        if isinstance(source, str) and source.startswith("/") and not any(ord(c) < 32 for c in source):
            device(source)

    for item in devices.values():
        if item.path.startswith("/"):
            try:
                info = (root / item.path.lstrip("/")).stat()
                item.identity = [info.st_dev, info.st_ino, info.st_rdev, info.st_size]
            except OSError:
                pass
        if not item.active and (not swaps_valid or not item.path.startswith("/")):
            item.active = None
        if item.kind == "unknown" and item.path.startswith("/"):
            try:
                mode = (root / item.path.lstrip("/")).stat().st_mode
                item.kind = "file" if stat.S_ISREG(mode) else "partition/block device" if stat.S_ISBLK(mode) else "unknown"
            except OSError:
                pass
    result.devices = sorted(devices.values(), key=lambda item: item.path)
    resume = read("sys/power/resume", optional=True).strip()
    offset = read("sys/power/resume_offset", optional=True).strip()
    cmdline = read("proc/cmdline", optional=True)
    hints = [token for token in cmdline.split() if token.startswith(("resume=", "resume_offset="))]
    resume_config = read('etc/initramfs-tools/conf.d/resume', optional=True)
    configured_resume = any(line.strip() and not line.lstrip().startswith('#')
                            and line.strip() not in ('RESUME=none', 'RESUME="none"', "RESUME='none'")
                            for line in resume_config.splitlines())
    image = read('sys/power/image_size', optional=True).strip()
    image_description = exact_size(int(image)) if image.isdigit() else 'unknown'
    result.resume = (f"resume device: {resume or 'unknown'}; offset: {offset or 'unknown'}; "
                     f"boot hints: {', '.join(hints) or 'none observed'}; "
                     f"initramfs resume hints: {'present' if configured_resume else 'none observed'}; "
                     f"image_size limit (not actual image size): {image_description}")
    power = read('sys/power/state', optional=True).split()
    result.hibernation = ('configured or hinted' if hints or configured_resume or resume not in ('', '0:0') or offset not in ('', '0')
                          else 'supported; configuration uncertain' if 'disk' in power
                          else 'not advertised' if power else 'unknown')
    crypttab = read('etc/crypttab', optional=True)
    if any(line.strip() and not line.lstrip().startswith('#') for line in crypttab.splitlines()):
        result.warnings.append('Encryption configuration entries observed. Their swap/resume coverage and backing storage are not established; key material was not read.')
    for line in read('proc/pressure/memory', optional=True).splitlines():
        fields = line.split()
        try:
            if len(fields) != 5 or fields[0] not in ('some', 'full'):
                raise ValueError()
            values = dict(field.split('=', 1) for field in fields[1:])
            if set(values) != {'avg10', 'avg60', 'avg300', 'total'}:
                raise ValueError()
            averages = [float(values[key]) for key in ('avg10', 'avg60', 'avg300')]
            if any(not math.isfinite(value) or not 0 <= value <= 100 for value in averages) or not values['total'].isdigit():
                raise ValueError()
            result.pressure[fields[0]] = averages[0]
        except ValueError:
            result.warnings.append('Malformed memory pressure sample; that row is unavailable.')
    for item in result.devices:
        if item.kind == 'file' and item.path.startswith('/'):
            try:
                parent = (root / item.path.lstrip('/')).parent.resolve(strict=True)
                if not parent.is_relative_to(root.resolve()):
                    raise ValueError('fixture path escaped root')
                space = os.statvfs(parent)
                result.disk_free[item.path] = space.f_bavail * space.f_frsize
            except (OSError, ValueError):
                result.disk_free[item.path] = None
    result.warnings.append("Configuration discovery covers fstab, top-level systemd swap units and sysfs zram. UUID/LABEL aliases, unit drop-ins, generators and other managers may be unresolved; absence does not mean unconfigured. Hibernation safety is not established by these hints.")
    return result


def check_settings(sample):
    """Explain one read-only sample; thresholds are triage signals, not targets."""
    m = sample.memory
    outcome, suggested = recommendation(sample)
    lines = [outcome, ('Conditional suggested total range: ' + exact_size(suggested[0]) + ' to ' + exact_size(suggested[1])) if suggested else 'No reliable size range from this instant.', 'Read-only assessment of one instant. No guaranteed optimum or RAM multiplier.',
             f"RAM total: {exact_size(m.get('MemTotal'))}; available: {exact_size(m.get('MemAvailable'))}.",
             f"Active swap total: {exact_size(m.get('SwapTotal'))}; free: {exact_size(m.get('SwapFree'))}."]
    total, available = m.get('MemTotal'), m.get('MemAvailable')
    swap, free = m.get('SwapTotal'), m.get('SwapFree')
    reliable_ram = total is not None and available is not None and total > 0 and 0 <= available <= total
    reliable_swap = swap is not None and free is not None and 0 <= free <= swap
    if not reliable_ram or not reliable_swap:
        lines.append('Incomplete or inconsistent counters: sizing advice is withheld. Refresh and inspect the missing sources.')
    else:
        low = available / total < .1
        tight = swap > 0 and free / swap < .2
        lines.append(f'Swap used: {exact_size(swap - free)}. Usage alone does not prove current pressure.')
        if low and (tight or swap == 0):
            lines.append('Available RAM is below 10% and swap is absent or over 80% used. If this persists during normal workloads, investigate heavy processes and consider more RAM or supported disk swap after checking storage. More swap may reduce OOM risk but can increase stalls.')
        elif low:
            lines.append('Available RAM is below 10%, but swap has headroom. Investigate sustained pressure and workload peaks before changing capacity.')
        else:
            lines.append('This sample shows RAM headroom. Keep the current capacity pending observations during peak workloads; idle headroom cannot establish future needs.')
    if sample.pressure:
        lines.append('Memory PSI over 10 seconds (% time stalled): ' + ', '.join(f'{key}={value:g}%' for key, value in sorted(sample.pressure.items())) + '.')
        if any(value > 1 for value in sample.pressure.values()):
            lines.append('PSI exceeds the 1% triage threshold. If sustained with slow response, investigate memory demand and I/O latency; adding swap alone may not improve performance.')
    else:
        lines.append('Memory PSI unavailable; absence of this signal does not establish absence of pressure.')
    for item in sample.devices:
        lines.append(f'{item.path}: {item.kind}; active={item.active}; capacity={exact_size(item.size)}; used={exact_size(item.used)}.')
        if item.kind == 'file':
            lines.append(f'Filesystem available space for {item.path}: {exact_size(sample.disk_free.get(item.path))}. Available space alone does not establish allocation eligibility.')
        elif item.kind == 'zram':
            lines.append('Zram capacity is compressed RAM-backed logical capacity, not equivalent to disk swap. Review its owning manager and compression statistics; it requires its own manager.')
    lines.extend(swappiness_advice(sample))
    lines.append(f'Hibernation: {sample.hibernation}. {sample.resume}. Aggregate swap capacity does not prove that the designated resume target can hold an image. Verify image size, target, encryption and resume offset with the system configuration before changing swap storage.')
    lines.extend(sample.warnings)
    return '\n\n'.join(lines)


def swappiness_advice(sample):
    """Explain direction, tradeoffs and a reversible trial without guessing I/O cost."""
    value = sample.swappiness
    lines = [f'Current swappiness: {value if value is not None else "unknown"} (range 0–200).',
             'What it does: when reclaiming RAM, Linux chooses between moving application memory to swap and reclaiming cached file data (which can be read again). Swappiness influences that balance; it is not a RAM usage percentage or a threshold at which swap turns on.',
             'Decrease it: favour keeping application memory in RAM and reclaim more file cache instead. This may help interactive apps if slow disk swap is causing pauses, but can mean more file reads and less useful cache. It does not bring already-swapped pages back immediately.',
             'Increase it: favour swapping application memory relative to reclaiming file cache. This can preserve useful file cache when swap is cheap, but slow swap can cause more I/O and pauses when an application needs its pages again. It does not immediately move a fixed amount of memory to swap.',
             'At 100, Linux treats swap and file paging as roughly equal I/O cost. Values above 100 can make sense for fast compressed swap such as zram, subject to CPU/compression costs. At 0, swapping is delayed until memory is very tight; 0 does not disable swap. Changing this value does not change swap capacity.']
    active = [item for item in sample.devices if item.active is True]
    if sample.memory.get('SwapTotal') == 0:
        lines.append('Advice for this sample: there is no active swap capacity. Changing swappiness will not create swap or solve a capacity shortage. Review whether supported swap is needed for your workload first.')
    elif not active or any(item.kind not in ('file', 'partition', 'partition/block device', 'zram') for item in active):
        lines.append('Advice for this sample: the active swap type is uncertain. Keep the current value until the inventory is resolved; a direction cannot be chosen from RAM size alone.')
    elif all(item.kind == 'zram' for item in active):
        lines.append('Advice for this sample: active swap is entirely zram. A higher value may be worth testing if workloads repeatedly run short of RAM or lose useful file cache. Check CPU load and compression effectiveness too; high memory PSI alone does not prove a higher value will help.')
    elif any(item.kind == 'zram' for item in active):
        lines.append('Advice for this sample: compressed and disk swap are both active. Swappiness is global, so raising it can eventually involve disk swap too. Check device priorities and per-device usage during peaks before choosing a direction.')
    else:
        lines.append('Advice for this sample: active swap uses files or partitions. Storage speed and swap I/O latency are unknown. If app-switching pauses coincide with disk swap activity, try a small decrease. If file-heavy work suffers from cache loss and swap is demonstrably faster, try a small increase. Without those symptoms, keep the current value.')
    if value is not None and sample.memory.get('SwapTotal', 0) > 0 and active:
        lower, higher = max(0, value - 10), min(200, value + 10)
        options = []
        if lower != value:
            options.append(f'{value} → {lower} to test a decrease')
        if higher != value:
            options.append(f'{value} → {higher} to test an increase')
        lines.append('How to test if the conditions above apply: ' + ' or '.join(options) +
                     '. These are small comparison steps, not recommended optimum values. Enter one value in New vm.swappiness, choose Temporary (until reboot), then Apply Swappiness and confirm. Run the same representative workload before and after; compare responsiveness, memory PSI and per-device swap usage. Stop if pauses or pressure worsen. Restore Previous Swappiness returns to the recorded value from this boot. Only choose Persistent after a repeatable improvement; changing multiple settings at once makes the result hard to interpret.')
    elif value is None:
        lines.append('Current swappiness is unknown; refresh before attempting a trial. No numeric target is inferred.')
    return lines


def helper_error():
    """Unprivileged readiness check; root helper enforces its own trust boundary."""
    try:
        for path in (HELPER, *HELPER.parents):
            info = path.lstat()
            if info.st_uid != 0 or info.st_mode & 0o022 or stat.S_ISLNK(info.st_mode):
                return f"Untrusted helper path: {path}. Administrator setup is required."
        if not HELPER.is_file():
            return "Installed memory helper is not a regular file."
        source = Path(__file__).with_name("memory_helper.py")
        if hashlib.sha256(HELPER.read_bytes()).digest() != hashlib.sha256(source.read_bytes()).digest():
            return "Installed memory helper differs from this version. Review and run administrator setup again."
        return None
    except OSError:
        return "Memory changes need one-time administrator setup. Use Helper Setup for instructions."


def request_change(request, env, log):
    """Only a fixed root-owned program is privileged; request travels via stdin."""
    error = helper_error()
    if error:
        raise RuntimeError(error)
    from memory_helper import validate_request
    validate_request(request)  # GUI copy validates UX; installed helper validates independently.
    command = ["/usr/bin/sudo", "-A", "--", "/usr/bin/python3", "-I", "-S", str(HELPER)]
    log("Requesting confirmed memory change through installed helper.\n")
    result = subprocess.run(command, input=json.dumps(request), env=env,
                            capture_output=True, text=True)
    if result.stdout:
        log(result.stdout)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"Memory helper failed ({result.returncode}).")
    return result.stdout.strip()


def toggle_error(item, enable):
    if item is None:
        return "Select one swap device."
    if item.active is None or item.identity is None:
        return "Selected device identity or active state is unknown. Refresh; unresolved aliases cannot be changed."
    if item.kind not in ("file", "partition", "partition/block device", "zram"):
        return "Unsupported swap device type."
    if item.active == enable:
        return "Selected swap is already enabled." if enable else "Selected swap is already disabled."
    return None


def recommendation(sample):
    m = sample.memory
    values = [m.get(key) for key in ('MemTotal', 'MemAvailable', 'SwapTotal', 'SwapFree')]
    if any(type(v) is not int or v < 0 for v in values):
        return 'Insufficient information to make a reliable recommendation.', None
    total, available, swap, free = values
    if total <= 0 or available > total or free > swap:
        return 'Insufficient information to make a reliable recommendation.', None
    if sample.hibernation == 'configured or hinted' or any(len(d.configured) > 1 and all('fstab' in c for c in d.configured) for d in sample.devices):
        return 'Configuration requires attention.', None
    pressure = any(v > 1 for v in sample.pressure.values())
    if available < total / 10 and (swap == 0 or free < swap / 5) and pressure:
        # A conditional incremental buffer, not a total-RAM multiplier.
        increment = max(512 * 1048576, (swap - free) // 2)
        low = ((swap + increment + 1048575) // 1048576) * 1048576
        return 'Consider increasing swap. If this pressure persists under representative workloads, a trial buffer may reduce OOM risk; inspect heavy apps, VMs and containers first.', (low, low + increment)
    return 'Swap configuration appears suitable. This instant cannot establish peak demand, justify reduction or prove swap unnecessary; observe representative applications, VMs and containers.', None
