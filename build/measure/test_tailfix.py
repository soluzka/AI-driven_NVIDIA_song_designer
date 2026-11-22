"""Run the tail fix over every render on disk and count the stutter it removes.

A stutter at the end is a run of gaps *inside* the last stretch of audio: 10-300 ms
of silence between 10-300 ms of level. Counting short silence runs over the whole
track says nothing (words have gaps too), so this only looks at the final 2.5 s of
real audio, before and after the fix.
"""

import glob
import os
import sys

import numpy as np
import soundfile as sf

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "song_service"))
import tailfix  # noqa: E402

FLOOR = 24.0
SHORT = (0.008, 0.30)      # a burst gap: 8-300 ms


def db_windows(mono, rate):
    hop = max(1, int(0.01 * rate))
    windows = len(mono) // hop
    sq = np.mean(mono[:windows * hop].reshape(windows, hop) ** 2, axis=1)
    return 20 * np.log10(np.sqrt(sq) + 1e-20), hop


def tail_bursts(mono, rate, end):
    db, hop = db_windows(mono, rate)
    loud = db[db > np.median(db) - FLOOR]
    above = db > (np.median(loud) - FLOOR)
    # The last 350 ms is the fade itself, which passes below the floor on its way
    # to zero by design; the stutter is what sits before it.
    lo = max(0, int((end - 2.5) / 0.01))
    hi = min(len(above), int((end - 0.35) / 0.01))
    edges = np.flatnonzero(np.diff(np.r_[0, (~above[lo:hi]).astype(int), 0]))
    count = 0
    for a, b in zip(edges[::2], edges[1::2]):
        if SHORT[0] <= (b - a) * 0.01 <= SHORT[1]:
            count += 1
    return count


paths = sorted(glob.glob(os.path.join(os.environ["TEMP"], "ace_*", "song.wav")),
               key=os.path.getmtime)

print("%-22s %7s %7s %8s %8s %10s" %
      ("render", "asked", "end", "bursts", "after", "silenced"))
for path in paths:
    data, rate = sf.read(path, always_2d=True, dtype="float32")
    total = len(data) / float(rate)
    end_before = tailfix.solid_end(data.mean(axis=1), rate)
    before = tail_bursts(data.mean(axis=1), rate, end_before)
    fixed, end, changed = tailfix.close(data.copy(), rate)
    after = tail_bursts(fixed.mean(axis=1), rate, end)
    print("%-22s %7.2f %7.2f %8d %8d %10.2f" % (
        os.path.basename(os.path.dirname(path)), total, end, before, after,
        (total - end) if changed else 0.0))
    stop = int(round(end * rate))
    if changed:
        assert float(np.max(np.abs(fixed[stop:]))) == 0.0, "audio past the fade: %s" % path
        assert after == 0, "a burst survived at %s" % path
        fade_db, _ = db_windows(fixed.mean(axis=1)[stop - int(0.3 * rate):stop], rate)
        rise = float(np.max(np.diff(fade_db))) if len(fade_db) > 20 else 0.0
        print("%-22s fade is %s (largest 10 ms rise %+.1f dB)" %
              ("", "monotone" if rise < 1.0 else "NOT monotone", rise))
    assert np.all(np.isfinite(fixed)), "a non-finite sample at %s" % path
    assert len(fixed) == len(data), "the length changed at %s" % path
