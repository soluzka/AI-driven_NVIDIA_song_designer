"""Is the mapped vocal lined up with the bed, and does it add or cancel?

The mix is the demucs bed plus the mapped vocal. If those two arrive with a delay,
or with opposite polarity, their sum has nulls: short, deep holes that sound like
a rattle or a flicker in the level, and that no whole-song measurement notices.

This measures, per window:

  lag    where the vocal best matches the bed's residual, in samples and ms
  sign   + adds, - cancels
  r      the normalised correlation at that lag

Usage: mix_null.py <render.wav> <bed.wav> <vocal.wav> [--at 77] [--span 8]
"""

import sys

import numpy as np
import soundfile as sf
from scipy.signal import fftconvolve


def mono_of(path):
    data, rate = sf.read(path, always_2d=True, dtype='float32')
    return data.mean(axis=1), rate


def main():
    paths = [a for a in sys.argv[1:] if not a.startswith('--')]
    at = float(sys.argv[sys.argv.index('--at') + 1]) if '--at' in sys.argv else 77.0
    span = float(sys.argv[sys.argv.index('--span') + 1]) if '--span' in sys.argv else 8.0
    render, rate = mono_of(paths[0])
    bed, _ = mono_of(paths[1])
    vocal, _ = mono_of(paths[2])
    n = min(len(render), len(bed), len(vocal))
    render, bed, vocal = render[:n], bed[:n], vocal[:n]

    lo = max(0, int((at - span / 2) * rate))
    hi = min(n, int((at + span / 2) * rate))
    residual = (render - bed)[lo:hi]
    piece = vocal[lo:hi]
    print('window %.1f-%.1f s   render %.1f dB   bed %.1f dB   vocal %.1f dB'
          % (at - span / 2, at + span / 2,
             20 * np.log10(np.sqrt(np.mean(residual ** 2)) + 1e-12),
             20 * np.log10(np.sqrt(np.mean(bed[lo:hi] ** 2)) + 1e-12),
             20 * np.log10(np.sqrt(np.mean(piece ** 2)) + 1e-12)))

    # Cross-correlate: positive lag means the vocal arrives after the residual.
    correlation = fftconvolve(residual, piece[::-1], mode='full')
    lags = np.arange(-len(piece) + 1, len(residual))
    limit = int(0.2 * rate)
    window = (lags >= -limit) & (lags <= limit)
    best = np.flatnonzero(window)[np.argmax(np.abs(correlation[window]))]
    lag = int(lags[best])
    peak = float(correlation[best])
    norm = float(np.sqrt(np.sum(residual ** 2) * np.sum(piece ** 2)) + 1e-20)
    print('lag %+d samples (%+.2f ms)   sign %s   r %+.3f'
          % (lag, 1000.0 * lag / rate, '+' if peak >= 0 else '-', peak / norm))

    # What the sum does at the moment of the deepest null inside the window.
    mix = bed + vocal
    hop = int(0.0015 * rate)
    frames = (hi - lo) // hop
    render_db = 20 * np.log10(np.sqrt(np.mean(residual.reshape(frames, hop) ** 2, axis=1)) + 1e-12)
    mix_db = 20 * np.log10(np.sqrt(np.mean(mix[lo:lo + frames * hop].reshape(frames, hop) ** 2, axis=1)) + 1e-12)
    # residual is render-bed, so render = residual + bed
    render_only = 20 * np.log10(np.sqrt(np.mean((residual + bed[lo:hi]).reshape(frames, hop) ** 2, axis=1)) + 1e-12)
    gap = render_only - mix_db
    worst = int(np.argmax(gap))
    print('deepest null: %.3f s   render %.1f dB   mix %.1f dB   the mix is %.1f dB down'
          % ((lo + worst * hop) / rate, render_only[worst], mix_db[worst], gap[worst]))
    print('frames where the mix is more than 6 dB under the render: %d of %d'
          % (int(np.sum(gap > 6)), frames))


if __name__ == '__main__':
    main()
