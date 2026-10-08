"""Read-only CPU/RAM probes and conservative shared build parallelism policy."""
import argparse
import os
from pathlib import Path
import sys

GIB = 1024 ** 3


def choose_jobs(cpus, available_bytes, *, lto=False, debug=False):
    cpus = max(1, cpus or 1)
    if available_bytes is None:
        return 1, 'memory availability unknown; using 1 job'
    available_bytes = max(0, available_bytes)
    reserve = max(GIB, available_bytes // 10)
    per_job = GIB * (1 + int(debug) + 2 * int(lto))
    jobs = max(1, min(cpus, max(0, available_bytes - reserve) // per_job))
    return jobs, (f'{cpus} usable CPUs, {available_bytes / GIB:.1f} GiB available RAM; '
                  f'reserving {reserve / GIB:.1f} GiB, budgeting {per_job / GIB:.0f} GiB/job. '
                  'Estimated budget; swap excluded. Link steps may need more memory.')


def read_text(path):
    try:
        return path.read_text().strip()
    except (OSError, UnicodeError):
        return ''


def detect_resources(proc=Path('/proc'), cgroup=Path('/sys/fs/cgroup')):
    try:
        cpus = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        cpus = os.cpu_count() or 1
    cpus = max(1, cpus)
    memory = None
    for line in read_text(proc / 'meminfo').splitlines():
        if line.startswith('MemAvailable:'):
            try:
                memory = max(0, int(line.split()[1]) * 1024)
            except (ValueError, IndexError):
                pass
    # Standard unified mount, including the container namespace root.
    paths = [cgroup]
    for line in read_text(proc / 'self/cgroup').splitlines():
        if line.startswith('0::'):
            relative = Path(line[3:].lstrip('/'))
            if '..' not in relative.parts:
                current = cgroup / relative
                while current != cgroup:
                    paths.append(current)
                    current = current.parent
    for directory in paths:
        quota = read_text(directory / 'cpu.max').split()
        try:
            if len(quota) == 2 and quota[0] != 'max':
                count, period = map(int, quota)
                if count > 0 and period > 0:
                    cpus = min(cpus, max(1, count // period))
        except ValueError:
            pass
        try:
            limit = int(read_text(directory / 'memory.max'))
            used = int(read_text(directory / 'memory.current'))
            if limit >= 0 and used >= 0:
                headroom = max(0, limit - used)
                memory = headroom if memory is None else min(memory, headroom)
        except ValueError:
            pass
    return cpus, memory


def recommend_jobs(*, lto=False, debug=False):
    return choose_jobs(*detect_resources(), lto=lto, debug=debug)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--lto', action='store_true')
    parser.add_argument('--debug', action='store_true')
    args = parser.parse_args()
    jobs, reason = recommend_jobs(lto=args.lto, debug=args.debug)
    print(f'Automatic parallel jobs: {jobs}. {reason}', file=sys.stderr)
    print(jobs)


if __name__ == '__main__':
    main()
