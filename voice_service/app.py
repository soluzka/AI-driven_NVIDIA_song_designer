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

import numpy as np
import soundfile as sf
import torch
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS

BASE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE, 'logs')
os.makedirs(OUT_DIR, exist_ok=True)

app = Flask(__name__)
CORS(app)

_model = None
_model_lock = threading.Lock()

# Chatterbox degrades on very long single passes, so the lyrics go in short
# phrases and the phrases are joined with a breath of silence.
PHRASE_CHARS = 180
GAP_SECONDS = 0.18


def _load_model():
    global _model
    with _model_lock:
        if _model is None:
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
    return jsonify({'ok': True, 'model_loaded': _model is not None,
                    'cuda': torch.cuda.is_available()})


@app.post('/api/voice/sing')
def sing():
    """Reference clip in the body, lyrics in ?text= or JSON text; WAV comes back."""
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


if __name__ == '__main__':
    port = int(os.environ.get('VOICE_PORT', '8095'))
    app.run(host='127.0.0.1', port=port, threaded=True)
