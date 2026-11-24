"""Why the sub reads as a hum, and what a filter would do about it.

Two things make a sub read as a hum rather than a bass note: it carries most of
the song's power, and it is nearly a sine, so there is nothing above it for the
ear to hear as a note. This prints both for one render, then runs candidate
low-end chains over that same audio and prints what each one leaves, so a corner
can be chosen from numbers instead of taste.

Usage: sub_check.py <wav>
"""

import sys

import numpy as np
import soundfile as sf
from scipy.signal import butter, sosfilt, sosfreqz

path = sys.argv[1]

data, rate = sf.read(path, always_2d=True, dtype='float32')
mono = data.mean(axis=1)

N = 32768
hop = N // 2
window = np.hanning(N).astype('float32')
freqs = np.fft.rfftfreq(N, 1.0 / rate)
frames = 1 + (len(mono) - N) // hop

ratios, fundamentals, shares = [], [], []


def share(frames_mono, lo, hi):
    """Share of the song's power that sits in one band, in dB."""
    total, band = [], []
    for i in range(0, frames_mono.shape[0] - N, hop):
        piece = frames_mono[i:i + N]
        spectrum = np.abs(np.fft.rfft(piece * window)) ** 2
        total.append(spectrum[(freqs >= 20) & (freqs <= 20000)].sum())
        band.append(spectrum[(freqs >= lo) & (freqs <= hi)].sum())
    total = np.asarray(total)
    band = np.asarray(band)
    keep = total > 0
    return float(np.median(10 * np.log10(band[keep] / total[keep] + 1e-30)))


for i in range(frames):
    piece = mono[i * hop:i * hop + N]
    if piece.shape[0] < N:
        break
    spectrum = np.abs(np.fft.rfft(piece * window)) ** 2
    low = (freqs >= 30) & (freqs <= 90)
    if not low.any():
        continue
    j = int(np.flatnonzero(low)[np.argmax(spectrum[low])])
    f0 = freqs[j]
    if f0 < 30 or f0 > 90:
        continue

    def at(target):
        idx = int(round(target / (rate / float(N))))
        lo, hi = max(0, idx - 2), min(len(spectrum), idx + 3)
        return float(spectrum[lo:hi].max())

    fundamental = at(f0)
    second = at(2 * f0)
    if fundamental <= 0:
        continue
    fundamentals.append(f0)
    ratios.append(10 * np.log10(second / fundamental + 1e-30))

ratios = np.asarray(ratios)
fundamentals = np.asarray(fundamentals)
print('file        %s' % path)
print('bass pitch  median %.1f Hz, 10th %.1f, 90th %.1f' % (
    np.median(fundamentals), np.percentile(fundamentals, 10),
    np.percentile(fundamentals, 90)))
print('2nd harmonic over fundamental: median %+.1f dB' % float(np.median(ratios)))
print('    (a bass note with body sits near -6 dB; a pure sub sits under -20)')
print()

before = share(mono, 20, 120)
print('as it is')
print('  20-120 Hz share %+.1f dB   20-32 %+.1f   32-45 %+.1f   45-60 %+.1f   '
      '60-120 %+.1f   80-120 %+.1f   120-250 %+.1f   250-2k %+.1f' % (
          before, share(mono, 20, 32), share(mono, 32, 45), share(mono, 45, 60),
          share(mono, 60, 120), share(mono, 80, 120), share(mono, 120, 250),
          share(mono, 250, 2000)))
print()


def shelf(frames_mono, corner, gain_db, order=2):
    """A low shelf built from a lowpass split: exact depth at DC, 6 dB/oct."""
    sos = butter(order, corner / (rate / 2.0), btype='low', output='sos')
    low = sosfilt(sos, frames_mono, axis=0)
    return frames_mono + low * ((10 ** (gain_db / 20.0)) - 1.0)


print('%22s %9s %9s %9s %9s %9s' % ('chain', '20-120', '60-120', '80-120', '120-250', '250-2k'))
print('-' * 74)
chains = [
    ('4-pole 32 (current)', [(4, 32.0)]),
    ('4-pole 40', [(4, 40.0)]),
    ('4-pole 45', [(4, 45.0)]),
    ('4-pole 50', [(4, 50.0)]),
    ('6-pole 45', [(6, 45.0)]),
    ('6-pole 50', [(6, 50.0)]),
    ('4-pole 40 + shelf -4 @ 95', [(4, 40.0), ('shelf', 95.0, -4.0)]),
    ('4-pole 45 + shelf -4 @ 95', [(4, 45.0), ('shelf', 95.0, -4.0)]),
    ('4-pole 40 + shelf -3.7 @ 160', [(4, 40.0), ('shelf', 160.0, -3.7)]),
    ('4-pole 40 + shelf -5 @ 160', [(4, 40.0), ('shelf', 160.0, -5.0)]),
    ('4-pole 40 + shelf -6.5 @ 160', [(4, 40.0), ('shelf', 160.0, -6.5)]),
    ('4-pole 45 + shelf -5 @ 160', [(4, 45.0), ('shelf', 160.0, -5.0)]),
]

for label, stages in chains:
    out = mono
    for stage in stages:
        if stage[0] == 'shelf':
            out = shelf(out, stage[1], stage[2])
        else:
            sos = butter(stage[0], stage[1] / (rate / 2.0), btype='high', output='sos')
            out = sosfilt(sos, out, axis=0).astype('float32')
    print('%22s %9.1f %9.1f %9.1f %9.1f %9.1f' % (
        label, share(out, 20, 120), share(out, 60, 120), share(out, 80, 120),
        share(out, 120, 250), share(out, 250, 2000)))

print()
print('candidate high-passes, response at the pitches that matter')
print('%14s %8s %8s %8s %8s %8s' % ('filter', '35 Hz', '45 Hz', '60 Hz', '80 Hz', '120 Hz'))
for order, hz in [(4, 32.0), (4, 40.0), (4, 45.0), (4, 50.0), (6, 45.0), (6, 50.0)]:
    sos = butter(order, hz / (rate / 2.0), btype='high', output='sos')
    w, h = sosfreqz(sos, worN=8192, fs=rate)
    response = 20 * np.log10(np.abs(h) + 1e-12)

    def at(target):
        return float(response[np.argmin(np.abs(w - target))])

    print('%14s %8.1f %8.1f %8.1f %8.1f %8.1f' % (
        '%d-pole %g' % (order, hz), at(35), at(45), at(60), at(80), at(120)))
