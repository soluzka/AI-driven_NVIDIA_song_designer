"""Local song service: ACE-Step, running on this machine's GPU.

The public page is static and cannot host a 3.5B model, so the model lives
here. The service loads it once, takes one job at a time (a single GPU cannot
usefully run two), and reports progress while it works.

    GET  /health                 -> {"ok": true, "model": ..., "device": ...}
    POST /api/song   {prompt, lyrics, seconds, steps}  -> 202 {"token", "status"}
    GET  /api/song/<token>       -> {"status", "elapsed", "progress", "error"}
    GET  /api/song/latest        -> the newest job, so a reloaded page can join it
    GET  /song_file/<token>      -> the rendered wav, kept for the panel's links
    GET  /last_song              -> logs/last_song.wav, whatever was rendered last

Nothing here is downloaded from a third party at request time: the checkpoint
sits in ACE_CHECKPOINT and the weights were fetched once during setup.
"""

import atexit
import gc
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import urllib.request
import uuid

from flask import Flask, jsonify, request, send_file, stream_with_context
from flask_cors import CORS

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _langid_compat():
    """Keep ACE-Step's segmenter working on a newer py3langid than we pin.

    acestep/language_segmentation/LangSegment.py calls
    LanguageIdentifier.from_pickled_model, and py3langid renamed that to
    from_model_file in 0.4. requirements.txt pins 0.2.2 so a fresh install is fine,
    but a machine that already had a newer one - an older install, a Colab session
    that resolved before the pin - failed every song with
    "type object 'LanguageIdentifier' has no attribute 'from_pickled_model'"
    instead of rendering. The new name is bound to the old one, which is all the
    segmenter needs.
    """
    try:
        from py3langid.langid import LanguageIdentifier
    except Exception:
        return
    if hasattr(LanguageIdentifier, 'from_pickled_model'):
        return
    for name in ('from_model_file', 'from_modelpath'):
        if hasattr(LanguageIdentifier, name):
            LanguageIdentifier.from_pickled_model = staticmethod(
                getattr(LanguageIdentifier, name))
            return


_langid_compat()

# Who owns this port, in a file any starter can read. Importing torch takes about
# forty seconds before /health answers, and a starter that treats "no answer" as
# "not running" starts a second copy that then fights this one for the card - one
# launcher and the desktop app were doing exactly that. The pid is written here,
# before the slow imports, so the window is covered from the first second.
PIDFILE = os.path.join(HERE, 'logs', 'service.pid')


def _write_pidfile():
    try:
        os.makedirs(os.path.dirname(PIDFILE), exist_ok=True)
        with open(PIDFILE, 'w', encoding='utf-8') as handle:
            handle.write(str(os.getpid()))
    except OSError:
        pass


def _clear_pidfile():
    try:
        with open(PIDFILE, encoding='utf-8') as handle:
            if handle.read().strip() != str(os.getpid()):
                return  # another copy owns the file now; leave it alone
        os.remove(PIDFILE)
    except (OSError, ValueError):
        pass


_write_pidfile()
atexit.register(_clear_pidfile)

# The vendored ACE-Step checkout and its weights.
ACE_REPO = os.environ.get('ACE_REPO', os.path.join(ROOT, 'third_party', 'ACE-Step'))
CHECKPOINT = os.environ.get('ACE_CHECKPOINT', os.path.join(HERE, 'models', 'ACE-Step-v1-3.5B'))

if ACE_REPO not in sys.path:
    sys.path.insert(0, ACE_REPO)

app = Flask(__name__)

# The page is served from another origin, so every route answers CORS.
CORS(app, resources={r"/*": {
    "origins": "*",
    "methods": ["GET", "POST", "OPTIONS"],
    "allow_headers": ["Content-Type"],
    "expose_headers": ["Content-Disposition"],
    "max_age": 86400,
}})

@app.after_request
def _allow_private_network(response):
    # The deployed page is served over https and calls this service on
    # http://127.0.0.1:8090, so the browser asks before a public page may reach a
    # loopback address. Answering the preflight keeps that request from being
    # blocked, which is what makes the model work without a tunnel.
    if request.headers.get('Access-Control-Request-Private-Network'):
        response.headers['Access-Control-Allow-Private-Network'] = 'true'
    return response


# 8 GB of VRAM cannot hold the whole 3.5B model alongside the decoder, so the
# weights are paged in and out instead of failing with an out-of-memory error.
CPU_OFFLOAD = os.environ.get('ACE_CPU_OFFLOAD', '1') != '0'
DTYPE = os.environ.get('ACE_DTYPE', 'bfloat16')


def _effective_dtype():
    """The dtype this card can actually run, which is not always the one asked for.

    bfloat16 needs Ampere or newer (compute 8.0+). A T4 is 7.5: the model loads there
    and then the first operator without a bf16 kernel fails with "GET was unable to
    find an engine to execute this computation" after minutes of work. float16 is the
    half precision every CUDA card has, so that is what a pre-Ampere card gets.
    """
    if DTYPE != 'bfloat16':
        return DTYPE
    try:
        import torch

        if not torch.cuda.is_available():
            return DTYPE
        major, _minor = torch.cuda.get_device_capability(0)
    except Exception:  # noqa: BLE001 - no torch, or no card: nothing to choose
        return DTYPE
    return DTYPE if major >= 8 else 'float16'
DEFAULT_STEPS = int(os.environ.get('ACE_STEPS', '30'))
# Three minutes: the page asks for 90-180 s depending on the seed, and a cap of
# 120 quietly truncated every longer request back to two.
MAX_SECONDS = float(os.environ.get('ACE_MAX_SECONDS', '180'))
# How long /api/lyrics holds the request open before handing back a token. A warm
# write lands in it; a cold one does not, and the tunnel would cut it at 100s.
LYRIC_INLINE_SECONDS = float(os.environ.get('LYRICS_INLINE_SECONDS', '40'))

_pipeline = None
# When the warm started, and why it failed if it did: /health reports both, so a
# page can show "2m 10s so far" rather than a bare loaded: false.
_warm_started = None
_warm_failed = None
# True only while the warm thread is inside the load. "Started and not loaded"
# was the old test, and it stays true for ever the moment anything releases the
# model again - the lyric warm-up does exactly that - so health reported
# "warming, 50 minutes" on a service that had finished loading long ago.
_warm_active = False
_pipeline_lock = threading.Lock()
_gpu_lock = threading.Lock()      # one generation at a time
_jobs = {}
_jobs_lock = threading.Lock()
_lyric_jobs = {}
_lyric_jobs_lock = threading.Lock()
_latest_token = None   # the song the page asked for most recently


_lyricist_instance = None
_lyricist_lock = threading.Lock()


def _release_pipeline():
    """Drop the song model so the card is free for the lyric model.

    ACE-Step holds about 7.9 GB of the 8 GB card and Qwen needs 3 GB, so the two
    cannot sit there together: whoever goes second pages, and paging cost a
    minute on the lyrics and doubled the render to 23 s a step. Every compose
    starts with the lyrics, so the lyric model takes the card and the song model
    is loaded again afterwards - about 40 seconds, which is cheaper than the
    paging it replaces and much cheaper than six megabytes of Qwen per second.
    """
    global _pipeline
    with _pipeline_lock:
        instance = _pipeline
        _pipeline = None
    if instance is not None:
        del instance
        import gc

        gc.collect()
        try:
            import torch

            torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass
        print('released the song model to free VRAM', flush=True)


def _lyricist():
    """One language model for the process, loaded on first use.

    On a GPU it is loaded only after the song model has been released, so it
    arrives on an empty card in about ten seconds instead of paging into the few
    hundred megabytes ACE-Step leaves behind. LYRICS_DEVICE=cpu writes the words
    in system RAM instead, which costs more time but never touches the card.
    """
    global _lyricist_instance
    with _lyricist_lock:
        if _lyricist_instance is None:
            from lyrics import Lyricist, lyrics_model_id, load_manifest

            device = os.environ.get('LYRICS_DEVICE')
            if not device:
                try:
                    import torch

                    device = 'cuda' if torch.cuda.is_available() else 'cpu'
                except Exception:  # noqa: BLE001
                    device = 'cpu'
            if device.startswith('cuda') and _tight_vram():
                _release_pipeline()
            _lyricist_instance = Lyricist(lyrics_model_id(load_manifest()), device=device)
        return _lyricist_instance


def _tight_vram(need_gb=12.0):
    """Whether this card cannot hold the lyric model beside the song model.

    ACE-Step takes about 8 GB and Qwen about 3 GB, so a free T4 keeps both and
    serves its first song without a reload. An 8 GB desktop card cannot: there,
    the song model is dropped for the lyricist, which is what keeps every step
    from paging. Releasing it unconditionally made a 16 GB card unload the model
    it had just warmed, so the first song paid for the load anyway.
    """
    return (_gpu().get('vram_gb') or 0) < need_gb


def _load_pipeline():
    """Load the model once; the first call takes a minute or two."""
    global _pipeline
    with _pipeline_lock:
        if _pipeline is not None:
            return _pipeline
        if not os.path.isdir(CHECKPOINT):
            raise RuntimeError(f'Checkpoint not found: {CHECKPOINT}')
        # transformers first, under the shared lock: the lyric warm-up runs in
        # another thread at the same moment, and whichever arrives second used to
        # see a module with no attributes yet - which is what made this fail with
        # "cannot import name 'CLIPTextModelWithProjection'" and left health
        # saying loaded: false after every start.
        from model_lock import import_transformers

        import_transformers()
        from acestep.pipeline_ace_step import ACEStepPipeline
        _pipeline = ACEStepPipeline(
            checkpoint_dir=CHECKPOINT,
            dtype=_effective_dtype(),
            torch_compile=False,
            cpu_offload=CPU_OFFLOAD,
        )
        return _pipeline


VOICE_SERVICE = os.environ.get('VOICE_SERVICE', 'http://127.0.0.1:8095')

# Where the video downloader listens. 8080 is what the Windows install uses, but a
# host can already have something there - Colab's own node process listens on 8080 -
# so a pooled notebook passes the port it managed to bind instead.
DOWNLOAD_PORT = int(os.environ.get('DOWNLOAD_PORT', '8080'))


def _measure_pitch(path):
    """The median pitch of a wav: what the recording measures, and where the model's
    vocal sits. Neither is a source of audio; the numbers are the whole point.
    """
    try:
        import librosa
        import numpy as np
        import soundfile as sf

        audio, rate = sf.read(path, always_2d=True)
        mono = audio.mean(axis=1).astype('float32')
        mono = librosa.resample(mono, orig_sr=rate, target_sr=16000)
        f0 = librosa.pyin(mono[:16000 * 12], fmin=70, fmax=600, sr=16000)[0]
        median = float(np.nanmedian(f0)) if np.isfinite(f0).any() else 0.0
        return {'f0_hz': round(median, 1), 'seconds': round(len(mono) / 16000.0, 2)}
    except Exception as exc:  # noqa: BLE001 - a measurement must not fail a song
        print(f'could not measure {path}: {exc}', flush=True)
        return {'f0_hz': 0.0, 'seconds': 0.0}


def _separate_song(song_path, work_dir):
    """Split a finished song into the model's vocal and the backing under it.

    The mapping has to touch the words and nothing else. Shifting or toning the
    mix would move the instruments with it, so the vocal is separated first and
    put back after it has been mapped.
    """
    import subprocess
    import sys

    out_dir = os.path.join(work_dir, 'stems')
    os.makedirs(out_dir, exist_ok=True)
    command = [sys.executable, '-m', 'demucs', '--two-stems=vocals', '-n', 'htdemucs',
               '--out', out_dir, song_path]
    print('separating: ' + ' '.join(command), flush=True)
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError('demucs could not separate the song: '
                           + (result.stderr or result.stdout or '')[-300:])
    stem = os.path.splitext(os.path.basename(song_path))[0]
    folder = os.path.join(out_dir, 'htdemucs', stem)
    vocal = os.path.join(folder, 'vocals.wav')
    backing = os.path.join(folder, 'no_vocals.wav')
    if not (os.path.exists(vocal) and os.path.exists(backing)):
        raise RuntimeError('demucs wrote no stems for this song')
    return vocal, backing


def _apply_room(mono, rate, wet):
    """A short synthetic room, so the vocal sits where the mapped phrase sits."""
    import numpy as np
    from scipy.signal import fftconvolve

    length = max(8, int(rate * 0.45))
    rng = np.random.default_rng(7)
    tail = rng.standard_normal(length).astype('float32')
    tail *= np.exp(-np.linspace(0.0, 5.0, length)).astype('float32')
    tail[0] = 1.0
    wet_signal = fftconvolve(mono, tail, mode='full')[:len(mono)].astype('float32')
    dry_rms = float(np.sqrt(np.mean(mono ** 2)) or 0.0)
    wet_rms = float(np.sqrt(np.mean(wet_signal ** 2)) or 0.0)
    if wet_rms > 1e-9 and dry_rms > 1e-9:
        wet_signal *= dry_rms / wet_rms
    return ((1.0 - wet) * mono + wet * wet_signal).astype('float32')


# Envelope correction for the transpose: how long a cepstral lifter separates
# formants from harmonics (about 2 ms), and how far the correction may push any
# one band, so a noisy frame cannot turn into a whistle or a hole.
FORMANT_LIFTER_MS = 2.0
FORMANT_GAIN_LIMIT_DB = 18.0


def _spectral_envelope(magnitude, rate):
    """The spectral envelope of every frame: the log spectrum, cepstrally
    smoothed at FORMANT_LIFTER_MS. That is where the formants live; the harmonics
    riding on it are what a pitch shift is supposed to move."""
    import numpy as np

    cepstrum = np.fft.irfft(np.log(magnitude + 1e-10), axis=0)
    keep = max(2, int(FORMANT_LIFTER_MS / 1000.0 * rate))
    lifter = np.zeros(cepstrum.shape[0])
    lifter[:keep] = 1.0
    lifter[-keep:] = 1.0
    return np.exp(np.fft.rfft(cepstrum * lifter[:, None], axis=0).real)


def _shift_pitch_keeping_formants(mono, rate, semitones):
    """Transpose the voice while its formants stay where the singer left them.

    A bare phase-vocoder shift moves the spectral envelope along with the
    harmonics: +3 semitones lifts F1 by 20%, which reads as a thinner, shifted
    person rather than the same person singing higher. Two things are needed, and
    they are not the same job:

    - the pitch has to move, and the phase vocoder does that well while keeping
      the texture of a real recording;
    - the envelope must not move, so after the shift the source's own envelope is
      divided back over the result as a smooth per-frame gain curve.

    A full vocoder resynthesis (WORLD) also holds the formants, but it rebuilds
    the signal from scratch and measured +281% spectral flatness against the
    source on a separated pop vocal - heard as a grain or grind. Correcting the
    vocoder output's own phase instead measured 23% *below* the source's flatness,
    with the formants inside 3%. So this is the version that runs.

    Returns None when librosa cannot do it, so the caller can leave the voice be.
    """
    import numpy as np

    try:
        import librosa

        n_fft, hop = 2048, 512
        shifted = librosa.effects.pitch_shift(mono, sr=int(rate), n_steps=float(semitones))
        source = librosa.stft(mono, n_fft=n_fft, hop_length=hop)
        target = librosa.stft(shifted, n_fft=n_fft, hop_length=hop)
        frames = min(source.shape[1], target.shape[1])
        source, target = source[:, :frames], target[:, :frames]
        gain = _spectral_envelope(np.abs(source), rate) / (
            _spectral_envelope(np.abs(target), rate) + 1e-10)
        limit = 10.0 ** (FORMANT_GAIN_LIMIT_DB / 20.0)
        out = librosa.istft(target * np.clip(gain, 1.0 / limit, limit),
                            hop_length=hop, length=len(mono))
        print('formants held: %d frames corrected to the source envelope, pitch x%.4f'
              % (frames, 2.0 ** (float(semitones) / 12.0)), flush=True)
        return np.ascontiguousarray(out, dtype='float32')
    except Exception as exc:  # noqa: BLE001 - reported, then the caller decides
        print(f'could not transpose the vocal: {exc}', flush=True)
        return None


def _map_vocal(mono, rate, mapping):
    """Put the sung vocal through the same map as the fitted phrase.

    The page pitches the recorded phrase into the song's key and tones it; that
    map used to stop at the reference, so the words the model sang arrived in no
    relation to the music. `mapping` is (semitones, low-pass Hz, room) and is
    applied to the vocal itself: transposing it keeps its length, so it still
    lands on the backing's grid.

    The transpose keeps the formants; see `_shift_pitch_keeping_formants`. If
    WORLD is unavailable the librosa shift is used instead, which moves them, so
    the run says which one happened rather than leaving it to be guessed.
    """
    import numpy as np

    semitones, lowpass, room = mapping or (0.0, 0.0, 0.0)
    out = np.ascontiguousarray(mono, dtype='float32')
    if abs(semitones) >= 0.05:
        shifted = _shift_pitch_keeping_formants(out, rate, semitones)
        if shifted is not None:
            out = shifted
        else:
            import librosa

            try:
                out = librosa.effects.pitch_shift(out, sr=rate, n_steps=float(semitones))
                print('formants not held: fell back to the phase vocoder', flush=True)
            except Exception as exc:  # noqa: BLE001 - a shift that fails keeps the voice
                print(f'pitch shift by {semitones} semitones failed: {exc}', flush=True)
    if lowpass and lowpass < rate / 2.0:
        from scipy.signal import butter, sosfilt

        sos = butter(2, float(lowpass) / (rate / 2.0), btype='low', output='sos')
        out = sosfilt(sos, out).astype('float32')
    if room and room > 0.01:
        out = _apply_room(out, rate, min(0.9, float(room)))
    peak = float(np.max(np.abs(out)) or 0.0)
    if peak > 0.99:
        out *= 0.99 / peak
    print(f'mapped the vocal: {semitones:+.2f} semitones, low-pass {lowpass:.0f} Hz, '
          f'room {room:.2f}', flush=True)
    return out


# How far above the bed the voice is placed, how far the bed dips under it, and
# the most gain that will ever be applied to a quiet model output.
#
# The offset is measured where the voice is actually singing, not across the whole
# track: a sung line covers about a third of a song, so matching whole-track RMS
# compared a sparse signal with a continuous one and put the voice +13.9 dB over
# the music (measured on a 180 s song) while claiming 2.5. That is what "too loud"
# was. Matching the active frames gives the number the ear hears.
# 0 dB and a light duck, not 1.5 dB over a bed cut by 45%: the two together put the
# voice 8.3 dB over the music under every word (measured on the mix this service
# makes), which is a lyric you cannot hear the song behind. At 0 dB and 20% it is
# about 3.5 dB - the voice is still the lead, and the music is still there.
LEAD_DB = 0.0
DUCK_UNDER_VOICE = 0.15
MAX_LEAD_GAIN = 8.0


def _layout_vocal(song_path, vocal_bytes, out_path, bpm, mapping=(0.0, 0.0, 0.0)):
    """Lay the sung lyrics over the instrumental and keep them the lead."""
    import numpy as np
    import soundfile as sf

    song, rate = sf.read(song_path, always_2d=True)
    vocal, vocal_rate = sf.read(io.BytesIO(vocal_bytes), always_2d=True)
    mono = vocal.mean(axis=1).astype('float32')
    if vocal_rate != rate and len(mono):
        mono = np.interp(np.linspace(0, len(mono) - 1, int(len(mono) * rate / vocal_rate)),
                         np.arange(len(mono)), mono).astype('float32')
    if not len(mono):
        raise RuntimeError('the voice service returned an empty vocal')
    mono = _map_vocal(mono, rate, mapping)

    channels = song.shape[1]
    out = song.copy()
    start = int(len(out) * 0.04)
    room = max(0, int(len(out) * 0.94) - start)
    if len(mono) > room:
        mono = mono[:room]
        # A hard stop mid-word is audible; fade the tail instead.
        fade = min(len(mono), int(rate * 0.4))
        mono[-fade:] *= np.linspace(1, 0, fade).astype('float32')

    placed = mono
    # The track steps back under the words so the singing is what is heard, and
    # comes back up wherever the phrase has a gap.
    win = max(1, int(rate * 0.05))
    hit = np.convolve(np.abs(placed), np.ones(win) / win, mode='same')
    gate = np.clip(hit / (float(hit.max()) * 0.12 + 1e-9), 0.0, 1.0)
    envelope = 1.0 - DUCK_UNDER_VOICE * gate

    # A fixed gain cannot make a lead vocal: measured on a finished song, the
    # voice sat 3.2 dB *below* the bed, which is why it sounded like part of the
    # backing rather than something sung over it. So the voice is matched to the
    # track it is about to sit on, and put LEAD_DB above it, whatever the model
    # returned.
    bed = out[start:start + len(placed)] * envelope[:, None]
    bed_rms = float(np.sqrt(np.mean(bed ** 2)) or 0.0)
    voice_rms = float(np.sqrt(np.mean(placed ** 2)) or 0.0)
    lead_gain = 1.0
    if bed_rms > 1e-6 and voice_rms > 1e-6:
        lead_gain = min(MAX_LEAD_GAIN, (bed_rms * 10 ** (LEAD_DB / 20.0)) / voice_rms)
    out[start:start + len(placed)] = bed + (placed * lead_gain)[:, None].repeat(channels, axis=1)

    peak = float(np.max(np.abs(out)) or 0.0)
    if peak > 0.98:
        out *= 0.98 / peak
    sf.write(out_path, out, rate)
    return {'seconds': round(len(out) / float(rate), 2),
            'vocal_seconds': round(len(placed) / float(rate), 2),
            'starts_at': round(100.0 * start / max(1, len(out)), 1),
            'lead_db': round(20 * float(np.log10(lead_gain)), 1)}


def _place_phrases(out, mono, envelope, rate, fade, lead, duck, first_start, bar_seconds):
    """Add the recorded phrase on bar lines without stacking it on itself.

    Copies were placed one bar apart whatever the phrase's length, so a ten second phrase
    over a two second bar overlapped itself four times: identical audio offset by a bar
    combs against itself, and the duck compounded to -32 dB, a hole rather than the track
    stepping back. Each copy now starts on the first bar line after the previous one ends,
    and the duck is applied once. Returns (out, placed) - placed as a percentage of the
    track, which is what the endpoint prints.
    """
    import numpy as np

    levels = [0.95, 0.8, 0.9, 0.72] if lead else [0.62, 0.45, 0.55, 0.38]
    pans = [0.5, 0.5, 0.5, 0.5] if lead else [0.35, 0.75, 0.6, 0.3]
    bar_samples = max(1, int(bar_seconds * rate))

    placements = []
    cursor = int(first_start)
    for level, pan in zip(levels, pans):
        if len(out) - cursor <= fade * 2:
            break
        placements.append((cursor, level, pan))
        cursor += len(mono)
        cursor = ((cursor + bar_samples - 1) // bar_samples) * bar_samples

    bed = np.ones(len(out), dtype='float32')
    for start, _level, _pan in placements:
        bed[start:min(len(out), start + len(mono))] = np.minimum(
            bed[start:min(len(out), start + len(mono))], duck)
    out *= bed[:, None]

    channels = out.shape[1]
    placed = []
    for start, level, pan in placements:
        part = mono[:len(out) - start]
        env = envelope[:len(part)]
        spread = np.stack([
            (part * env) * level * (1.0 - pan),
            (part * env) * level * pan if channels > 1 else (part * env) * level * (1.0 - pan),
        ][:channels], axis=1)
        out[start:start + len(part)] += spread
        placed.append(round(100 * start / max(1, len(out))))
    return out, placed


def _mix_separated(backing_path, vocal_path, out_path, mapping=(0.0, 0.0, 0.0)):
    """Map the model's own vocal and lay it back over the backing it was sung with."""
    import numpy as np
    import soundfile as sf

    bed, rate = sf.read(backing_path, always_2d=True)
    vocal, vocal_rate = sf.read(vocal_path, always_2d=True)
    mono = vocal.mean(axis=1).astype('float32')
    if vocal_rate != rate and len(mono):
        mono = np.interp(np.linspace(0, len(mono) - 1, int(len(mono) * rate / vocal_rate)),
                         np.arange(len(mono)), mono).astype('float32')
    if not len(mono) or float(np.max(np.abs(mono))) < 1e-4:
        raise RuntimeError('the song came back with no vocal to map')
    placed = _map_vocal(mono, rate, mapping)

    frames = min(len(placed), len(bed))
    bed = bed[:frames].copy()
    placed = placed[:frames]
    win = max(1, int(rate * 0.05))
    hit = np.convolve(np.abs(placed), np.ones(win) / win, mode='same')
    gate = np.clip(hit / (float(hit.max()) * 0.12 + 1e-9), 0.0, 1.0)
    bed *= (1.0 - DUCK_UNDER_VOICE * gate)[:, None]

    # Both levels are measured over the frames where the voice is present. Over
    # the whole track the voice is silent for most of it, its RMS then reads far
    # below what the ear hears, and the gain that "matches" it overshoots by more
    # than 10 dB - which is how the vocal ended up on top of the song.
    active = gate > 0.5
    if bool(np.any(active)) and float(np.sqrt(np.mean(placed[active] ** 2)) or 0.0) > 1e-6:
        bed_rms = float(np.sqrt(np.mean(bed[active] ** 2)) or 0.0)
        voice_rms = float(np.sqrt(np.mean(placed[active] ** 2)) or 0.0)
    else:
        bed_rms = float(np.sqrt(np.mean(bed ** 2)) or 0.0)
        voice_rms = float(np.sqrt(np.mean(placed ** 2)) or 0.0)
    lead_gain = 1.0
    if bed_rms > 1e-6 and voice_rms > 1e-6:
        lead_gain = min(MAX_LEAD_GAIN, (bed_rms * 10 ** (LEAD_DB / 20.0)) / voice_rms)
    mixed = bed + (placed * lead_gain)[:, None].repeat(bed.shape[1], axis=1)
    peak = float(np.max(np.abs(mixed)) or 0.0)
    if peak > 0.98:
        mixed *= 0.98 / peak
    sf.write(out_path, mixed, rate)
    return {'seconds': round(len(mixed) / float(rate), 2),
            'vocal_seconds': round(len(placed) / float(rate), 2),
            'lead_db': round(20 * float(np.log10(lead_gain)), 1)}


def _run_voice_job(token, reference_path, prompt, lyrics, seconds, steps, seed,
                   mapping=(0.0, 0.0, 0.0)):
    """The model sings the lyrics; the recording is measured, never sung."""
    started = time.time()
    try:
        if not lyrics:
            _set(token, status='writing', progress=0.02)
            from lyrics import lyrics_model_id, load_manifest

            # Writing is the first long silence: a cold 3.5B model spends a
            # minute there, so the page is told the token count as it climbs.
            song = _lyricist().write_song(
                prompt or 'electronic music', '', seed,
                progress=lambda done: _set(token, progress=round(0.02 + 0.28 * done, 3)))
            lyrics = '\n'.join(song.get('lines') or [])
            _set(token, prompt=prompt, lyrics=lyrics, progress=0.3)
        if not lyrics.strip():
            raise RuntimeError('the lyric model wrote nothing to sing')

        measured = _measure_pitch(reference_path)
        print('the recording measures %(f0_hz)s Hz over %(seconds)s s' % measured, flush=True)
        _set(token, reference=measured)

        # The model sings the lyrics itself, so the melody, timing and phrasing
        # belong to the song it is singing over. The recording is a measurement
        # (its pitch, and the tone the page derived from it) applied afterwards;
        # it is never the voice in the track.
        _run_job(token, prompt, lyrics, seconds, steps, seed,
                 span=(0.1, 0.78), complete_status='mixing')
        with _jobs_lock:
            job = dict(_jobs.get(token) or {})
        if job.get('status') not in ('mixing', 'complete'):
            raise RuntimeError(job.get('error') or 'the song did not render')

        _set(token, status='separating', progress=0.82)
        vocal_path, backing_path = _separate_song(job['path'], job['tmp_dir'])

        # The model sings its own melody over its own backing, so its vocal is already
        # in the song's key: it is not transposed. Two things were wrong with the
        # register match that used to run here. A fractional shift moves the words off
        # the music - one render came out at -5.5 semitones, and on that job's own
        # separated vocal the transpose put +7.4 dB into 60-120 Hz and took 5-6 dB out
        # of 250 Hz-3 kHz, which is the soft, muffled, low vocal that was reported. And
        # the pitch it matched on is not trustworthy: on that same stem pyin's median
        # was 70.0 Hz, exactly its search floor, with an autocorrelation of 46 Hz and a
        # 90th percentile of 233 Hz. A transpose built on that number is a detune, not
        # a register. The measurement is still reported, so the panel can say where the
        # two voices sat.
        sung = _measure_pitch(vocal_path)
        semitones = 0.0
        mapping = (semitones, mapping[1], mapping[2])
        print('pitch: recording %(r)s Hz, model vocal %(v)s Hz, no transpose (already in key)'
              % {'r': measured.get('f0_hz'), 'v': sung.get('f0_hz')}, flush=True)
        _set(token, map={'semitones': 0.0, 'reference_hz': measured.get('f0_hz'),
                         'vocal_hz': sung.get('f0_hz')})

        _set(token, status='mixing', progress=0.92)
        mixed_path = os.path.join(job['tmp_dir'], 'song_with_voice.wav')
        layout = _mix_separated(backing_path, vocal_path, mixed_path, mapping)
        layout['semitones'] = semitones
        _set(token, status='complete', progress=1.0, path=mixed_path,
             lyrics=lyrics, voice=layout, elapsed=round(time.time() - started, 1))
        _keep_song(token, mixed_path)
        print('song with the model singing the lyrics, mapped: %s' % (layout,), flush=True)
    except Exception as exc:  # noqa: BLE001 - the page shows the reason
        traceback.print_exc()
        _set(token, status='error', error=str(exc), elapsed=round(time.time() - started, 1))
    finally:
        try:
            os.remove(reference_path)
        except OSError:
            pass


def _prune_jobs():
    """Forget all but the newest finished jobs, with their temp audio.

    The page streams a finished song twice - once into the player and again
    from "Save the song" - so the file cannot be deleted on the first read.
    Keeping a couple of renders is a few megabytes; keeping every one is not.

    """
    with _jobs_lock:
        done = [token for token, job in _jobs.items()
                if job['status'] in ('complete', 'error') and token != _latest_token]
        for token in done[:-1]:
            job = _jobs.pop(token, None)
            if job:
                shutil.rmtree(job.get('tmp_dir') or '', ignore_errors=True)


KEPT_SONGS = 12


def _kept_song(token):
    """Where a finished song is kept so it outlives the job record."""
    return os.path.join(HERE, 'logs', f'song_{token}.wav')


def _keep_song(token, path):
    """Copy a finished song somewhere a restart cannot take it away.

    Jobs live in memory, so restarting the service - which the launcher and any
    code change both do - used to make "Save" answer "not finished" for a song
    that had played a second earlier. The record can be forgotten; the file on
    disk is what the page is actually asking for, so it is kept by token and
    "Save" still works after the restart.
    """
    try:
        kept = _kept_song(token)
        os.makedirs(os.path.dirname(kept), exist_ok=True)
        shutil.copyfile(path, kept)
        old = sorted((entry for entry in os.scandir(os.path.dirname(kept))
                      if entry.name.startswith('song_') and entry.name.endswith('.wav')),
                     key=lambda entry: entry.stat().st_mtime, reverse=True)
        for entry in old[KEPT_SONGS:]:
            try:
                os.remove(entry.path)
            except OSError:
                pass
        return kept
    except OSError as exc:
        print(f'could not keep a copy of {token}: {exc}', flush=True)
        return None


def _set(token, **fields):
    with _jobs_lock:
        job = _jobs.get(token)
        if job is not None:
            job.update(fields)


def _prompt_for(genres, words, bpm, key):
    """A tag prompt in the shape the model expects: genre, mood, instruments."""
    parts = [', '.join(genres[:3]) if genres else 'electronic']
    if bpm:
        parts.append(f'{bpm} bpm')
    if key:
        parts.append(key)
    if words:
        parts.append('samples of ' + ', '.join(words[:4]))
    parts.append('mastered, wide stereo, emotional vocal')
    return ', '.join(parts)


def _warm_lyrics():
    """Load the lyric model at startup, not on the first click.

    Loading takes tens of seconds. Doing it here means the page's first lyric
    request comes back quickly instead of spending that time on "Composing...".
    The model is released again before a song job loads ACE-Step, so the two
    still never share the 8 GB of VRAM.
    """
    try:
        _lyricist()
        print('lyric model ready', flush=True)
    except Exception:  # noqa: BLE001 - a failed warm-up must not stop the service
        traceback.print_exc()


threading.Thread(target=_warm_lyrics, daemon=True).start()


def _release_lyricist():
    """Give the lyric model's memory back before the 3.5B music model loads.

    It lives in system RAM now, not VRAM, but ACE-Step pages its 7.9 GB of
    weights through RAM too, so the 6 GB Qwen holds is still worth freeing
    before every render.
    """
    global _lyricist_instance
    with _lyricist_lock:
        instance = _lyricist_instance
        _lyricist_instance = None
    if instance is None:
        return
    try:
        instance._pipeline = None
    except Exception:  # noqa: BLE001 - best effort, it is being thrown away
        pass
    del instance
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001
        pass
    print('released the lyric model to free VRAM', flush=True)


# Where the sub-bass filter sits in _write_samples. A 4-pole corner at 32 Hz is the
# usual mastering one: below a low C, where the renders' rumble was and no instrument
# of theirs sits.
SUB_HP_HZ = 32.0


def _write_samples(target_wav, out_path, sample_rate):
    """Write a decoded waveform without depending on libsndfile.

    ACE-Step saves through torchaudio, and torchaudio 2.9 dropped its soundfile
    and sox backends: every save now goes through torchcodec, which is not
    installed, so generation ended in "TorchCodec is required for
    save_with_torchcodec" after the whole expensive diffusion had run. Writing
    the samples ourselves costs nothing and needs no extra package.
    """
    import numpy as np

    samples = target_wav
    if hasattr(samples, 'detach'):
        samples = samples.detach().float().cpu().numpy()
    if isinstance(samples, (list, tuple)):
        samples = samples[0] if samples else None
        if samples is None:
            raise RuntimeError('the model returned no waveform')
        if hasattr(samples, 'detach'):
            samples = samples.detach().float().cpu().numpy()
    samples = np.asarray(samples, dtype=np.float32).squeeze()
    if samples.ndim == 0 or samples.size == 0:
        raise RuntimeError('the model returned an empty waveform')
    print(f'waveform: shape={samples.shape} peak={float(np.abs(samples).max()):.3f}', flush=True)

    # The decoder hands over (channels, frames). Getting this wrong writes two
    # samples and produces a 48-byte "song" - silent, no vocal, which is exactly
    # what happened before.
    if samples.ndim == 1:
        frames = samples[:, None]
    elif samples.shape[0] <= 4 and samples.shape[1] > samples.shape[0]:
        frames = samples.T
    else:
        frames = samples
    channels = frames.shape[1]

    # Stdlib writer first: libsndfile in this environment refuses the file with
    # "Format not recognised" even when the format is given explicitly, and the
    # wave module cannot fail that way.
    import wave

    # The model hands back a thump under the bass: 20-40 Hz carried 6-32% of the power on
    # six renders, peaked at 34-40 Hz with no harmonics above them - a swell, not a note.
    # Four poles at 32 Hz take that share to 3-20% and leave 40-120 Hz where it was, so
    # kick and bass keep their weight and only the inaudible part goes. Filtered before
    # the normalisation, so the peak reference is what will be written.
    try:
        from scipy.signal import butter, sosfilt

        sos = butter(4, SUB_HP_HZ / (sample_rate / 2.0), btype='high', output='sos')
        frames = sosfilt(sos, frames, axis=0).astype('float32')
        print(f'sub-bass under {SUB_HP_HZ:.0f} Hz removed', flush=True)
    except Exception as exc:  # noqa: BLE001 - a missing scipy must not lose the render
        print(f'could not filter the sub-bass: {exc}', flush=True)

    # Every render is lifted to the same peak. The model hands back whatever
    # level it feels like - some songs peaked at 0.4 and sounded muffled next to
    # the loud browser band - so 0.95 (about -0.5 dBFS) is applied here instead
    # of only trimming the ones that clip.
    peak = float(np.abs(frames).max())
    scale = (0.95 / peak) if peak > 1e-6 else 1.0
    pcm = np.clip(frames * scale, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype('<i2')
    with wave.open(out_path, 'wb') as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(int(sample_rate))
        handle.writeframes(pcm.tobytes())
    print(f'wrote {out_path}: {channels} ch, {frames.shape[0]} frames', flush=True)
    return out_path

    # Explicit format: letting libsndfile infer it from the file name failed
    # inside this process with "Format not recognised", so it is stated here.
    
def _use_soundfile_writes(pipeline):
    """Replace the pipeline's torchaudio save with a soundfile save.

    The pipeline writes its wav inside __call__ and returns the paths it wrote,
    so the writing step itself has to be replaced - there is no flag for it.
    The signature and the returned path are kept, so the rest of __call__
    (including the input-params json written next to the audio) still works.
    """
    cls = type(pipeline)
    if getattr(cls, '_writes_with_soundfile', False):
        return pipeline

    def save_wav_file(self, target_wav, idx, save_path=None, sample_rate=48000, format='wav'):
        if save_path is None:
            base = os.path.join(os.getcwd(), 'outputs')
            os.makedirs(base, exist_ok=True)
            path = os.path.join(base, f'output_{time.strftime("%Y%m%d%H%M%S")}_{idx}.wav')
        elif os.path.isdir(save_path):
            path = os.path.join(save_path, f'output_{time.strftime("%Y%m%d%H%M%S")}_{idx}.wav')
        else:
            parent = os.path.dirname(save_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            path = save_path
        print(f'saving audio to {path} with soundfile', flush=True)
        return _write_samples(target_wav, path, sample_rate)

    cls.save_wav_file = save_wav_file
    cls._writes_with_soundfile = True
    print('ACE-Step will write wav files with soundfile (no torchcodec)', flush=True)
    return pipeline


def _count_render_steps(token, span):
    """Make the page's percentage the model's own step count.

    The pipeline has no progress callback: it goes from 'starting' to a finished
    wav minutes later, so the page used to sit at 10% for the whole render and
    read as a freeze. Its diffusion loop does count itself with tqdm, so that
    counter is replaced here with one that reports each step it finishes. `span`
    is the slice of the job's progress this render owns, so a job with a vocal
    staged ahead of it does not appear to go backwards.

    Returns the real tqdm, to be put back when the render ends.
    """
    from acestep import pipeline_ace_step as ace

    lo, hi = span
    original = ace.tqdm

    def counting(iterable, *args, **kwargs):
        total = kwargs.get('total') or 0
        for index, item in enumerate(iterable, 1):
            yield item
            if total:
                done = min(1.0, index / float(total))
                _set(token, progress=round(lo + (hi - lo) * done, 3))

    ace.tqdm = counting
    return original


def _stop_counting(original):
    from acestep import pipeline_ace_step as ace

    ace.tqdm = original


def _run_job(token, prompt, lyrics, seconds, steps, seed, span=(0.08, 0.95),
             complete_status='complete'):
    """Render a backing. `span` is the progress range it is allowed to fill.

    A sub-job (the backing under a sung vocal) passes a later span and
    `complete_status='mixing'`: the token is shared with the job that will mix
    the voice in, and marking it complete here made the page fetch the bare
    instrumental and call it finished.
    """
    tmp_dir = tempfile.mkdtemp(prefix='ace_')
    out_path = os.path.join(tmp_dir, 'song.wav')
    started = time.time()
    _set(token, status='loading', progress=span[0], tmp_dir=tmp_dir)
    try:
        _release_lyricist()
        pipeline = _use_soundfile_writes(_load_pipeline())
        _set(token, status='generating', progress=span[0])
        counter = _count_render_steps(token, span)
        try:
            with _gpu_lock:
                outputs = pipeline(
                    audio_duration=seconds,
                    prompt=prompt,
                    lyrics=lyrics,
                    infer_step=steps,
                    guidance_scale=15.0,
                    scheduler_type='euler',
                    cfg_type='apg',
                    omega_scale=10.0,
                    manual_seeds=[seed],
                    guidance_interval=0.5,
                    guidance_interval_decay=0.0,
                    min_guidance_scale=3.0,
                    use_erg_tag=True,
                    use_erg_lyric=True,
                    use_erg_diffusion=True,
                    oss_steps=[],
                    guidance_scale_text=0.0,
                    guidance_scale_lyric=0.0,
                    save_path=out_path,
                )
        finally:
            _stop_counting(counter)
        print(f'pipeline returned {len(outputs or [])} item(s)', flush=True)
        if not os.path.exists(out_path):
            # Newer/older checkouts name the file themselves; take whatever wav
            # the pipeline reported or wrote.
            for item in outputs or []:
                if isinstance(item, str) and item.endswith('.wav') and os.path.exists(item):
                    out_path = item
                    break
        if not os.path.exists(out_path):
            for name in os.listdir(tmp_dir):
                if name.endswith('.wav'):
                    out_path = os.path.join(tmp_dir, name)
                    break
        if not os.path.exists(out_path):
            raise RuntimeError('The model finished without writing a wav file')
        # Keep the last render next to the logs: the job's own copy is deleted as
        # soon as the page has streamed it, and without a copy there is no way to
        # check afterwards whether the voice is really in the audio.
        try:
            logs = os.path.join(HERE, 'logs')
            os.makedirs(logs, exist_ok=True)
            shutil.copyfile(out_path, os.path.join(logs, 'last_song.wav'))
            print('kept a copy at logs/last_song.wav', flush=True)
        except OSError as exc:
            print(f'could not keep a copy of the render: {exc}', flush=True)
        _set(token, status=complete_status,
             progress=1.0 if complete_status == 'complete' else span[1],
             path=out_path, elapsed=round(time.time() - started, 1))
        # Only the finished article is kept: a sub-job's instrumental is an
        # intermediate and would be saved by mistake.
        if complete_status == 'complete':
            _keep_song(token, out_path)
    except Exception as exc:  # noqa: BLE001 - reported to the page as text
        traceback.print_exc()
        _set(token, status='error', error=str(exc)[:400],
             elapsed=round(time.time() - started, 1))


@app.route('/pc/<path:rest>', methods=['GET', 'POST', 'HEAD', 'OPTIONS'])
def pc_proxy(rest):
    """The PC's downloader, carried on the one address the internet reaches.

    Playing a TikTok or a Dailymotion card means yt-dlp fetching the file on this
    machine, and that list only ever lives on loopback - a phone cannot reach it,
    so the player had nothing to fetch and every non-YouTube card failed there
    while working on the PC. This hands the request to the downloader unchanged
    and streams the answer back, so the same tunnel that carries songs carries the
    video too. Loops are avoided: the prefix is /pc and the downloader has no such
    route of its own.
    """
    if request.method == 'OPTIONS':
        return ('', 204)

    import urllib.error
    import urllib.request

    target = 'http://127.0.0.1:%d/' % DOWNLOAD_PORT + rest
    query = request.query_string.decode('latin-1')
    if query:
        target += '?' + query

    headers = {}
    if request.content_type:
        headers['Content-Type'] = request.content_type
    range_header = request.headers.get('Range')
    if range_header:
        headers['Range'] = range_header  # seeking in a playing video needs it
    data = None if request.method in ('GET', 'HEAD') else request.get_data()

    upstream = urllib.request.Request(target, data=data, headers=headers,
                                      method=request.method)
    try:
        answer = urllib.request.urlopen(upstream, timeout=900)
    except urllib.error.HTTPError as exc:
        return app.response_class(exc.read(), status=exc.code,
                                  mimetype=exc.headers.get('Content-Type', 'application/json'))
    except Exception as exc:  # noqa: BLE001 - the downloader being down is normal
        return jsonify({'error': 'The downloader on this PC is not answering: %s' % exc}), 503

    response = app.response_class(
        stream_with_context(iter(lambda: answer.read(65536), b'')),
        status=getattr(answer, 'status', 200),
        mimetype=answer.headers.get('Content-Type', 'application/octet-stream'))
    for header in ('Content-Length', 'Content-Range', 'Accept-Ranges', 'Content-Disposition'):
        value = answer.headers.get(header)
        if value:
            response.headers[header] = value
    return response


@app.route('/api/align', methods=['POST', 'OPTIONS'])
def api_align():
    """Where each lyric line is sung, measured from the song's own vocal.

    The page sends the audio it is about to store and the lines it will draw, and
    keeps the schedule that comes back with the song, so the card, the player and
    the recorded video all use one measured timing instead of a guess.
    """
    if request.method == 'OPTIONS':
        return ('', 204)
    body = request.get_json(silent=True) or {}
    raw_lines = request.form.get('lines', body.get('lines'))
    if isinstance(raw_lines, str):
        try:
            raw_lines = json.loads(raw_lines)
        except ValueError:
            raw_lines = [line for line in raw_lines.splitlines() if line.strip()]
    if not isinstance(raw_lines, list) or not raw_lines:
        return jsonify({'error': 'no lines to align'}), 400
    lines = [str(line).strip()[:200] for line in raw_lines if str(line).strip()][:120]
    if not lines:
        return jsonify({'error': 'no lines to align'}), 400

    work = tempfile.mkdtemp(prefix='align-request-')
    try:
        upload = request.files.get('audio')
        if upload is not None and upload.filename:
            suffix = os.path.splitext(upload.filename)[1] or '.wav'
            audio_path = os.path.join(work, 'song' + suffix)
            upload.save(audio_path)
        else:
            audio_path = str(body.get('path') or '')
            if not audio_path or not os.path.exists(audio_path):
                return jsonify({'error': 'send the audio file, or a path that exists'}), 400

        from align import align_lines

        started = time.time()
        result = align_lines(audio_path, lines, work_dir=os.path.join(work, 'align'))
        result['seconds_taken'] = round(time.time() - started, 1)
        print('aligned %d lines in %.1fs from %s'
              % (len(lines), result['seconds_taken'], result.get('source')), flush=True)
        return jsonify(result)
    except Exception as exc:  # noqa: BLE001 - report the reason to the page
        traceback.print_exc()
        return jsonify({'error': 'alignment failed: %s' % exc}), 500
    finally:
        shutil.rmtree(work, ignore_errors=True)


@app.route('/api/top', methods=['POST', 'OPTIONS'])
def api_top_proxy():
    """The most-watched listing, carried by the one address the internet can reach.

    yt-dlp has to run on this PC - YouTube refuses a datacentre address - and the
    downloader that runs it stays on loopback, where a phone cannot follow. This
    service already has the tunnel, so it hands the request over on loopback and
    returns the answer unchanged. Without it the Top 100 button has no route at
    all from another device.
    """
    if request.method == 'OPTIONS':
        return ('', 204)

    import urllib.error
    import urllib.request

    body = request.get_data() or b'{}'
    upstream = urllib.request.Request(
        'http://127.0.0.1:%d/api/top' % DOWNLOAD_PORT, data=body,
        headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(upstream, timeout=180) as answer:
            payload = answer.read()
            status = answer.status
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        status = exc.code
    except Exception as exc:  # noqa: BLE001 - the downloader being down is normal
        return jsonify({'error': 'The downloader on this PC is not answering: %s' % exc}), 503
    return app.response_class(payload, status=status, mimetype='application/json')


@app.route('/api/lyrics', methods=['POST', 'OPTIONS'])
def api_lyrics():
    """Write lyrics for the page with the local language model."""
    if request.method == 'OPTIONS':
        return ('', 204)
    body = request.get_json(silent=True) or {}
    genre = str(body.get('genre') or 'electronic music')[:120]
    subject = str(body.get('subject') or '')[:400]
    seed = int(body.get('seed') or 0)
    corpus = body.get('corpus') or []
    if not isinstance(corpus, list):
        corpus = []
    corpus = [str(item)[:80] for item in corpus][:60]
    angle = str(body.get('angle') or '')[:120]
    avoid = body.get('avoid') or []
    if not isinstance(avoid, list):
        avoid = []
    avoid = [str(line)[:160] for line in avoid][-14:]

    # The write is a job, not one long response. Warm it takes about fifteen
    # seconds, but a cold lyric model - or a card that is still rendering - takes
    # minutes, and this machine is reached through a Cloudflare tunnel, which cuts
    # a request that has produced nothing for a hundred seconds (error 524). That
    # cut is what the page saw as "the lyric model did not answer": the words were
    # written, the connection was not there to carry them. So the write runs in a
    # thread, and the answer is returned here only when it is already finished;
    # otherwise the page collects it from /api/lyrics/<token>, a small GET that no
    # edge timeout can cut.
    token = uuid.uuid4().hex
    with _lyric_jobs_lock:
        _lyric_jobs[token] = {'status': 'writing', 'error': '', 'sections': None,
                              'lines': None, 'model': '', 'created': time.time()}
        if len(_lyric_jobs) > 12:
            finished = [key for key, job in _lyric_jobs.items() if job['status'] != 'writing']
            for key in sorted(finished, key=lambda key: _lyric_jobs[key]['created'])[:-6]:
                _lyric_jobs.pop(key, None)

    threading.Thread(target=_run_lyric_job,
                     args=(token, genre, subject, seed, corpus, angle, avoid),
                     daemon=True).start()

    deadline = time.time() + LYRIC_INLINE_SECONDS
    while time.time() < deadline:
        with _lyric_jobs_lock:
            job = dict(_lyric_jobs.get(token) or {})
        if job.get('status') != 'writing':
            break
        time.sleep(0.2)
    with _lyric_jobs_lock:
        job = dict(_lyric_jobs.get(token) or {})
    if job.get('status') == 'writing':
        return jsonify({'token': token, 'status': 'writing'}), 202
    return _lyric_answer(job)


def _lyric_answer(job):
    """A finished lyric job in the shape the page has always been given."""
    if job.get('status') != 'done':
        return jsonify({'error': job.get('error') or 'the lyric model failed'}), 502
    return jsonify({'lines': job.get('lines') or [], 'sections': job.get('sections') or [],
                    'model': job.get('model') or ''})


def _run_lyric_job(token, genre, subject, seed, corpus, angle, avoid):
    """Write one song's words and keep them where the poll can find them."""
    try:
        from lyrics import lyrics_model_id, load_manifest

        # A whole song in sections: verses, a chorus that repeats, a bridge.
        song = _lyricist().write_song(genre, subject, seed, corpus=corpus,
                                      angle=angle, avoid=avoid)
        sections = song.get('sections') or []
        lines = song.get('lines') or []
        model = lyrics_model_id(load_manifest())
    except Exception as exc:  # noqa: BLE001 - report the reason to the page
        traceback.print_exc()
        with _lyric_jobs_lock:
            job = _lyric_jobs.get(token)
            if job is not None:
                job.update(status='error', error='lyric model failed: %s' % exc)
        return

    print('wrote %d sections / %d lines' % (len(sections), len(lines)), flush=True)
    with _lyric_jobs_lock:
        job = _lyric_jobs.get(token)
        if job is None:
            return
        if len(lines) < 6 or len(sections) < 2:
            job.update(status='error', error='lyric model returned too little text',
                       lines=lines, sections=sections)
        else:
            job.update(status='done', sections=sections, lines=lines, model=model)


@app.route('/api/lyrics/<token>')
def api_lyrics_status(token):
    """Where the poll collects words the tunnel could not carry in one answer."""
    with _lyric_jobs_lock:
        job = dict(_lyric_jobs.get(token) or {})
    if not job:
        return jsonify({'error': 'unknown or expired lyric job'}), 404
    if job['status'] == 'writing':
        return jsonify({'status': 'writing',
                        'elapsed': round(time.time() - job['created'], 1)})
    if job['status'] == 'error':
        return jsonify({'status': 'error', 'error': job['error'],
                        'lines': job.get('lines') or [],
                        'sections': job.get('sections') or []})
    return jsonify({'status': 'done', 'lines': job.get('lines') or [],
                    'sections': job.get('sections') or [], 'model': job.get('model') or ''})


def _langid_state():
    """The installed py3langid and whether it can do what ACE-Step asks."""
    state = {'version': None, 'from_pickled_model': None}
    try:
        from importlib.metadata import version as package_version
        state['version'] = package_version('py3langid')
    except Exception:  # noqa: BLE001 - the field is a report, never a failure
        pass
    try:
        from py3langid.langid import LanguageIdentifier
        state['from_pickled_model'] = hasattr(LanguageIdentifier, 'from_pickled_model')
    except Exception:  # noqa: BLE001
        state['from_pickled_model'] = False
    return state


def _gpu():
    """What the render will run on, so /health can say cuda instead of guessing."""
    try:
        import torch

        if torch.cuda.is_available():
            props = torch.cuda.get_device_properties(0)
            return {'device': 'cuda', 'gpu': torch.cuda.get_device_name(0),
                    'vram_gb': round(props.total_memory / (1024 ** 3), 1)}
    except Exception:  # noqa: BLE001
        pass
    return {'device': 'cpu', 'gpu': None, 'vram_gb': None}


@app.route('/health')
def health():
    ready = _pipeline is not None
    # How long the warm has been running, so the page can say "2m 10s" instead of
    # leaving someone guessing whether it is working or stuck. This machine needs
    # minutes, not seconds, and being told which is not the same as waiting blind.
    warming_for = int(time.time() - _warm_started) if _warm_active else None
    return jsonify({
        'ok': True,
        'warming': bool(_warm_active and not ready),
        'warming_seconds': warming_for,
        # A warm that finished and whose model was then released (the lyric model
        # taking the card on a small GPU) is not warming: it is simply not loaded,
        # and the next song loads it.
        'warm_done': bool(_warm_started and not _warm_active and not _warm_failed),
        'warm_failed': _warm_failed,
        'model': 'ACE-Step v1 3.5B',
        'checkpoint': CHECKPOINT,
        'checkpoint_present': os.path.isdir(CHECKPOINT),
        'loaded': ready,
        'cpu_offload': CPU_OFFLOAD,
        # The setting, and what the card will really run. They differ on a T4, and
        # the difference is the whole reason a song died at 217 seconds once.
        'dtype': _effective_dtype(),
        'dtype_setting': DTYPE,
        'lyrics_loaded': _lyricist_instance is not None,
        # The exact package the segmenter will use, and whether it still has the
        # method ACE-Step calls. A card that answered "the model did not answer"
        # with no way to tell why is why these two fields exist: one request to
        # /health now says whether the environment or the model is the problem.
        'langid': _langid_state(),
        **_gpu(),
    })


# What Cloudflare's edge accepts. urllib's own "Python-urllib/3.x" is refused with
# error 1010 on both Workers, which reads as a rejected key.
BROWSER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                 '(KHTML, like Gecko) Chrome/125.0 Safari/537.36')


def _pool_join_key():
    """The key that lets a machine into the pool, kept on this machine only."""
    from_env = (os.environ.get('SONG_JOIN_KEY') or '').strip()
    if from_env:
        return from_env
    try:
        with open(os.path.join(ROOT, 'build', 'song_endpoint.key'), encoding='utf-8',
                  errors='replace') as handle:
            return handle.read().strip()
    except OSError:
        return ''


def _machine_note():
    """This machine's id, so every card it puts in the pool can be recognised later.

    The reset script writes build/machine_id.txt and sends it with its own join; the
    page's join went without it, so the cards it created carried no id and the reset
    script could not drop them - which is how one PC ended up with three cards.
    """
    try:
        with open(os.path.join(ROOT, 'build', 'machine_id.txt'), encoding='utf-8',
                  errors='replace') as handle:
            return handle.read().strip()[:80]
    except OSError:
        return ''


def _pool_public_url():
    """This machine's tunnel address, written when the tunnel connected."""
    for name in ('SONG_PUBLIC_URL', 'PUBLIC_URL'):
        value = (os.environ.get(name) or '').strip()
        if value:
            return value.rstrip('/')
    try:
        with open(os.path.join(ROOT, 'build', 'tunnel_url.txt'), encoding='utf-8',
                  errors='replace') as handle:
            return handle.read().strip().rstrip('/')
    except OSError:
        return ''


@app.route('/api/pool/join', methods=['POST', 'OPTIONS'])
def api_pool_join():
    """Put this machine in the site's pool because the page asked, not the launcher.

    The launcher used to publish on every start, so a machine turned up in the pool
    without anyone choosing it, and the site answered "AI ready" to somebody who had
    connected nothing. Now the website does the connecting: the page asks this
    service to publish, the key never leaves the machine, and a machine whose page
    is never opened stays out of the pool.
    """
    if request.method == 'OPTIONS':
        return ('', 204)
    key = _pool_join_key()
    if not key:
        return jsonify({'ok': False, 'error': 'no join key on this machine (build/song_endpoint.key)'}), 400
    url = _pool_public_url()
    if not url:
        return jsonify({'ok': False, 'error': 'no tunnel address yet (build/tunnel_url.txt)'}), 400

    answers = {}
    body = json.dumps({'url': url, 'note': _machine_note()}).encode('utf-8')
    for name, target, header in (
        ('site', 'https://browser.bpiersing.workers.dev/api/song/endpoint', 'x-song-key'),
        ('pool', 'https://song-pool.bpiersing.workers.dev/join', 'x-join-key'),
    ):
        try:
            # The user agent matters: Cloudflare answers a bare urllib request with
            # "error code: 1010" and a 403 before the key is looked at, so a join from this
            # service was refused while the identical request from a browser-shaped client
            # was accepted. That looked exactly like a wrong key on the pool Worker.
            ask = urllib.request.Request(target, data=body, headers={
                'Content-Type': 'application/json',
                'Accept': 'application/json',
                'User-Agent': BROWSER_AGENT,
                header: key})
            with urllib.request.urlopen(ask, timeout=20) as answer:
                answers[name] = answer.read().decode('utf-8', 'replace')[:300]
        except Exception as exc:  # noqa: BLE001
            answers[name] = 'error: %s' % exc
    return jsonify({'ok': True, 'url': url, **answers})


@app.route('/api/song', methods=['POST', 'OPTIONS'])
def api_song():
    if request.method == 'OPTIONS':
        return ('', 204)

    data = request.get_json(silent=True) or {}
    prompt = (data.get('prompt') or '').strip()
    lyrics = (data.get('lyrics') or '').strip()
    # An instrumental has no words to sing: the recorded voice is the lead. The
    # marker is what the model understands as "no vocal".
    instrumental = bool(data.get('instrumental'))
    if instrumental:
        lyrics = '[inst]'
        prompt = (prompt + ', instrumental, no lead vocal, backing track').strip(', ')
    if not lyrics:
        return jsonify({'error': 'No lyrics provided'}), 400
    if not prompt:
        prompt = 'electronic, instrumental, cinematic'

    try:
        seconds = float(data.get('seconds') or 60)
    except (TypeError, ValueError):
        seconds = 60
    seconds = max(8.0, min(MAX_SECONDS, seconds))

    try:
        steps = int(data.get('steps') or DEFAULT_STEPS)
    except (TypeError, ValueError):
        steps = DEFAULT_STEPS
    steps = max(8, min(80, steps))

    global _latest_token
    token = uuid.uuid4().hex
    seed = int(data.get('seed') or (uuid.uuid4().int % 1000000))
    with _jobs_lock:
        # One GPU, one song. A second click while a song is still rendering - or
        # a page that was reloaded and asked again - joins the song in flight
        # instead of queueing another multi-minute render behind it.
        for running_token, job in _jobs.items():
            if job['status'] in ('queued', 'loading', 'generating'):
                return jsonify({'token': running_token, 'status': job['status'],
                                'seconds': job['seconds'], 'reused': True}), 202

    # Outside the lock: _prune_jobs takes it itself.
    _prune_jobs()
    with _jobs_lock:
        _jobs[token] = {
            'status': 'queued', 'progress': 0.0, 'error': '',
            'path': None, 'tmp_dir': None, 'elapsed': 0,
            'prompt': prompt, 'seconds': seconds,
            'created': time.time(),
        }
        _latest_token = token

    threading.Thread(target=_run_job, args=(token, prompt, lyrics, seconds, steps, seed),
                     daemon=True).start()
    return jsonify({'token': token, 'status': 'queued', 'prompt': prompt,
                    'seconds': seconds, 'steps': steps}), 202


@app.route('/api/song/voice', methods=['POST', 'OPTIONS'])
def api_song_voice():
    """A song whose lyrics the AI writes and the recorded voice sings.

    The request body is the voice recording - a description of the timbre, not a
    source of words. Everything else rides in the query string, so a phone can
    send the audio without building a multipart form.
    """
    if request.method == 'OPTIONS':
        return ('', 204)
    raw = request.get_data()
    if len(raw) < 2000:
        return jsonify({'error': 'Send the voice recording as the request body.'}), 400

    prompt = (request.args.get('prompt') or '').strip()
    lyrics = (request.args.get('lyrics') or '').strip()
    try:
        seconds = float(request.args.get('seconds') or 60)
    except (TypeError, ValueError):
        seconds = 60
    seconds = max(20.0, min(MAX_SECONDS, seconds))
    try:
        steps = int(request.args.get('steps') or DEFAULT_STEPS)
    except (TypeError, ValueError):
        steps = DEFAULT_STEPS
    steps = max(8, min(80, steps))
    # The page's voice map: the same semitones, tone and room it applied to the
    # recorded phrase, to be applied to what the model sings.
    try:
        semitones = float(request.args.get('semitones') or 0)
    except (TypeError, ValueError):
        semitones = 0.0
    semitones = max(-12.0, min(12.0, semitones))
    try:
        lowpass = float(request.args.get('lowpass') or 0)
    except (TypeError, ValueError):
        lowpass = 0.0
    lowpass = 0.0 if lowpass <= 0 else max(300.0, min(20000.0, lowpass))
    try:
        room = float(request.args.get('room') or 0)
    except (TypeError, ValueError):
        room = 0.0
    room = max(0.0, min(0.9, room))
    mapping = (semitones, lowpass, room)

    global _latest_token
    token = uuid.uuid4().hex
    seed = int(request.args.get('seed') or (uuid.uuid4().int % 1000000))
    with _jobs_lock:
        for running_token, job in _jobs.items():
            if job['status'] in ('queued', 'loading', 'generating', 'writing',
                                 'singing', 'mixing'):
                return jsonify({'token': running_token, 'status': job['status'],
                                'reused': True}), 202

    _prune_jobs()
    logs = os.path.join(HERE, 'logs')
    os.makedirs(logs, exist_ok=True)
    reference_path = os.path.join(logs, 'voice_reference_%s.wav' % token)
    with open(reference_path, 'wb') as handle:
        handle.write(raw)
    with _jobs_lock:
        _jobs[token] = {
            'status': 'writing', 'progress': 0.02, 'error': '',
            'path': None, 'tmp_dir': None, 'elapsed': 0,
            'prompt': prompt, 'seconds': seconds, 'lyrics': lyrics,
            'voice': None, 'created': time.time(),
        }
        _latest_token = token

    threading.Thread(target=_run_voice_job,
                     args=(token, reference_path, prompt, lyrics, seconds, steps, seed,
                           mapping),
                     daemon=True).start()
    return jsonify({'token': token, 'status': 'writing', 'seconds': seconds}), 202


@app.route('/api/song/latest')
def api_song_latest():
    """The newest job, so a reloaded or restarted page can join the song.

    A song takes minutes on one card. Without this the token only existed in
    the page's memory: a reload, a closed tab or a restarted app left the model
    singing into nothing, and the page looked like it had never run it.

    """
    with _jobs_lock:
        job = _jobs.get(_latest_token) if _latest_token else None
        if job is None:
            # Nothing has been asked for since this process started. That is a
            # normal state, not a missing resource: a 404 made the page's console
            # read as if something had gone wrong.
            return jsonify({'status': 'idle', 'token': None})
        return jsonify({
            'token': _latest_token,
            'status': job['status'],
            'progress': job['progress'],
            'error': job['error'],
            'elapsed': round(time.time() - job['created'], 1),
            'seconds': job['seconds'],
        })


@app.route('/api/song/<token>')
def api_song_status(token):
    with _jobs_lock:
        job = _jobs.get(token)
        if job is None:
            return jsonify({'error': 'Unknown or expired job'}), 404
        return jsonify({
            'status': job['status'],
            'progress': job['progress'],
            'error': job['error'],
            'elapsed': round(time.time() - job['created'], 1),
            'seconds': job['seconds'],
        })


@app.route('/song_file/<token>')
def song_file(token):
    with _jobs_lock:
        job = dict(_jobs.get(token) or {})

    path = None
    if job.get('status') == 'complete' and job.get('path') and os.path.exists(job['path']):
        path = job['path']
    elif os.path.exists(_kept_song(token)):
        # The job record is gone (a restart, or a later song pruned it) but the
        # song itself was kept, which is all the player or "Save" needs.
        path = _kept_song(token)

    if path is None:
        if job and job.get('status') not in ('complete', 'error'):
            percent = round((job.get('progress') or 0) * 100)
            return jsonify({'error': f'this song is still rendering ({percent}%)'}), 404
        return jsonify({'error': 'that song is no longer on this machine - make it again'}), 404

    prompt = job.get('prompt') or 'song'

    label = re.sub(r'[^a-z0-9]+', '-', prompt.lower())[:40].strip('-') or 'song'
    # The job stays until a later one replaces it: the page reads this file once
    # for the player and again when "Save the song" is clicked, and a reloaded
    # page may ask for it a third time.
    return send_file(path, as_attachment=True,
                     download_name=f'library-song-{label}.wav',
                     mimetype='audio/wav')


@app.route('/api/song/mix', methods=['POST', 'OPTIONS'])
def api_song_mix():
    """Put a recorded phrase into the song that was just rendered.

    The body is the fitted voice as a wav. It is placed where an arrangement
    would put it - on the turn and again two thirds in - so the phrase lands with
    the music rather than playing over the top of it from the first bar.
    """
    if request.method == 'OPTIONS':
        return ('', 204)
    # Read and written here: without this the read below becomes a local lookup
    # and fails with UnboundLocalError.
    global _latest_token
    voice = request.get_data()
    if not voice:
        return jsonify({'error': 'No voice audio sent'}), 400

    with _jobs_lock:
        job = _jobs.get(_latest_token) if _latest_token else None
        finished = job if (job and job.get('status') == 'complete' and job.get('path')) else None

    # A restart forgets the jobs, but the last render is kept on disk for exactly
    # this reason: the song survives, so the voice can still be mixed into it.
    song_path = finished['path'] if finished else None
    prompt = (finished.get('prompt') if finished else '') or 'song'
    if not song_path or not os.path.exists(song_path):
        kept = os.path.join(HERE, 'logs', 'last_song.wav')
        if os.path.exists(kept):
            song_path = kept
            prompt = 'library song'
    if not song_path or not os.path.exists(song_path):
        return jsonify({'error': 'No finished song to mix into yet - generate one first'}), 409

    try:
        import io
        import secrets
        import numpy as np
        import soundfile as sf

        song, rate = sf.read(song_path, always_2d=True, dtype='float32')
        phrase, phrase_rate = sf.read(io.BytesIO(voice), always_2d=True, dtype='float32')
        if phrase.size == 0:
            return jsonify({'error': 'The voice audio was empty'}), 400

        # Mono, at the song's own rate.
        mono = phrase.mean(axis=1)
        if phrase_rate != rate and phrase_rate > 0:
            wanted = int(round(len(mono) * rate / phrase_rate))
            mono = np.interp(
                np.linspace(0, len(mono) - 1, wanted),
                np.arange(len(mono)),
                mono,
            ).astype('float32')

        peak = float(np.max(np.abs(mono)) or 0.0)
        if peak > 0:
            mono = (mono / peak) * 0.7

        out = song.copy()
        fade = max(1, int(rate * 0.02))
        envelope = np.ones(len(mono), dtype='float32')
        envelope[:fade] = np.linspace(0, 1, fade)
        envelope[-fade:] = np.linspace(1, 0, fade)

        # On the beat, not over the top. The phrase is placed on bar lines of the
        # song's own tempo, repeated as a hook across four bars, panned side to side
        # and the track is ducked under it - which is what makes it sit in the song
        # instead of sounding pasted on.
        try:
            bpm = float(request.args.get('bpm') or 0)
        except (TypeError, ValueError):
            bpm = 0.0
        if not (40 <= bpm <= 260):
            bpm = 120.0
        beat = 60.0 / bpm
        bar = 4.0 * beat
        first_bar = int((len(out) * 0.30) / (bar * rate)) + 1
        # Lead means the voice carries the song: louder, and the track steps further
        # back under it. A layer is quieter and leaves the track where it is.
        lead = str(request.args.get('lead') or '') in ('1', 'true', 'yes')
        # Was 0.4 (lead) / 0.55, applied once per copy: with copies overlapping that
        # compounded to -32 dB of the track. See _place_phrases.
        duck = 0.55 if lead else 0.7
        out, placed = _place_phrases(out, mono, envelope, rate, fade, lead, duck,
                                     int(first_bar * bar * rate), bar)

        loudest = float(np.max(np.abs(out)) or 0.0)
        if loudest > 0.98:
            out = out * (0.98 / loudest)

        mixed_path = os.path.join(os.path.dirname(song_path), 'song_with_voice.wav')
        sf.write(mixed_path, out, rate)

        token = secrets.token_hex(8)
        with _jobs_lock:
            _jobs[token] = {
                'status': 'complete', 'progress': 1.0, 'error': '',
                'path': mixed_path, 'tmp_dir': None, 'elapsed': 0,
                'prompt': prompt + ' with voice',
                'seconds': round(len(out) / float(rate), 2),
                'created': time.time(),
            }
            _latest_token = token
        print('mixed the voice onto bar lines at %s%% (%s bpm)' % (placed, bpm), flush=True)
        return jsonify({'token': token, 'seconds': round(len(out) / float(rate), 2),
                        'placed_at': placed, 'bpm': bpm, 'bars': len(placed), 'lead': lead})
    except Exception as exc:
        print('mix failed: %r' % exc, flush=True)
        return jsonify({'error': 'Could not mix the voice in: %s' % exc}), 500


@app.route('/last_song')
def last_song():
    """The last rendered song, saved next to the logs.

    A stable address for the one thing that matters after a restart: the audio
    the model actually produced.
    """
    path = os.path.join(HERE, 'logs', 'last_song.wav')
    if not os.path.exists(path):
        return jsonify({'error': 'Nothing has been rendered yet'}), 404
    return send_file(path, as_attachment=True, download_name='last-song.wav',
                     mimetype='audio/wav')


def _warm():
    """Put the song model on the card at startup instead of on the first song.

    Records when it started and how it ended, so /health can report "warming, 2m
    10s so far" or the reason it failed rather than a bare loaded: false.

    Everything else loading lazily keeps the service quick to answer, but the
    first compose then pays for the load. With SONG_WARM=1 that minute is spent
    quietly after start instead, and health reports loaded: true when it is
    ready. Off by default, because holding 8 GB of VRAM from the moment a
    service starts is not what every machine wants.
    """
    global _warm_started, _warm_failed, _warm_active
    _warm_started = time.time()
    _warm_active = True
    try:
        info = _gpu()
        print(f'warming on {info["device"]}: {info.get("gpu") or "cpu"}', flush=True)
        _load_pipeline()
        print('song model ready after %.0fs' % (time.time() - _warm_started), flush=True)
    except Exception as exc:  # noqa: BLE001
        _warm_failed = str(exc)[:300]
        print(f'warm load failed: {exc}', flush=True)
    finally:
        _warm_active = False


if __name__ == '__main__':
    # The page looks for this service on 8090, so an empty or zero PORT from the
    # environment must not turn into a random port.
    port = int(os.environ.get('SONG_PORT') or os.environ.get('PORT') or 8090)
    # Loopback by default: on a desktop the tunnel runs on the same machine. A
    # container has to answer on its own interface instead, which is what
    # deploy/gpu sets SONG_HOST for.
    host = os.environ.get('SONG_HOST') or '127.0.0.1'
    if not port:
        port = 8090
    if os.environ.get('SONG_WARM', '0') not in ('0', '', 'false', 'False'):
        threading.Thread(target=_warm, daemon=True).start()
    # Otherwise loading happens on the first request so the service answers
    # health immediately.
    app.run(host=host, port=port, threaded=True)
