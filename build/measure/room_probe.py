"""How dark and how quiet does the room have to be to stop cancelling the voice?

The room is a noise burst convolved with the vocal and normalised to the vocal's own
RMS, then added at `wet`. White noise is not a room: it is a hiss, and its peaks are
10 dB over its RMS, so they cancel the voice where they meet it out of phase - which
is heard as a quiet rattle.

For each candidate tail this prints the number of 1.5 ms windows where the render
plus the tail falls more than 6 dB below the render, which is the fault being fixed.

Usage: room_probe.py <render.wav> <vocal.wav>
"""

import sys

import numpy as np
import soundfile as sf
from scipy.signal import butter, fftconvolve, sosfilt

HOP = 0.0015


def envelope_db(mono, rate, hop_seconds=HOP):
    hop = max(1, int(hop_seconds * rate))
    frames = len(mono) // hop
    rms = np.sqrt(np.mean(mono[:frames * hop].reshape(frames, hop) ** 2, axis=1) + 1e-24)
    return 20 * np.log10(rms)


def make_tail(rate, loudness_seconds=0.45, corner=None):
    length = max(8, int(rate * loudness_seconds))
    rng = np.random.default_rng(7)
    tail = rng.standard_normal(length).astype('float32')
    tail *= np.exp(-np.linspace(0.0, 5.0, length)).astype('float32')
    if corner:
        tail = sosfilt(butter(2, corner / (rate / 2.0), btype='low', output='sos'),
                       tail).astype('float32')
    tail[0] = 0.0
    return tail


def count_dips(render, tail_signal, rate, threshold=6.0):
    mixed = render + tail_signal
    before = envelope_db(render, rate)
    after = envelope_db(mixed, rate)
    n = min(len(before), len(after))
    return int(np.sum((after[:n] - before[:n]) < -threshold))


def main():
    render, rate = sf.read(sys.argv[1], always_2d=True, dtype='float32')
    vocal, vocal_rate = sf.read(sys.argv[2], always_2d=True, dtype='float32')
    if vocal_rate != rate:
        from fractions import Fraction

        from scipy.signal import resample_poly

        ratio = Fraction(int(vocal_rate), int(rate)).limit_denominator(1000)
        render = resample_poly(render, ratio.numerator, ratio.denominator, axis=0)
        rate = vocal_rate
    render_mono = render.mean(axis=1)
    vocal_mono = vocal.mean(axis=1)
    n = min(len(render_mono), len(vocal_mono))
    render_mono, vocal_mono = render_mono[:n], vocal_mono[:n]
    dry_rms = float(np.sqrt(np.mean(vocal_mono ** 2)) + 1e-12)

    print('%-38s %8s %10s' % ('tail', 'wet', 'dips'))
    print('-' * 60)
    for corner, corner_label in ((None, 'white noise'), (6000, 'low-pass 6 kHz'),
                                 (3000, 'low-pass 3 kHz'), (1500, 'low-pass 1.5 kHz')):
        tail = make_tail(rate, corner=corner)
        wet_signal = fftconvolve(vocal_mono, tail, mode='full')[:n].astype('float32')
        wet_rms = float(np.sqrt(np.mean(wet_signal ** 2)) + 1e-12)
        matched = wet_signal * (dry_rms / wet_rms)
        for wet in (0.26, 0.15, 0.08):
            dips = count_dips(render_mono, matched * wet, rate)
            print('%-38s %8.2f %10d' % (corner_label, wet, dips))


if __name__ == '__main__':
    main()
