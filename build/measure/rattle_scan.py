"""Find a rattle too quiet to hear as level: jumps and flickers.

A rattle under the music is not a level problem, so no band measurement shows it.
It leaves one of two marks:

  jump     a sample-to-sample step far larger than its neighbours (a click)
  flicker  a 1-10 ms hole where the audio drops far below its surroundings

Both are judged against the local signal, not the whole track, so a quiet passage
is not called a fault for being quiet. Every hit is reported in dBFS and in dB
below the track's peak, which is what makes "really quiet" a number.

With --after, the service's own low-end treatment is applied first and both
versions are scanned, so a change can be blamed or cleared.

Usage: rattle_scan.py <wav> [--after]
"""

import os
import sys

import numpy as np
import soundfile as sf

HERE = os.path.dirname(os.path.abspath(__file__))

BLOCK = 0.05        # local reference window
JUMP_FACTOR = 12.0  # a step this many times the local median is a click
GAP_DB = 25.0       # a hole this far below its surroundings is a flicker
GAP_WINDOW = 0.0015  # 1.5 ms: short enough to catch a flicker, long enough to be real


def load_treatment():
    sys.path.insert(0, HERE)
    from test_sub_fix import load_helper         # noqa: PLC0415 - path set above
    return load_helper()


def scan(mono, rate, label):
    peak = float(np.max(np.abs(mono)) + 1e-12)
    peak_db = 20 * np.log10(peak)

    def dbfs(value):
        return 20 * np.log10(max(float(value), 1e-12))

    # --- jumps ---------------------------------------------------------------
    step = np.abs(np.diff(mono))
    block = max(8, int(BLOCK * rate))
    blocks = len(step) // block
    reference = np.empty(blocks, dtype='float32')
    for i in range(blocks):
        reference[i] = np.median(step[i * block:(i + 1) * block])
    floor = float(np.median(reference)) * 0.5
    reference = np.maximum(reference, max(floor, 1e-9))
    if blocks:
        tail = np.full(len(step) - blocks * block, float(reference[-1]), dtype='float32')
        expanded = np.concatenate([np.repeat(reference, block)[:len(step)], tail])
    else:
        expanded = np.full(len(step), 1e-9, dtype='float32')
    expanded = np.maximum(expanded, 1e-9)
    ratio = step / expanded
    hits = np.flatnonzero(ratio > JUMP_FACTOR)
    # One click often trips on two adjacent samples; report the local maxima only.
    keeps = []
    for index in hits:
        if not keeps or index - keeps[-1] > 2:
            keeps.append(int(index))
        elif step[index] > step[keeps[-1]]:
            keeps[-1] = int(index)
    keeps = keeps[:2000]
    jumps = [(i / float(rate), float(step[i])) for i in keeps]

    # --- flickers ------------------------------------------------------------
    window = max(4, int(GAP_WINDOW * rate))
    frames = len(mono) // window
    rms = np.sqrt(np.mean(mono[:frames * window].reshape(frames, window) ** 2, axis=1) + 1e-24)
    rms_db = 20 * np.log10(rms)
    span = max(1, int(0.05 / GAP_WINDOW))          # +-50 ms of surroundings
    holes = []
    for i in range(span, frames - span):
        around = np.median(rms_db[i - span:i + span + 1])
        if rms_db[i] < around - GAP_DB:
            if not holes or i - holes[-1][1] > 2:
                holes.append([i, i])
            else:
                holes[-1][1] = i
    flickers = [((a * window) / float(rate), ((b - a + 1) * window) / float(rate) * 1000.0,
                 float(rms_db[a:b + 1].min())) for a, b in holes]

    print('%-28s peak %.1f dBFS   %d s' % (label, peak_db, len(mono) / float(rate)))
    if jumps:
        levels = [dbfs(amplitude / 2.0) for _, amplitude in jumps]
        worst = sorted(range(len(jumps)), key=lambda k: levels[k], reverse=True)[:5]
        print('  jumps    %d   worst %s dBFS (%s dB under the peak)'
              % (len(jumps), ' / '.join('%.1f' % levels[k] for k in worst),
                 ' / '.join('%.1f' % (peak_db - levels[k]) for k in worst)))
        if len(jumps) > 2:
            times = np.array([t for t, _ in jumps])
            gaps = np.diff(times)
            print('           first %s s ... last %.1f s   spacing median %.3f s'
                  % (', '.join('%.2f' % t for t, _ in jumps[:4]), times[-1], float(np.median(gaps))))
    else:
        print('  jumps    0')
    if flickers:
        worst = sorted(flickers, key=lambda f: f[2])[:5]
        print('  flickers %d   longest %.1f ms   quietest %.1f dBFS (%s dB under the peak)'
              % (len(flickers), max(f[1] for f in flickers), min(f[2] for f in flickers),
                 '%.1f' % (peak_db - min(f[2] for f in flickers))))
        print('           first %s s' % ', '.join('%.2f' % f[0] for f in flickers[:4]))
    else:
        print('  flickers 0')
    print()


def main():
    paths = [a for a in sys.argv[1:] if not a.startswith('--')]
    after = '--after' in sys.argv
    if not paths:
        raise SystemExit('usage: rattle_scan.py <wav> [--after]')
    helper = load_treatment() if after else None
    for path in paths:
        data, rate = sf.read(path, always_2d=True, dtype='float32')
        mono = data.mean(axis=1)
        scan(mono, rate, os.path.basename(path))
        if helper is not None:
            treated = helper['_fix_low_end'](data.copy(), rate).mean(axis=1)
            scan(treated, rate, os.path.basename(path) + ' + treatment')


if __name__ == '__main__':
    main()
