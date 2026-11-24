"""Follow one render's lowest pitch through the song.

hum_scan said the loudest thing under 120 Hz is 35-37 Hz and it holds for most
of the song. A held note and a bass line look the same in a whole-song average,
so this prints the pitch every 4 seconds with its level, and then the longest
stretch where that same pitch (within a semitone) also keeps its level (within
6 dB) -- the signature of a note that never stops, as opposed to a bass line
that comes and goes.

Usage: hum_track.py <wav> [step_seconds]
"""

import sys

import numpy as np
import soundfile as sf

path = sys.argv[1]
step = float(sys.argv[2]) if len(sys.argv) > 2 else 4.0

data, rate = sf.read(path, always_2d=True, dtype='float32')
mono = data.mean(axis=1)
seconds = len(mono) / float(rate)

N = 16384                                  # 0.34 s at 48 kHz: resolves 20 Hz bins
hop = N // 2
window = np.hanning(N).astype('float32')
freqs = np.fft.rfftfreq(N, 1.0 / rate)
mask = (freqs >= 20) & (freqs <= 120)
band = freqs[mask]

frames = 1 + (len(mono) - N) // hop
peaks, levels = [], []
for i in range(frames):
    piece = mono[i * hop:i * hop + N]
    spectrum = np.abs(np.fft.rfft(piece * window))
    low = spectrum[mask]
    j = int(np.argmax(low))
    peaks.append(float(band[j]))
    levels.append(20 * np.log10(float(low[j]) / (N / 2.0) + 1e-12) + 100.0)

peaks = np.asarray(peaks)
levels = np.asarray(levels) - 100.0
frame_seconds = hop / float(rate)
times = np.arange(len(peaks)) * frame_seconds

# The mode pitch, and then the one run where it neither changes nor drops.
pitch_mode = float(np.median(peaks))
same = np.abs(12 * np.log2(np.maximum(peaks, 1e-9) / pitch_mode)) <= 1.0
held = levels >= (np.median(levels[same]) - 6.0) if same.any() else np.zeros_like(same)
both = same & held

runs, start = [], None
for i, flag in enumerate(both):
    if flag and start is None:
        start = i
    elif not flag and start is not None:
        runs.append((start, i))
        start = None
if start is not None:
    runs.append((start, len(both)))
longest = max(runs, key=lambda r: r[1] - r[0]) if runs else (0, 0)

print('file        %s' % path)
print('length      %.1f s   frames %d (%.2f s each)' % (seconds, frames, frame_seconds))
print('pitch mode  %.1f Hz   dominant on %.0f%% of frames' % (pitch_mode, 100 * same.mean()))
print('held        %.0f%% of frames   longest run %.1f s (%.1f-%.1f s)' % (
    100 * both.mean(), (longest[1] - longest[0]) * frame_seconds,
    times[longest[0]], times[max(longest[0], longest[1] - 1)]))
print()
print('%8s %8s %8s' % ('at s', 'dominant', 'level'))
for t in np.arange(0, seconds - step, step):
    sel = (times >= t) & (times < t + step)
    if not sel.any():
        continue
    print('%8.1f %7.1f %7.1f dB' % (t, float(np.median(peaks[sel])),
                                    float(np.median(levels[sel]))))
