"""Do the two stems sum back to the render?

Every comparison of the finished mix against the render assumes bed + vocal = render.
demucs is a mask, so that should hold; if it does not, a "dip in the mix" can be an
artefact of the comparison rather than anything anyone hears. This prints the sum's
correlation with the render, the best-fit scale, and the three levels in 1.5 ms
windows around the dip that was measured.

Usage: stem_sum.py <render.wav> <bed.wav> <vocal.wav> [--at 77.088]
"""

import sys

import numpy as np
import soundfile as sf


def mono_of(path):
    data, rate = sf.read(path, always_2d=True, dtype='float32')
    return data.mean(axis=1), rate, data.shape[1]


def db(value):
    return 20 * np.log10(max(float(value), 1e-12))


def main():
    argv = sys.argv[1:]
    at = 77.088
    if '--at' in argv:
        index = argv.index('--at')
        at = float(argv[index + 1])
        del argv[index:index + 2]
    render, rate, render_ch = mono_of(argv[0])
    bed, bed_rate, bed_ch = mono_of(argv[1])
    vocal, vocal_rate, vocal_ch = mono_of(argv[2])
    n = min(len(render), len(bed), len(vocal))
    render, bed, vocal = render[:n], bed[:n], vocal[:n]
    print('render %d ch @ %d, bed %d ch @ %d, vocal %d ch @ %d, %d samples'
          % (render_ch, rate, bed_ch, bed_rate, vocal_ch, vocal_rate, n))

    total = bed + vocal
    correlation = float(np.dot(total, render) /
                        (np.sqrt(np.dot(total, total) * np.dot(render, render)) + 1e-20))
    scale = float(np.dot(total, render) / (np.dot(total, total) + 1e-20))
    residual = total - render * scale
    print('bed + vocal against the render: r %+.4f, best scale %.3f, residual %.1f dB'
          % (correlation, scale, db(np.sqrt(np.mean(residual ** 2)))))

    hop = int(0.0015 * rate)
    lo = max(0, int((at - 0.03) * rate))
    hi = min(n, int((at + 0.03) * rate))
    frames = (hi - lo) // hop
    print()
    print('%9s %9s %9s %9s %9s' % ('at s', 'render', 'bed', 'vocal', 'sum'))

    def window_db(signal):
        piece = signal[lo:lo + frames * hop].reshape(frames, hop)
        return 20 * np.log10(np.sqrt(np.mean(piece ** 2, axis=1)) + 1e-12)

    rows = [window_db(render), window_db(bed), window_db(vocal), window_db(total)]
    for i in range(frames):
        if (i % 4) == 0 or rows[0][i] - rows[3][i] > 20:
            print('%9.3f %9.1f %9.1f %9.1f %9.1f'
                  % ((lo + i * hop) / rate, rows[0][i], rows[1][i], rows[2][i], rows[3][i]))


if __name__ == '__main__':
    main()
