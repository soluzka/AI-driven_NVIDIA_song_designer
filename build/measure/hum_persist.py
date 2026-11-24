"""Which low pitch never stops?

A bass line has notes: each pitch is loud when it is played and gone when it is
not, so its level over the song swings widely. A hum is the opposite - one
narrow band that sits at almost the same level from the first second to the last,
whether or not anything else is playing.

For every 1/24-octave band from 20 to 120 Hz this prints how much the band's
level moves across the song (90th minus 10th percentile; small = constant) next
to how loud it is, and then the bands that hold stillest.

Usage: hum_persist.py <wav> [--top N]
"""

import sys

import numpy as np
import soundfile as sf

path = [a for a in sys.argv[1:] if not a.startswith('--')][0]
top = 10
if '--top' in sys.argv:
    top = int(sys.argv[sys.argv.index('--top') + 1])
lo_hz = 20.0
hi_hz = 8000.0
for flag, attr in (('--lo', 'lo_hz'), ('--hi', 'hi_hz')):
    if flag in sys.argv:
        globals()[attr] = float(sys.argv[sys.argv.index(flag) + 1])

data, rate = sf.read(path, always_2d=True, dtype='float32')
mono = data.mean(axis=1)

N = 16384                       # 0.34 s at 48 kHz
hop = N // 2
window = np.hanning(N).astype('float32')
freqs = np.fft.rfftfreq(N, 1.0 / rate)
useful = (freqs >= lo_hz) & (freqs <= hi_hz)

frames = 1 + (len(mono) - N) // hop
power = np.empty((frames, int(useful.sum())), dtype='float32')
for i in range(frames):
    spectrum = np.abs(np.fft.rfft(mono[i * hop:i * hop + N] * window))
    power[i] = (spectrum[useful] ** 2).astype('float32')

band = freqs[useful]
db = 10 * np.log10(power + 1e-30)
p10 = np.percentile(db, 10, axis=0)
p50 = np.percentile(db, 50, axis=0)
p90 = np.percentile(db, 90, axis=0)
swing = p90 - p10                       # dB the band moves over the song
overall = 10 * np.log10(power.sum(axis=1) + 1e-30)
loudness = p50 - float(np.median(overall))   # dB under the song's own median level

print('file   %s' % path)
print('       %.1f s, %d frames of %.2f s, %.0f-%.0f Hz' % (
    len(mono) / float(rate), frames, hop / float(rate), lo_hz, hi_hz))
print()
print('%8s %8s %8s %8s   %s' % ('Hz', 'p10', 'p50', 'swing', 'verdict'))
print('-' * 62)
for hz, lo, mid, sw in zip(band, p10, p50, swing):
    if sw < 6.0 and mid - float(np.median(overall)) > -60.0:
        verdict = 'never stops' if sw < 3.0 else 'nearly constant'
        print('%8.1f %8.1f %8.1f %8.1f   %s' % (hz, lo, mid, sw, verdict))
print()
order = np.argsort(swing)[:top]
print('stillest bands (smallest swing):')
for i in order:
    print('%8.1f Hz  swing %5.1f dB   10th %7.1f  median %7.1f  over song median %6.1f dB' % (
        band[i], swing[i], p10[i], p50[i], loudness[i]))
print()
print('reference: a note that is only played now and then swings 20-40 dB;')
print('a hum swings less than 6 dB across the whole song.')
