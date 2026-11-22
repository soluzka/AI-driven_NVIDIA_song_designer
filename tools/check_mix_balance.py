"""The voice-over-music balance of every separated render, in one table.

`_mix_separated` sets the lead's gain so the voice sits `LEAD_DB` over the music
under a word, and ducks the music by `DUCK_UNDER_VOICE`. Those two numbers are
what a listener hears as "can I hear the song under the lyric", so after any
change to the mix they have to be measured again on real material - the synthetic
bed reports a different figure from any real song.

    <service-venv-python> tools/check_mix_balance.py [stems-root]

`stems-root` defaults to build/stems/htdemucs, which is where the demucs command
in the README puts one folder per render holding `no_vocals.wav` and `vocals.wav`.
Each song is measured with today's constants and with the pair they replaced, and
the mix is rebuilt with the service's own `_mix_separated` and compared, so the
arithmetic here cannot drift from the service's without the table saying so.

Measured on the twelve renders in the service's logs: 1.3-2.4 dB of voice over
the music under a word (mean 1.7), against 2.8-5.1 dB with the old constants.
"""
import argparse
import contextlib
import glob
import importlib.util
import io
import os
import sys
import tempfile
import threading

import numpy as np
import soundfile as sf
from scipy.signal import fftconvolve

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP = os.path.join(ROOT, 'song_service', 'app.py')
DEFAULT_STEMS = os.path.join(ROOT, 'build', 'stems', 'htdemucs')

# The pair that buried the song under the lyric: 1.5 dB over with a 45% duck.
BEFORE = (1.5, 0.45)


def load_app():
    """The service module, for its constants and its own mix function.

    Importing it starts the lyric helper, which without the `lyrics` module on the
    path prints an ImportError from a background thread. That has nothing to do
    with the mix, so it is kept out of the table.
    """
    spec = importlib.util.spec_from_file_location('song_app', APP)
    module = importlib.util.module_from_spec(spec)
    quiet = io.StringIO()
    with contextlib.redirect_stderr(quiet):
        spec.loader.exec_module(module)
        # The service starts its lyric model in a background thread as it imports.
        # Without the `lyrics` module on this path the thread prints a traceback
        # that says nothing about the mix, so it is kept out of the table by waiting
        # for that thread here, inside the redirect.
        for thread in threading.enumerate():
            if thread is not threading.main_thread() and thread.daemon:
                thread.join(2.0)
    return module


def rms(x):
    return float(np.sqrt(np.mean(np.square(x))) or 0.0)


def db(ratio):
    return 20 * np.log10(max(ratio, 1e-12))


def load_pair(backing_path, vocal_path, app):
    """The bed and the voice as `_mix_separated` receives them."""
    bed, rate = sf.read(backing_path, always_2d=True)
    vocal, vocal_rate = sf.read(vocal_path, always_2d=True)
    mono = vocal.mean(axis=1).astype('float32')
    if vocal_rate != rate and len(mono):
        mono = np.interp(np.linspace(0, len(mono) - 1, int(len(mono) * rate / vocal_rate)),
                         np.arange(len(mono)), mono).astype('float32')
    with contextlib.redirect_stdout(io.StringIO()):
        placed = app._map_vocal(mono, rate, (0.0, 0.0, 0.0))
    frames = min(len(placed), len(bed))
    return bed[:frames].copy(), placed[:frames], rate


def gate_for(placed, rate):
    """The service's presence gate: a 50 ms envelope of the voice, normalised so a
    word passes and a gap does not."""
    win = max(1, int(rate * 0.05))
    hit = fftconvolve(np.abs(placed), np.ones(win) / win, mode='same')
    return np.clip(hit / (float(hit.max()) * 0.12 + 1e-9), 0.0, 1.0)


def balance(bed, placed, rate, lead_db, duck, app):
    """The arithmetic `_mix_separated` does, with the two constants as arguments."""
    gate = gate_for(placed, rate)
    ducked = bed * (1.0 - duck * gate)[:, None]

    active = gate > 0.5
    bed_rms = float(np.sqrt(np.mean(ducked[active] ** 2)) or 0.0) if np.any(active) else rms(ducked)
    voice_rms = float(np.sqrt(np.mean(placed[active] ** 2)) or 0.0) if np.any(active) else rms(placed)
    gain = 1.0
    if bed_rms > 1e-6 and voice_rms > 1e-6:
        gain = min(app.MAX_LEAD_GAIN, (bed_rms * 10 ** (lead_db / 20.0)) / voice_rms)
    voice = placed * gain
    mixed = ducked + voice[:, None].repeat(bed.shape[1], axis=1)
    # `_mix_separated` holds the mix under 0.98 with one peak trim at the end. It
    # is a scale on voice and music together, so it moves no ratio here, but the
    # rebuilt file has to have it or the comparison reads a hot song as a mismatch.
    peak = float(np.max(np.abs(mixed)) or 0.0)
    if peak > 0.98:
        mixed *= 0.98 / peak
    return {'active': active, 'ducked': ducked, 'voice': voice,
            'mixed': mixed, 'gain_db': db(gain)}


def measure(bed, placed, rate, lead_db, duck, app):
    """The dip under a word and the voice's height over the music there, in dB."""
    out = balance(bed, placed, rate, lead_db, duck, app)
    active = out['active']
    if not np.any(active):
        return {'active': active, 'dip': 0.0, 'over': 0.0, 'gain_db': out['gain_db']}
    underwater = out['ducked'][active].mean(axis=1)
    dip = db(rms(bed[active].mean(axis=1)) / max(rms(underwater), 1e-12))
    over = db(rms(out['voice'][active]) / max(rms(underwater), 1e-12))
    return {'active': active, 'dip': dip, 'over': over, 'gain_db': out['gain_db'],
            'mixed': out['mixed']}


def check_service(bed_path, vocal_path, ours, app):
    """Rebuild the same mix with the service's own function and compare."""
    work = tempfile.mkdtemp(prefix='mixbatch-')
    out_path = os.path.join(work, 'service.wav')
    with contextlib.redirect_stdout(io.StringIO()):
        app._mix_separated(bed_path, vocal_path, out_path, (0.0, 0.0, 0.0))
    from_service, _ = sf.read(out_path, always_2d=True)
    os.unlink(out_path)
    os.rmdir(work)
    return float(np.max(np.abs(from_service[:len(ours)] - ours)))


def main(argv=None):
    parser = argparse.ArgumentParser(description='The voice-over-music balance of every separated render.')
    parser.add_argument('stems', nargs='?', default=DEFAULT_STEMS,
                        help='folder of htdemucs/<song>/{no_vocals,vocals}.wav (default: %(default)s)')
    args = parser.parse_args(argv)

    app = load_app()
    folders = sorted(path for path in glob.glob(os.path.join(args.stems, '*')) if os.path.isdir(path))
    pairs = []
    for folder in folders:
        backing = os.path.join(folder, 'no_vocals.wav')
        vocal = os.path.join(folder, 'vocals.wav')
        if os.path.exists(backing) and os.path.exists(vocal):
            pairs.append((folder, backing, vocal))
    if not pairs:
        raise SystemExit('no no_vocals.wav + vocals.wav pairs under %s' % args.stems)

    print('%d songs: LEAD_DB %.1f dB, DUCK_UNDER_VOICE %.0f%% (measured %s)'
          % (len(pairs), app.LEAD_DB, app.DUCK_UNDER_VOICE * 100,
             os.path.relpath(APP, ROOT).replace(os.sep, '/')))
    print()
    print('%-40s %8s %9s %10s %8s %9s %8s'
          % ('render', 'sounding', 'dip now', 'voice now', 'dip old', 'voice old', 'rebuild'))
    rows = []
    worst = 0.0
    for folder, backing, vocal in pairs:
        bed, placed, rate = load_pair(backing, vocal, app)
        now = measure(bed, placed, rate, app.LEAD_DB, app.DUCK_UNDER_VOICE, app)
        before = measure(bed, placed, rate, BEFORE[0], BEFORE[1], app)
        diff = check_service(backing, vocal, now['mixed'], app)
        worst = max(worst, diff)
        sounding = 100.0 * now['active'].mean()
        rows.append((os.path.basename(folder), sounding, now['dip'], now['over'], before['dip'], before['over']))
        print('%-40s %7.0f%% %8.1f %9.1f %8.1f %8.1f %8.1e'
              % (rows[-1][0][:40], sounding, now['dip'], now['over'], before['dip'], before['over'], diff))

    over_now = [row[3] for row in rows]
    over_old = [row[5] for row in rows]
    lo = min(rows, key=lambda row: row[3])
    hi = max(rows, key=lambda row: row[3])
    print()
    print('voice over the music now : %.1f-%.1f dB, mean %.1f over %d songs'
          % (lo[3], hi[3], sum(over_now) / len(over_now), len(rows)))
    print('  lowest  %s (%.1f dB)' % (lo[0][:40], lo[3]))
    print('  highest %s (%.1f dB)' % (hi[0][:40], hi[3]))
    print('voice over the music old : %.1f-%.1f dB, mean %.1f'
          % (min(over_old), max(over_old), sum(over_old) / len(over_old)))
    print('music dips under a word  : %.1f-%.1f dB now, %.1f-%.1f dB old'
          % (min(row[2] for row in rows), max(row[2] for row in rows),
             min(row[4] for row in rows), max(row[4] for row in rows)))
    print('rebuild vs the service\'s own mix: worst difference %.1e' % worst)
    # The whole point is that this table describes the service, so a mix that no
    # longer matches it is a failure, not a footnote.
    if worst > 1e-3:
        print()
        print('the service\'s `_mix_separated` no longer matches this arithmetic: '
              'the balance has changed, and the table above is not the service\'s.')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
