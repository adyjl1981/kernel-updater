"""Small latency overlay, validated by the target kernel's native Kconfig."""
import argparse
import json
import re
from pathlib import Path
import subprocess

REQUIRED = dict(PREEMPT='y', HZ_1000='y', HZ='1000', HIGH_RES_TIMERS='y', NO_HZ_IDLE='y')
DISABLED = ('PREEMPT_NONE', 'PREEMPT_VOLUNTARY', 'PREEMPT_LAZY', 'PREEMPT_RT',
            'HZ_100', 'HZ_250', 'HZ_300', 'NO_HZ_FULL', 'HZ_PERIODIC')

def values(path):
    return dict(re.findall(r'^CONFIG_(\w+)=(.*)$', Path(path).read_text(), re.M))

def configure(source, make_args):
    source = Path(source).resolve()
    config = source / '.config'
    baseline = values(config)
    symbols = set()
    for path in source.rglob('Kconfig*'):
        if path.is_file():
            symbols.update(re.findall(r'^\s*(?:menuconfig|config)\s+(\w+)', path.read_text(errors='replace'), re.M))
    missing = set(REQUIRED) - symbols
    if missing:
        raise ValueError('Unsupported Low Latency options: ' + ', '.join(sorted(missing)))
    requested = dict(REQUIRED)
    requested.update({s: 'n' for s in DISABLED if s in symbols})
    if 'PREEMPT_DYNAMIC' in symbols:
        requested['PREEMPT_DYNAMIC'] = 'y'
    # NO_HZ_FULL previously exposed callback offload. Keep that baseline
    # capability through its supported alternative dependency.
    if baseline.get('RCU_NOCB_CPU') == 'y' and 'RCU_EXPERT' in symbols:
        requested['RCU_EXPERT'] = 'y'
    for symbol, value in requested.items():
        subprocess.run([str(source / 'scripts/config'), '--file', str(config), '--set-val', symbol, value], check=True, cwd=source)
    subprocess.run(['make', *make_args, 'olddefconfig'], cwd=source, stdin=subprocess.DEVNULL, check=True)
    effective = values(config)
    errors = [s for s, v in REQUIRED.items() if effective.get(s) != v]
    errors += [s for s in DISABLED if effective.get(s, 'n') != 'n']
    allowed = set(requested) | {'PREEMPT_NONE_BUILD', 'PREEMPT_VOLUNTARY_BUILD'}
    lost = [s for s, v in baseline.items() if v in ('y', 'm') and s not in allowed
            and effective.get(s, 'n') not in (('y',) if v == 'y' else ('y', 'm'))]
    report = {'requested': requested, 'effective': {s: effective.get(s, 'n') for s in requested},
              'lost_baseline_features': lost, 'errors': errors,
              'dynamic_fallback': effective.get('PREEMPT_DYNAMIC') != 'y'}
    (source / '.kernel-manager-low-latency.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    if errors or lost:
        raise ValueError('Low Latency validation failed: ' + ', '.join(errors + lost))
    return report

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source')
    parser.add_argument('make_args', nargs='*')
    args = parser.parse_args()
    configure(args.source, args.make_args)
