"""The voice-over-music balance of the render-based mix, in one table.

The finished song is now the render itself with only the mapping's difference added,
so **no lead gain is applied at all**: the voice sits where the model put it. The only
thing that moves the level under a word is the duck, and it scales the whole mix -
voice and music together - by `DUCK_UNDER_VOICE` (15%, which is -1.41 dB), not just
the bed. Because it scales both, it cannot change the ratio the listener hears; it
lowers the mix under the word.

So the number to watch is the render's own balance, measured here from the separated
stems: the voice against the music in the frames where the voice is sounding.

    <service-venv-python> tools/check_mix_balance.py [stems-root]
    <service-venv-python> tools/check_mix_balance.py [stems-root] --render <song.wav>

`stems-root` defaults to build/stems/htdemucs, one folder per render holding
`no_vocals.wav` and `vocals.wav` (the demucs command is in the service README). With
`--render` the mix is rebuilt through the service's own `_mix_separated` and compared
with `render * (1 - duck * gate)`, which is what that function must produce when the
mapping is the identity: any lead gain, or any rebuild from the stems instead of the
render, shows up as a mismatch and a nonzero exit.

Measured on the twelve renders in build/stems/htdemucs: the voice sits -5.9 to +1.7 dB
over the music under a word (mean -0.4), the mix is 1.41 dB lower there (the duck), and
the rebuild matches the render times the duck to 3.1e-05.
"""
import argparse
import contextlib
import glob
import importlib.util
import io
import os
import sys
import threading
from fractions import Fraction

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP = os.path.join(ROOT, 'song_service', 'app.py')
DEFAULT_STEMS = os.path.join(ROOT, 'build', 'stems', 'htdemucs')


def load_app():
    """The service module, for its constants and its own mix function.

    Importing it starts the lyric helper, which without the `lyrics` module on the
    path prints an ImportError from a background thread. That has nothing to do with
    the mix, so it is kept out of the table.
    """
    spec = importlib.util.spec_from_file_location('song_app', APP)
    module = importlib.util.module_from_spec(spec)
    quiet = io.StringIO()
    with contextlib.redirect_stderr(quiet):
        spec.loader.exec_module(module)
        for thread in threading.enumerate():
            if thread is not threading.main_thread() and thread.daemon:
                thread.join(2.0)
    return module


def rms(x):
    return float(np.sqrt(np.mean(np.square(x))) or 0.0)


def db(ratio):
    return 20 * np.log10(max(float(ratio), 1e-12))


def gate_for(voice, rate, app):
    """The service's presence gate: a 50 ms envelope of the voice, normalised so a
    word passes and a gap does not. `voice` is (samples, channels)."""
    level = np.sqrt(np.mean(np.square(voice), axis=1))
    win = max(1, int(rate * 0.05))
    kernel = np.ones(win) / win
    hit = np.convolve(level, kernel, mode='same')
    return np.clip(hit / (float(hit.max()) * 0.12 + 1e-9), 0.0, 1.0)


def measure(bed, voice, rate, app):
    """The balance under a word, from the two stems."""
    gate = gate_for(voice, rate, app)
    active = gate > 0.5
    if not np.any(active):
        return {'active': active, 'gate': gate, 'over': 0.0, 'duck': 0.0}
    voice_rms = rms(voice[active])
    music_rms = rms(bed[active])
    duck = float(np.median(20 * np.log10(
        1.0 - app.DUCK_UNDER_VOICE * gate[active] + 1e-12)))
    return {'active': active, 'gate': gate,
            'over': db(voice_rms / max(music_rms, 1e-12)), 'duck': duck}


def verify(bed_path, vocal_path, render_path, app, work):
    """Rebuild the mix with the service and compare with render * (1 - duck * gate)."""
    out_path = os.path.join(work, 'service.wav')
    with contextlib.redirect_stdout(io.StringIO()):
        app._mix_separated(bed_path, vocal_path, out_path, (0.0, 0.0, 0.0),
                           render_path=render_path)
    mixed, rate = sf.read(out_path, always_2d=True, dtype='float32')
    bed, stem_rate = sf.read(bed_path, always_2d=True, dtype='float32')
    voice, _ = sf.read(vocal_path, always_2d=True, dtype='float32')
    render, render_rate = sf.read(render_path, always_2d=True, dtype='float32')
    if render_rate != stem_rate:
        ratio = Fraction(int(stem_rate), int(render_rate)).limit_denominator(1000)
        render = resample_poly(render, ratio.numerator, ratio.denominator, axis=0)
    frames = min(len(render), len(bed), len(voice), len(mixed))
    gate = gate_for(voice[:frames], stem_rate, app)
    expected = render[:frames] * (1.0 - app.DUCK_UNDER_VOICE * gate)[:, None]
    # `_mix_separated` holds the mix under 0.98 with one peak trim; it is a scale on
    # everything, so it is put on the expected signal too.
    peak = float(np.max(np.abs(expected)) or 0.0)
    if peak > 0.98:
        expected *= 0.98 / peak
    os.unlink(out_path)
    return float(np.max(np.abs(mixed[:frames] - expected)))


def main(argv=None):
    parser = argparse.ArgumentParser(description='The voice-over-music balance of the render-based mix.')
    parser.add_argument('stems', nargs='?', default=DEFAULT_STEMS,
                        help='folder of htdemucs/<song>/{no_vocals,vocals}.wav (default: %(default)s)')
    parser.add_argument('--render', default=None,
                        help='a render (song.wav) whose stems are in <stems>/<folder>; rebuilds and checks the mix')
    parser.add_argument('--stems-dir', default=None,
                        help='the one stems folder to check with --render (default: the first found)')
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

    print('%d songs: render-based mix, no lead gain, DUCK_UNDER_VOICE %.0f%% (%.2f dB) [%s]'
          % (len(pairs), app.DUCK_UNDER_VOICE * 100,
             db(1.0 - app.DUCK_UNDER_VOICE), os.path.relpath(APP, ROOT).replace(os.sep, '/')))
    print()
    print('%-40s %8s %9s %9s %9s' % ('render', 'sounding', 'voice/music', 'duck there', 'lead gain'))
    print('-' * 80)
    rows = []
    for folder, backing, vocal in pairs:
        bed, rate = sf.read(backing, always_2d=True, dtype='float32')
        voice, _ = sf.read(vocal, always_2d=True, dtype='float32')
        frames = min(len(bed), len(voice))
        result = measure(bed[:frames], voice[:frames], rate, app)
        rows.append((os.path.basename(folder), 100.0 * result['active'].mean(),
                     result['over'], result['duck']))
        print('%-40s %7.0f%% %8.1f %8.2f %8.1f'
              % (rows[-1][0][:40], rows[-1][1], rows[-1][2], rows[-1][3], 0.0))

    over = [row[2] for row in rows]
    lowest = min(rows, key=lambda row: row[2])
    highest = max(rows, key=lambda row: row[2])
    print()
    print('voice over the music under a word: %.1f-%.1f dB, mean %.1f over %d songs'
          % (min(over), max(over), sum(over) / len(over), len(rows)))
    print('  lowest  %s (%.1f dB)' % (lowest[0][:40], lowest[2]))
    print('  highest %s (%.1f dB)' % (highest[0][:40], highest[2]))
    print('the mix under a word is %.2f dB lower (the duck on the whole mix, not the bed)'
          % float(np.median([row[3] for row in rows])))
    print('lead gain: 0.0 dB on this path - the render already carries its own balance')

    status = 0
    if args.render:
        folder = args.stems_dir or pairs[0][0]
        backing = os.path.join(folder, 'no_vocals.wav')
        vocal = os.path.join(folder, 'vocals.wav')
        import tempfile
        work = tempfile.mkdtemp(prefix='mixbalance-')
        try:
            diff = verify(backing, vocal, args.render, app, work)
        finally:
            os.rmdir(work)
        print()
        print('rebuild through _mix_separated against render * (1 - duck * gate): '
              'worst difference %.1e' % diff)
        if diff > 1e-3:
            print("the service's mix is not the render plus the mapping's difference: "
                  'a lead gain, or a rebuild from the stems, has come back.')
            status = 1
    return status


if __name__ == '__main__':
    sys.exit(main())
