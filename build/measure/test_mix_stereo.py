"""Run the service's own mix on the stored stems, and count the dips it leaves.

`_mix_separated` is lifted out of `song_service/app.py` and executed from its own
source, so the shipped code is what is measured (importing the module starts the
lyric warm-up and loads the model). The result is compared with the render the mix
came from: a dip is the finished mix falling below that render, which is what the
mix must not do.

Usage: test_mix_stereo.py <render.wav> <bed.wav> <vocal.wav> <out.wav> [--db 6]
"""

import ast
import os
import sys

import numpy as np
import soundfile as sf

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(os.path.dirname(HERE)), 'song_service', 'app.py')

WANTED = {'_mix_separated', '_map_vocal', '_apply_room',
          '_shift_pitch_keeping_formants'}
WANTED_CONSTS = {'LEAD_DB', 'DUCK_UNDER_VOICE', 'MAX_LEAD_GAIN', 'ROOM_SEND'}


def load(app=APP):
    with open(app, encoding='utf-8') as handle:
        tree = ast.parse(handle.read())
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in WANTED:
            nodes.append(node)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in WANTED_CONSTS:
                    nodes.append(node)
    namespace = {'np': np}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), app, 'exec'), namespace)
    return namespace


def envelope(path, hop_seconds=0.0015):
    data, rate = sf.read(path, always_2d=True, dtype='float32')
    mono = data.mean(axis=1)
    hop = max(1, int(hop_seconds * rate))
    frames = len(mono) // hop
    rms = np.sqrt(np.mean(mono[:frames * hop].reshape(frames, hop) ** 2, axis=1) + 1e-24)
    return 20 * np.log10(rms), rate


def main():
    argv = sys.argv[1:]
    threshold = 6.0
    if '--db' in argv:
        index = argv.index('--db')
        threshold = float(argv[index + 1])
        del argv[index:index + 2]
    if len(argv) != 4:
        raise SystemExit('usage: test_mix_stereo.py <render> <bed> <vocal> <out> [--db 6]')
    render, bed, vocal, out = argv

    namespace = load()
    print('constants: lead %+g dB, duck %.0f%%, max lead gain %g'
          % (namespace['LEAD_DB'], 100 * namespace['DUCK_UNDER_VOICE'],
             namespace['MAX_LEAD_GAIN']))
    print('mixing %s + %s -> %s' % (os.path.basename(bed), os.path.basename(vocal),
                                    os.path.basename(out)))
    # 0.26 is what the page sends for the lo-fi profile.
    layout = namespace['_mix_separated'](bed, vocal, out, (0.0, 0.0, 0.26),
                                        render_path=render)
    print('layout: %s' % (layout,))

    before, rate = envelope(render)
    after, _ = envelope(out)
    n = min(len(before), len(after))
    difference = after[:n] - before[:n]
    hop_seconds = 0.0015

    mask = difference < -threshold
    runs, start = [], None
    for i, flag in enumerate(mask):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            runs.append((start, i))
            start = None
    if start is not None:
        runs.append((start, len(mask)))

    print()
    print('mix - render: %d window(s) of %.1f ms more than %.1f dB down'
          % (len(runs), hop_seconds * 1000, threshold))
    if runs:
        deepest = sorted(runs, key=lambda r: difference[r[0]:r[1]].min())[:6]
        for a, b in deepest:
            print('   %7.3f s  %5.1f ms  %6.1f dB'
                  % (a * hop_seconds, (b - a) * hop_seconds * 1000,
                     float(np.min(difference[a:b]))))
    print('whole mix: %.1f dB median level; the render %.1f dB'
          % (np.median(after), np.median(before)))


if __name__ == '__main__':
    main()
