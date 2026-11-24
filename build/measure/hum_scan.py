"""Is the low end a hum, or is it the bass moving?

Rumble and a hum are different faults. Rumble is broadband low energy that
follows the arrangement; a hum is one pitch that sits still for the whole song.
So for every render this prints the DC offset, the share of power in four low
bands, and then the test that matters: the strongest bin in 20-120 Hz and how
often, across the song, that same pitch is within 6 dB of its own median level.

    steady  : share of frames where that pitch holds its level
    crest   : median pitch level minus the band's median level in the same frames
              (a hum sits above the band it hums in; a bass line does not)

Usage: hum_scan.py <wav> [...]      (no arguments: every song in song_service/logs)
"""

import glob
import os
import sys

import numpy as np
import soundfile as sf
from scipy.signal import welch

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
LOGS = os.path.join(ROOT, 'song_service', 'logs')

BANDS = [(20, 32), (32, 45), (45, 60), (60, 120)]


def analyse(path):
    data, rate = sf.read(path, always_2d=True, dtype='float32')
    mono = data.mean(axis=1)
    total_seconds = len(mono) / float(rate)
    dc = float(np.mean(mono))

    f, p = welch(mono, fs=rate, nperseg=65536, noverlap=32768, window='hann',
                 detrend=False, scaling='density')
    total = p[(f >= 20) & (f <= 20000)].sum()

    def share(lo, hi):
        return 10 * np.log10(p[(f >= lo) & (f < hi)].sum() / total + 1e-30)

    # The pitch that hums: the loudest bin under 120 Hz over the whole song.
    low = (f >= 20) & (f <= 120)
    peak_hz = float(f[low][np.argmax(p[low])])

    # Now watch that pitch frame by frame. 32768 samples is 0.68 s at 48 kHz,
    # long enough to resolve 20 Hz two bins apart, short enough that a bass note
    # that changes does not get averaged into a station.
    nperseg = 32768
    hop = nperseg // 2
    frame_count = 1 + (len(mono) - nperseg) // hop
    window = np.hanning(nperseg).astype('float32')
    freqs = np.fft.rfftfreq(nperseg, 1.0 / rate)
    lo_hz, hi_hz = peak_hz / 1.122, peak_hz * 1.122      # +- a sixth of an octave
    band_mask = (freqs >= 20) & (freqs <= 120)
    pitch_mask = (freqs >= lo_hz) & (freqs <= hi_hz)
    if not pitch_mask.any():
        pitch_mask = np.zeros_like(band_mask)

    pitch_levels, band_levels = [], []
    for i in range(frame_count):
        piece = mono[i * hop:i * hop + nperseg]
        if piece.shape[0] < nperseg:
            break
        spectrum = np.abs(np.fft.rfft(piece * window)) ** 2
        pitch_levels.append(10 * np.log10(spectrum[pitch_mask].max() + 1e-30))
        band_levels.append(10 * np.log10(spectrum[band_mask].sum() + 1e-30))

    pitch_levels = np.asarray(pitch_levels)
    band_levels = np.asarray(band_levels)
    median_pitch = float(np.median(pitch_levels))
    steady = float(np.mean(np.abs(pitch_levels - median_pitch) <= 6.0))
    crest = median_pitch - float(np.median(band_levels))

    return {
        'name': os.path.basename(path),
        'seconds': total_seconds,
        'rate': rate,
        'dc': dc,
        'peak_hz': peak_hz,
        'steady': steady,
        'crest': crest,
        'shares': [share(lo, hi) for lo, hi in BANDS],
    }


def main():
    paths = sys.argv[1:]
    if not paths:
        paths = sorted(glob.glob(os.path.join(LOGS, 'song_*.wav')) +
                       glob.glob(os.path.join(LOGS, 'last_song.wav')))
    if not paths:
        raise SystemExit('no renders found')

    header = '%-42s %6s %7s %6s %6s %6s %6s %6s %6s' % (
        '', 'sec', 'peakHz', 'steady', 'crest', '20-32', '32-45', '45-60', '60-120')
    print(header)
    print('-' * len(header))
    rows = []
    for path in paths:
        row = analyse(path)
        rows.append(row)
        print('%-42s %6.1f %7.1f %5.0f%% %6.1f %6.1f %6.1f %6.1f %6.1f' % (
            row['name'][:42], row['seconds'], row['peak_hz'], row['steady'] * 100,
            row['crest'], *row['shares']))

    print()
    print('steady = share of 0.68 s frames where the peak pitch holds within 6 dB')
    print('crest  = that pitch above the whole 20-120 Hz band (dB)')
    if rows:
        worst = max(rows, key=lambda r: (r['steady'], r['crest']))
        print('worst: %s -- %.1f Hz holds %.0f%% of the song, %.1f dB over the band, '
              'DC %+.5f' % (worst['name'], worst['peak_hz'], worst['steady'] * 100,
                            worst['crest'], worst['dc']))


if __name__ == '__main__':
    main()
