"""Is there a periodic flicker in the level?

A headset that flickers, or a take whose latent gates, shows up as a train of
dips in the level: short holes at a steady rate. Music's own dips are not steady -
they follow the arrangement. So this measures two things:

  dips      runs where the 5 ms envelope falls a given number of dB below its own
            +-250 ms surroundings, with their duration, depth and spacing
  rate      the strongest periodicity in the envelope, from 3 to 60 Hz, and how
            far above the rest of the modulation spectrum it stands

Usage: flicker_scan.py <wav> [...] [--depth 6]
"""

import os
import sys

import numpy as np
import soundfile as sf

HOP = 0.005          # 5 ms envelope
SURROUND = 0.25      # +-250 ms local reference


def analyse(path, depth_db):
    data, rate = sf.read(path, always_2d=True, dtype='float32')
    mono = data.mean(axis=1)
    hop = max(1, int(HOP * rate))
    frames = len(mono) // hop
    rms = np.sqrt(np.mean(mono[:frames * hop].reshape(frames, hop) ** 2, axis=1) + 1e-24)
    db = 20 * np.log10(rms)
    span = max(1, int(SURROUND / HOP))
    reference = np.empty(frames, dtype='float32')
    for i in range(frames):
        lo, hi = max(0, i - span), min(frames, i + span + 1)
        reference[i] = np.median(db[lo:hi])
    below = db < reference - depth_db

    runs, start = [], None
    for i, flag in enumerate(below):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            runs.append((start, i))
            start = None
    if start is not None:
        runs.append((start, frames))

    durations = np.array([(b - a) * HOP * 1000.0 for a, b in runs]) if runs else np.array([])
    depths = np.array([float(np.max(reference[a:b] - db[a:b])) for a, b in runs]) if runs else np.array([])
    starts = np.array([a * HOP for a, _ in runs]) if runs else np.array([])
    gaps = np.diff(starts) if len(starts) > 1 else np.array([])

    # Periodicity of the level itself: FFT of the (mean-removed) envelope.
    envelope = db - db.mean()
    spectrum = np.abs(np.fft.rfft(envelope * np.hanning(len(envelope))))
    freqs = np.fft.rfftfreq(len(envelope), HOP)
    band = (freqs >= 3) & (freqs <= 60)
    if band.any():
        peak_index = np.flatnonzero(band)[np.argmax(spectrum[band])]
        peak_hz = float(freqs[peak_index])
        peak_ratio = float(spectrum[peak_index] / (np.median(spectrum[band]) + 1e-12))
    else:
        peak_hz, peak_ratio = 0.0, 0.0

    print('%-30s %5.0f s   dips %4d   rate %.1f Hz (x%.1f the floor)'
          % (os.path.basename(path), len(mono) / float(rate), len(runs), peak_hz, peak_ratio))
    if len(runs):
        print('   duration  median %.0f ms  longest %.0f ms  (10th %.0f)'
              % (np.median(durations), durations.max(), np.percentile(durations, 10)))
        print('   depth     median %.1f dB  deepest %.1f dB' % (np.median(depths), depths.max()))
        if len(gaps) > 1:
            steady = float(np.std(gaps) / (np.mean(gaps) + 1e-9))
            print('   spacing   median %.0f ms  steadiness %.2f (0 = perfectly regular)'
                  % (np.median(gaps) * 1000.0, steady))
        print('   first     %s s' % ', '.join('%.2f' % s for s in starts[:6]))
    print()


def main():
    paths = [a for a in sys.argv[1:] if not a.startswith('--')]
    depth_db = 6.0
    if '--depth' in sys.argv:
        depth_db = float(sys.argv[sys.argv.index('--depth') + 1])
    if not paths:
        raise SystemExit('usage: flicker_scan.py <wav> [...] [--depth 6]')
    for path in paths:
        analyse(path, depth_db)


if __name__ == '__main__':
    main()
