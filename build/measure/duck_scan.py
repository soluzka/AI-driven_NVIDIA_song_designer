"""Where does the mix lose level the render still has?

The mix is the demucs bed plus the mapped vocal, with the bed ducked under the
voice, and this compares it with the render it came from. The rates matter: demucs
writes its stems at 44.1 kHz while the render is 48 kHz, and comparing the two
sample-for-sample invents deep nulls that do not exist. Everything is resampled to
the mix's rate first.

  mix - render   negative = the mix is quieter than what the model produced

Usage: duck_scan.py <mix.wav> <render.wav> <bed.wav> <vocal.wav> [--db 6] [--hop 0.0015]
"""

import sys
from fractions import Fraction

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly


def load(path):
    data, rate = sf.read(path, always_2d=True, dtype='float32')
    return data.mean(axis=1), rate


def resample_to(mono, rate, target):
    if rate == target:
        return mono
    ratio = Fraction(int(target), int(rate)).limit_denominator(1000)
    return resample_poly(mono, ratio.numerator, ratio.denominator).astype('float32')


def envelope(mono, rate, hop_seconds):
    hop = max(1, int(hop_seconds * rate))
    frames = len(mono) // hop
    rms = np.sqrt(np.mean(mono[:frames * hop].reshape(frames, hop) ** 2, axis=1) + 1e-24)
    return 20 * np.log10(rms)


def runs_below(values, threshold, hop, duration_limit=0.5):
    mask = values < threshold
    runs, start = [], None
    for i, flag in enumerate(mask):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            runs.append((start, i))
            start = None
    if start is not None:
        runs.append((start, len(mask)))
    return [(a, b) for a, b in runs if (b - a) * hop < duration_limit]


def report(label, difference, hop, threshold):
    dips = runs_below(difference, -threshold, hop)
    print(label)
    if not dips:
        print('   no dip deeper than %.1f dB' % threshold)
        return
    depths = [float(np.min(difference[a:b])) for a, b in dips]
    order = np.argsort(depths)
    print('   %d dip(s) deeper than %.1f dB   deepest %.1f dB for %.1f ms'
          % (len(dips), threshold, depths[order[0]], (dips[order[0]][1] - dips[order[0]][0]) * hop * 1000))
    for k in order[:6]:
        a, b = dips[k]
        print('      %7.3f s  %5.1f ms  %6.1f dB' % (a * hop, (b - a) * hop * 1000, depths[k]))


def main():
    argv = sys.argv[1:]
    threshold = 6.0
    if '--db' in argv:
        index = argv.index('--db')
        threshold = float(argv[index + 1])
        del argv[index:index + 2]
    hop = 0.0015
    if '--hop' in argv:
        index = argv.index('--hop')
        hop = float(argv[index + 1])
        del argv[index:index + 2]
    if len(argv) != 4:
        raise SystemExit('usage: duck_scan.py <mix> <render> <bed> <vocal> [--db 6] [--hop 0.0015]')

    mix, mix_rate = load(argv[0])
    render, render_rate = load(argv[1])
    bed, bed_rate = load(argv[2])
    vocal, vocal_rate = load(argv[3])
    print('%s: %d Hz; render %d Hz; bed %d Hz; vocal %d Hz'
          % (argv[0].split('/')[-1], mix_rate, render_rate, bed_rate, vocal_rate))
    render = resample_to(render, render_rate, mix_rate)
    bed = resample_to(bed, bed_rate, mix_rate)
    vocal = resample_to(vocal, vocal_rate, mix_rate)

    n = min(len(mix), len(render), len(bed), len(vocal))
    mix_db = envelope(mix[:n], mix_rate, hop)
    render_db = envelope(render[:n], mix_rate, hop)
    bed_db = envelope(bed[:n], mix_rate, hop)
    vocal_db = envelope(vocal[:n], mix_rate, hop)
    m = min(len(mix_db), len(render_db), len(bed_db), len(vocal_db))
    mix_db, render_db, bed_db, vocal_db = (a[:m] for a in (mix_db, render_db, bed_db, vocal_db))

    print('median levels: mix %.1f dB, render %.1f dB, bed %.1f dB, vocal %.1f dB'
          % (np.median(mix_db), np.median(render_db), np.median(bed_db), np.median(vocal_db)))
    print('bed + vocal against the render: r %+.4f'
          % (np.corrcoef(bed_db + vocal_db, render_db)[0, 1]))
    print()
    report("mix - render (the model's own render against the finished mix):",
           mix_db - render_db, hop, threshold)
    print()
    report('bed - render (what the separation did):', bed_db - render_db, hop, threshold)


if __name__ == '__main__':
    main()
