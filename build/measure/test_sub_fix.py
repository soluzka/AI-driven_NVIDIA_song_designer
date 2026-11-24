"""Measure the service's own `_fix_low_end` on a real render.

The helper is lifted out of `song_service/app.py` by name and executed from its own
source, so what is measured is the shipped code. It is not imported: importing the
module starts the lyric warm-up and loads the 3.5B model.

Usage: test_sub_fix.py <wav> [...]
"""

import ast
import os
import sys

import numpy as np
import soundfile as sf

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(os.path.dirname(HERE)), 'song_service', 'app.py')

WANTED_FUNCS = {'_fix_low_end', '_sub_share_db'}
WANTED_CONSTS = {'SUB_HP_HZ', 'SUB_SHARE_MAX_DB', 'SUB_SHELF_HZ', 'SUB_TRIM_LIMIT_DB'}


def load_helper():
    with open(APP, encoding='utf-8') as handle:
        tree = ast.parse(handle.read())
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in WANTED_FUNCS:
            nodes.append(node)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in WANTED_CONSTS:
                    nodes.append(node)
    missing = WANTED_FUNCS | WANTED_CONSTS
    found = set()
    for node in nodes:
        found.add(node.name if isinstance(node, ast.FunctionDef) else
                  node.targets[0].id)
    if missing - found:
        raise SystemExit('not found in app.py: %s' % ', '.join(sorted(missing - found)))
    namespace = {'np': np}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), APP, 'exec'), namespace)
    return namespace


BANDS = [(20, 120), (60, 120), (80, 120), (120, 250), (250, 2000), (20, 32), (32, 45)]


def shares(mono, rate):
    from scipy.signal import welch

    freqs, power = welch(mono, fs=rate, nperseg=65536, noverlap=32768, window='hann',
                         detrend=False, scaling='density')
    total = power[(freqs >= 20) & (freqs <= 20000)].sum()
    return [10 * np.log10(power[(freqs >= lo) & (freqs <= hi)].sum() / total + 1e-30)
            for lo, hi in BANDS]


def main():
    paths = sys.argv[1:]
    if not paths:
        raise SystemExit('usage: test_sub_fix.py <wav> [...]')
    namespace = load_helper()
    print('constants: HP %g Hz (4 poles), share cap %+.1f dB, shelf %g Hz, limit %g dB'
          % (namespace['SUB_HP_HZ'], namespace['SUB_SHARE_MAX_DB'],
             namespace['SUB_SHELF_HZ'], namespace['SUB_TRIM_LIMIT_DB']))
    header = '%-40s %9s %9s %9s %9s %9s %9s %9s' % (
        '', '20-120', '60-120', '80-120', '120-250', '250-2k', '20-32', '32-45')
    for path in paths:
        data, rate = sf.read(path, always_2d=True, dtype='float32')
        before = shares(data.mean(axis=1), rate)
        frames = namespace['_fix_low_end'](data.copy(), rate)
        after = shares(frames.mean(axis=1), rate)
        print()
        print(os.path.basename(path))
        print(header)
        order = list(range(len(BANDS)))
        print('%-40s %9.1f %9.1f %9.1f %9.1f %9.1f %9.1f %9.1f' % (
            'before', *[before[i] for i in order]))
        print('%-40s %9.1f %9.1f %9.1f %9.1f %9.1f %9.1f %9.1f' % (
            'after', *[after[i] for i in order]))
        print('%-40s %9.1f %9.1f %9.1f %9.1f %9.1f %9.1f %9.1f' % (
            'change', *[after[i] - before[i] for i in order]))
        print('sub band now holds %.0f%% of the song\'s power (was %.0f%%)'
              % (100 * 10 ** (after[0] / 10), 100 * 10 ** (before[0] / 10)))


if __name__ == '__main__':
    main()
