# Voice service

The model that sings the AI's lyrics in **your** voice.

This is a separate service from `song_service` on purpose. That one owns ACE-Step
(the music model) and the language model that writes the lyrics. This one owns a
voice-cloning model. They are two different libraries with two different torch
expectations, and installing the cloning model into the song environment would
risk the 3.5B music model that already works. So each has its own `.venv`, and
each is started by the same launcher.

## What it does, and what it does not

What it does: takes a recording of your voice (about five seconds is enough),
ignores every word in it, and speaks **new text** in that voice. In this project
the new text is the lyrics `song_service` just wrote, so the song is sung in your
timbre instead of the model's singer.

What it does not do: turn a spoken phrase into a melody. Chatterbox is a speech
model, not a singing voice synthesiser. The result is a vocal performance built
from spoken phrases laid over the beat — closer to spoken-word or a rap delivery
than to a belted chorus. If you want true sung pitch, that needs a singing voice
conversion model (RVC / so-vits-svc) plus stem separation, which are not part of
this project.

It also cannot be hosted on Cloudflare: Workers have no CUDA and no room for
these weights. It runs on the GPU in this machine and is reached only locally by
`song_service`.

## Install

Needs Python 3.11, an NVIDIA card with a CUDA 12 driver, and a couple of GB of
disk for the wheels plus the weights. Run from the project root:

```
uv venv voice_service\.venv --python 3.11 --seed
uv pip install --python voice_service\.venv\Scripts\python.exe -r voice_service\requirements.txt
uv pip install --python voice_service\.venv\Scripts\python.exe --reinstall torch==2.6.0+cu126 torchaudio==2.6.0+cu126 --index-url https://download.pytorch.org/whl/cu126
```

The second command is the one that is easy to miss. `chatterbox-tts` pins
`torch==2.6.0`, and PyPI's 2.6.0 wheel is the **CPU** build — so installing the
requirements alone silently replaces a working CUDA torch with one where
`torch.cuda.is_available()` is `False`, and the service falls back to the CPU and
takes minutes per phrase. Reinstalling the `+cu126` wheels of the same version
afterwards keeps chatterbox's pin satisfied and the card in use. `requirements.txt`
carries the same note, so the ordering travels with the file.

Check it with:

```
voice_service\.venv\Scripts\python.exe -c "import torch; print(torch.__version__, torch.cuda.is_available())"
```

which must print `2.6.0+cu126 True`.

The model weights are downloaded from Hugging Face on the **first** request, not
at install time, and they land in the usual Hugging Face cache
(`%USERPROFILE%\.cache\huggingface`). The first "Sing the lyrics" therefore pays
for the download as well as the load; later ones do not.

If an import fails with `module 'numpy' has no attribute '__version__'`, a copy of
numpy was interrupted half-extracted: its `dist-info` is there but
`numpy/__init__.py` is not. `uv pip install --python voice_service\.venv\Scripts\python.exe --reinstall numpy==1.26.4` fixes it
without touching torch. numpy 2.x is not an option — chatterbox's pins are for
1.26.

## Run

The launcher starts it with everything else:

```
powershell -NoProfile -ExecutionPolicy Bypass -File tools\start_services.ps1
```

On its own:

```
$env:VOICE_PORT = 8095; voice_service\.venv\Scripts\python.exe voice_service\app.py
```

Logs: `voice_service/logs/service_out.log` and `service_err.log`, written by the
launcher.

## Endpoints

| Route | Method | Purpose |
| --- | --- | --- |
| `/health` | GET | Whether the model is loaded, and whether CUDA is visible |
| `/api/voice/sing?text=…` | POST | Body is the reference WAV; the query is the lyrics. Returns `{token, seconds, phrases, url}` |
| `/voice_file/<token>` | GET | The sung WAV |

The reference clip is written to a temporary file, used for the request, and
removed afterwards. It is not stored: the saved output is only the singing.

## How `song_service` uses it

`song_service` never asks the browser to talk to this service. `POST
/api/song/voice` on **8090** receives the recording, and does the whole job:

1. write lyrics with the language model,
2. `POST /api/voice/sing` here with the recording and those lyrics,
3. render an instrumental backing with ACE-Step,
4. lay the sung vocal over the backing, ducking the track under it.

That is why only one address is public — the tunnel points at 8090, and this
service stays on loopback.

## Sharing the card

The machine has 8 GB of VRAM. ACE-Step wants most of it for the render and this
model wants a few GB for the voice, so the two are never loaded at the same time;
`song_service` renders the backing and then this service is used for the vocal.
If a request fails with a CUDA out-of-memory error, the other model was still
resident — render the backing first, then sing.

## Tunables

In `voice_service/app.py`:

- `PHRASE_CHARS` (180) — how much lyric text goes into one model pass. Long text
  in a single pass degrades; the phrases are generated in order and joined with
  `GAP_SECONDS` (0.18 s) of silence between them.
- `OUT_DIR` — `voice_service/logs`, where the sung WAVs are kept. They are small;
  nothing prunes them yet.
