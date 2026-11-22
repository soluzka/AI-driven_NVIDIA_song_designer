"""Close the gated tail the model sometimes leaves on a take.

ACE-Step does not always fill the length it was asked for. Across six 59.9 s
requests the audio truly ended at 55.3, 57.9, 58.2, 59.84, 59.85 and 59.9 s, and
in four of them what preceded that end was not a fade but a burst pattern: 10-120
ms of level, 10-120 ms at about -60 dB, repeated for the last half second to 4.6
seconds. Through the page that is a stutter just before the song stops.

Those bursts carry no music - they are the tail of the latent being gated - so the
fix is to find the last moment that is still continuous, fade the 300 ms before it
and write silence after it. A take that already ends cleanly (the audio runs to
within a tenth of a second of the end) is returned untouched.
"""

import numpy as np

BLOCK = 0.05        # a block counts as solid if every 10 ms window in it is up
FLOOR_DB = 24.0     # "up" is relative to the track's own median level
FADE = 0.30         # seconds of fade, ending at the last solid moment


def solid_end(mono, rate):
    """Time of the last still-continuous block of audio, in seconds."""
    hop = max(1, int(0.01 * rate))
    windows = len(mono) // hop
    if windows < 5:
        return len(mono) / float(rate)
    squares = np.mean(mono[:windows * hop].reshape(windows, hop) ** 2, axis=1)
    db = 20 * np.log10(np.sqrt(squares) + 1e-20)
    loud = db[db > np.median(db) - FLOOR_DB]
    if not len(loud):
        return len(mono) / float(rate)
    above = db > (np.median(loud) - FLOOR_DB)
    per_block = max(1, int(round(BLOCK / 0.01)))
    blocks = windows // per_block
    if blocks < 1:
        return len(mono) / float(rate)
    solid = above[:blocks * per_block].reshape(blocks, per_block).all(axis=1)
    index = np.flatnonzero(solid)
    if not len(index):
        return len(mono) / float(rate)
    return float((index[-1] + 1) * per_block * hop) / rate


def close(frames, rate):
    """Fade the last solid moment out and silence whatever follows it.

    Returns (frames, end_seconds, changed). `frames` is (samples,) or
    (samples, channels) and is modified in place.
    """
    mono = frames.mean(axis=1) if frames.ndim > 1 else frames
    end = solid_end(mono, rate)
    total = len(mono) / float(rate)
    if total - end < 0.15:
        return frames, end, False
    stop = min(len(mono), int(round(end * rate)))
    fade = min(stop, max(2, int(round(FADE * rate))))
    ramp = (np.cos(np.linspace(0.0, np.pi, fade)) * 0.5 + 0.5).astype('float32')
    if frames.ndim > 1:
        frames[stop - fade:stop] *= ramp[:, None]
        frames[stop:] = 0.0
    else:
        frames[stop - fade:stop] *= ramp
        frames[stop:] = 0.0
    return frames, stop / float(rate), True
