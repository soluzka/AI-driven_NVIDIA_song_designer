"""Would mixing from the render instead of from the stems remove the ticks?

The current mix is rebuilt as bed + mapped vocal. Those two stems only stand in for
the render (their envelopes correlate r 0.59 with it), so where they don't, the sum
cancels and leaves 1.5 ms ticks up to 12.7 dB deep.

This builds the alternative - the render itself, plus only the difference the
mapping made to the vocal, minus the duck on the bed - and counts the ticks it
leaves against the same render. When the mapping is the identity the alternative is
the render exactly, so it cannot tick.

Usage: test_mix_delta.py <render> <bed> <vocal> <out> [--room 0.2] [--db 6]
"""

import ast
import os
import sys
from fractions import Fraction

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(os.path.dirname(HERE)), 'song_service', 'app.py')
WANTED = {'_map_vocal', '_apply_room', '_shift_pitch_keeping_formants'}
WANTED_CONSTS = {'LEAD_DB', 'DUCK_UNDER_VOICE', 'MAX_LEAD_GAIN'}


def load_functions():
    with open(APP, encoding='utf-8') as handle:
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
    exec(compile(ast.Module(body=nodes, type_ignores=[]), APP, 'exec'), namespace)
    return namespace


def read(path):
    data, rate = sf.read(path, always_2d=True, dtype='float32')
    return data, rate


def to_rate(data, rate, target):
    if rate == target:
        return data
    ratio = Fraction(int(target), int(rate)).limit_denominator(1000)
    out = resample_poly(data, ratio.numerator, ratio.denominator, axis=0)
    return out.astype('float32')


def envelope_db(mono, rate, hop_seconds):
    hop = max(1, int(hop_seconds * rate))
    frames = len(mono) // hop
    rms = np.sqrt(np.mean(mono[:frames * hop].reshape(frames, hop) ** 2, axis=1) + 1e-24)
    return 20 * np.log10(rms)


def main():
    argv = sys.argv[1:]
    room = 0.2
    threshold = 6.0
    for flag, name in (('--room', 'room'), ('--db', 'threshold')):
        if flag in argv:
            index = argv.index(flag)
            value = float(argv[index + 1])
            del argv[index:index + 2]
            if name == 'room':
                room = value
            else:
                threshold = value
    render_path, bed_path, vocal_path, out_path = argv

    functions = load_functions()
    render, render_rate = read(render_path)
    bed, bed_rate = read(bed_path)
    vocal, vocal_rate = read(vocal_path)
    rate = bed_rate                                   # the stems' rate, like the service
    render = to_rate(render, render_rate, rate)
    channels = render.shape[1]
    if vocal.shape[1] == 1 and channels > 1:
        vocal = np.repeat(vocal, channels, axis=1)

    mapping = (0.0, 0.0, room)
    placed = np.stack([functions['_map_vocal'](vocal[:, ch], rate, mapping)
                       for ch in range(vocal.shape[1])], axis=1)

    frames = min(len(render), len(bed), len(placed), len(vocal))
    render, bed, placed, vocal = (a[:frames] for a in (render, bed, placed, vocal))
    level = np.sqrt(np.mean(placed ** 2, axis=1))
    win = max(1, int(rate * 0.05))
    hit = np.convolve(level, np.ones(win) / win, mode='same')
    gate = np.clip(hit / (float(hit.max()) * 0.12 + 1e-9), 0.0, 1.0)
    active = gate > 0.5
    if bool(np.any(active)) and float(np.sqrt(np.mean(level[active] ** 2)) or 0.0) > 1e-6:
        bed_rms = float(np.sqrt(np.mean(bed[active] ** 2)) or 0.0)
        voice_rms = float(np.sqrt(np.mean(level[active] ** 2)) or 0.0)
    else:
        bed_rms = float(np.sqrt(np.mean(bed ** 2)) or 0.0)
        voice_rms = float(np.sqrt(np.mean(level ** 2)) or 0.0)
    lead_gain = 1.0
    if bed_rms > 1e-6 and voice_rms > 1e-6:
        lead_gain = min(functions['MAX_LEAD_GAIN'],
                        (bed_rms * 10 ** (functions['LEAD_DB'] / 20.0)) / voice_rms)

    # The render, plus only the wet difference the mapping added, scaled. Subtracting
    # the dry vocal as well cancels it wherever the mapping's early reflections are
    # out of phase, which is what put 54 ticks in the first version of this; adding
    # the difference at one gain cannot cancel, and is the render exactly when the
    # mapping is the identity.
    mixed = render + (placed - vocal) * lead_gain
    mixed *= (1.0 - functions['DUCK_UNDER_VOICE'] * gate)[:, None]
    peak = float(np.max(np.abs(mixed)) or 0.0)
    if peak > 0.98:
        mixed *= 0.98 / peak
    sf.write(out_path, mixed, rate)
    print('wrote %s at %d Hz, lead gain %.1f dB, peak %.2f'
          % (os.path.basename(out_path), rate, 20 * np.log10(lead_gain),
             20 * np.log10(float(np.max(np.abs(mixed))) + 1e-12)))

    hop = 0.0015
    before = envelope_db(render.mean(axis=1), rate, hop)
    after = envelope_db(mixed.mean(axis=1), rate, hop)
    n = min(len(before), len(after))
    difference = after[:n] - before[:n]
    count = int(np.sum(difference < -threshold))
    print('mix - render at %.1f ms: %d window(s) more than %.1f dB down'
          % (hop * 1000, count, threshold))
    if count:
        order = np.argsort(difference)[:6]
        print('   worst: %s' % ', '.join('%.2f s %.1f dB' % (i * hop, difference[i])
                                         for i in sorted(order)))


if __name__ == '__main__':
    main()
