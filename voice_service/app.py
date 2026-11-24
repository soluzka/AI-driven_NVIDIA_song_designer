"""Singing-in-your-own-voice service.

The song model writes words, but it cannot use the voice of the person who
recorded a phrase: it only produces one mixed track with its own singer. This
service takes the recording as a *timbre reference* only - the words in it are
never used - and speaks the lyrics the AI wrote, in that voice. That vocal is then
laid over an instrumental backing by the song service.

Chatterbox is a zero-shot cloning model: about five seconds of clean speech is
enough to describe a voice. It is a speech model, so the result is a sung-lyric
performance built from spoken phrases, not a melodic renderer.
"""

import os
import re
import secrets
import tempfile
import threading
import time

from flask import Flask, jsonify, request, send_file
from flask_cors import CORS

BASE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE, 'logs')
os.makedirs(OUT_DIR, exist_ok=True)

app = Flask(__name__)
CORS(app)

# numpy and soundfile write the vocal; they are not needed to answer. A host that
# publishes this API without the model install (the Vercel service) starts on
# Flask alone, and /health says so rather than the import killing the process.
try:
    import numpy as np
    import soundfile as sf
    _AUDIO_DEPS_ERROR = None
except Exception as _exc:  # noqa: BLE001 - a slim host has neither
    np = None
    sf = None
    _AUDIO_DEPS_ERROR = str(_exc)

_model = None
_model_lock = threading.Lock()

# Chatterbox degrades on very long single passes, so the lyrics go in short
# phrases and the phrases are joined with a breath of silence.
PHRASE_CHARS = 180
GAP_SECONDS = 0.18


def _model_installed():
    """Whether this host can synthesise at all: chatterbox, numpy and soundfile."""
    import importlib.util

    if np is None or sf is None:
        return False
    try:
        return importlib.util.find_spec('chatterbox') is not None
    except (ImportError, ValueError):
        return False


def _torch_status():
    """Whether a card is visible, without paying for torch at startup.

    torch takes tens of seconds to import on Windows. Importing it at module level
    meant the process existed but Flask had not bound the port yet, so the launcher
    saw a running process and the page saw a refused connection - the two together
    read as "the voice service is up but broken". The import now happens on first
    use, and health answers `unknown` until then instead of blocking.
    """
    try:
        import torch

        return {'cuda': bool(torch.cuda.is_available()), 'torch': torch.__version__}
    except Exception as err:  # noqa: BLE001
        return {'cuda': None, 'torch': 'unavailable: %s' % (err,)}


def _load_model():
    global _model
    with _model_lock:
        if _model is None:
            import torch
            from chatterbox.tts import ChatterboxTTS
            device = 'cuda' if torch.cuda.is_available() else 'cpu'
            _model = ChatterboxTTS.from_pretrained(device=device)
        return _model


def _phrases(text):
    """Split lyrics into lines, dropping tags the model should not read aloud."""
    text = re.sub(r'\[[^\]]*\]', ' ', text or '')
    lines = [re.sub(r'\s+', ' ', line).strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return []
    phrases, current = [], ''
    for line in lines:
        if len(current) + len(line) + 1 > PHRASE_CHARS and current:
            phrases.append(current)
            current = line
        else:
            current = (current + ' ' + line).strip()
    if current:
        phrases.append(current)
    return phrases


@app.get('/health')
def health():
    # Only touch torch once something has asked for the model; until then this must
    # answer in milliseconds so the launcher can tell up from loading.
    status = _torch_status() if _model is not None else {'cuda': None, 'torch': 'not loaded'}
    return jsonify({'ok': True, 'model_loaded': _model is not None,
                    'model_installed': _model_installed(),
                    'audio_deps_error': _AUDIO_DEPS_ERROR,
                    'cuda': status.get('cuda'), 'torch': status.get('torch')})


@app.post('/api/voice/sing')
def sing():
    """Reference clip in the body, lyrics in ?text= or JSON text; WAV comes back."""
    if not _model_installed():
        return jsonify({
            'error': 'This host has no voice model installed: chatterbox, numpy and '
                     'soundfile live in the GPU install, so the API answers but '
                     'nothing can be sung here.',
            'model_installed': False,
        }), 503

    raw = request.get_data()
    if len(raw) < 2000:
        return jsonify({'error': 'Send the voice recording as the request body.'}), 400

    payload = request.get_json(silent=True) or {}
    text = (request.args.get('text') or payload.get('text') or '').strip()
    phrases = _phrases(text)
    if not phrases:
        return jsonify({'error': 'There are no lyrics to sing.'}), 400

    started = time.time()
    work = tempfile.mkdtemp(prefix='voice_')
    ref_path = os.path.join(work, 'reference.wav')
    with open(ref_path, 'wb') as handle:
        handle.write(raw)

    try:
        model = _load_model()
        pieces = []
        for phrase in phrases:
            wav = model.generate(phrase, audio_prompt_path=ref_path)
            if hasattr(wav, 'detach'):
                wav = wav.detach().float().cpu().numpy()
            wav = np.asarray(wav).reshape(-1).astype('float32')
            if wav.size:
                pieces.append(wav)
                pieces.append(np.zeros(int(model.sr * GAP_SECONDS), dtype='float32'))
        if not pieces:
            return jsonify({'error': 'The model returned no audio.'}), 502

        vocal = np.concatenate(pieces)
        peak = float(np.max(np.abs(vocal)) or 0.0)
        if peak > 0.98:
            vocal *= 0.98 / peak
        token = secrets.token_hex(8)
        out_path = os.path.join(OUT_DIR, 'vocal_%s.wav' % token)
        sf.write(out_path, vocal, model.sr)
        seconds = round(len(vocal) / float(model.sr), 2)
        print('sang %d phrases in %.1fs -> %s (%.2fs)' % (
            len(phrases), time.time() - started, out_path, seconds), flush=True)
        return jsonify({'token': token, 'seconds': seconds,
                        'phrases': len(phrases), 'sample_rate': model.sr,
                        'url': '/voice_file/' + token})
    except Exception as err:  # noqa: BLE001 - the page needs the reason, not a traceback
        print('sing failed: %r' % (err,), flush=True)
        return jsonify({'error': str(err)}), 500
    finally:
        try:
            os.remove(ref_path)
            os.rmdir(work)
        except OSError:
            pass


@app.get('/voice_file/<token>')
def voice_file(token):
    if not re.fullmatch(r'[0-9a-f]{8,32}', token):
        return jsonify({'error': 'bad token'}), 400
    path = os.path.join(OUT_DIR, 'vocal_%s.wav' % token)
    if not os.path.exists(path):
        return jsonify({'error': 'not found'}), 404
    return send_file(path, mimetype='audio/wav')


# ---------------------------------------------------------------------------
# Public mount path
#
# A platform that publishes this service under a prefix (Vercel services route
# /voice/* here and keep the path) hands the request over with that prefix still
# in PATH_INFO. The routes below are written without one, so it is stripped here
# and recorded in SCRIPT_NAME: the same app answers at / locally and at
# /voice/<route> in front of the internet.
# ---------------------------------------------------------------------------
class _MountPrefix:
    def __init__(self, wsgi_app, prefix):
        self.wsgi_app = wsgi_app
        self.prefix = prefix

    def __call__(self, environ, start_response):
        path = environ.get('PATH_INFO') or ''
        if path == self.prefix or path.startswith(self.prefix + '/'):
            environ['SCRIPT_NAME'] = (environ.get('SCRIPT_NAME') or '') + self.prefix
            environ['PATH_INFO'] = path[len(self.prefix):] or '/'
        return self.wsgi_app(environ, start_response)


app.wsgi_app = _MountPrefix(app.wsgi_app, '/voice')


if __name__ == '__main__':
    port = int(os.environ.get('VOICE_PORT', '8095'))
    app.run(host='127.0.0.1', port=port, threaded=True)
