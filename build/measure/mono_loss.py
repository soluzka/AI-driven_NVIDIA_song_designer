"""How much of the vocal survives being folded to mono?

The mix path takes the separated vocal, averages its channels and lays that back
over the bed. Averaging is where a wide or out-of-phase stereo vocal cancels: the
sum is quieter than either channel, and at the moments where the vocal dominates
the mix, the music goes with it - a short, deep dip that reads as a rattle.

Per 10 ms window this prints the level of the mono sum against the level of the
two channels, which is the loss the fold costs. Negative is energy destroyed.

Usage: mono_loss.py <vocals.wav> [--hop 0.01]
"""

import sys

import numpy as np
import soundfile as sf


def main():
    path = [a for a in sys.argv[1:] if not a.startswith('--')][0]
    hop_seconds = float(sys.argv[sys.argv.index('--hop') + 1]) if '--hop' in sys.argv else 0.01

    data, rate = sf.read(path, always_2d=True, dtype='float32')
    if data.shape[1] < 2:
        raise SystemExit('that stem is mono already')
    left, right = data[:, 0], data[:, 1]
    mid = (left + right) / 2.0
    sideless = np.sqrt((left ** 2 + right ** 2) / 2.0)     # what the pair really carries

    hop = max(1, int(hop_seconds * rate))
    frames = len(mid) // hop
    mid_rms = np.sqrt(np.mean(mid[:frames * hop].reshape(frames, hop) ** 2, axis=1) + 1e-24)
    pair_rms = np.sqrt(np.mean(sideless[:frames * hop].reshape(frames, hop) ** 2, axis=1) + 1e-24)
    loss = 20 * np.log10(mid_rms / pair_rms + 1e-12)

    print('%-24s %.0f s, %d frames of %.0f ms' % (path.split('/')[-1], len(mid) / float(rate),
                                                 frames, hop * 1000.0 / rate))
    print('loss of the mono fold: median %+.1f dB, 10th %+.1f, 1st %+.1f, worst %+.1f'
          % (np.median(loss), np.percentile(loss, 10), np.percentile(loss, 1), loss.min()))
    quiet = pair_rms > np.percentile(pair_rms, 60)
    print('  in the louder 40%% of the vocal: median %+.1f dB, worst %+.1f dB'
          % (np.median(loss[quiet]), loss[quiet].min()))
    order = np.argsort(loss)
    print('  worst windows:')
    for k in order[:8]:
        print('    %6.2f s  %+6.1f dB   mono %.1f dBFS, the pair %.1f dBFS'
              % (k * hop / float(rate), loss[k],
                 20 * np.log10(mid_rms[k] + 1e-12), 20 * np.log10(pair_rms[k] + 1e-12)))
    share = float(np.mean(loss < -6.0))
    print('  windows losing more than 6 dB: %.1f%% of the stem' % (100 * share))


if __name__ == '__main__':
    main()
