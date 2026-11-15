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
voice_service\.venv\Scripts\python.exe voice_service\install.py --check    # report, change nothing
voice_service\.venv\Scripts\python.exe voice_service\install.py            # install everything
```

**`install.py` is the whole install, and it deliberately does not leave you on CPU
torch.** `chatterbox-tts` pins `torch==2.6.0`, and PyPI's 2.6.0 wheel is the **CPU**
build — a plain `pip install -r requirements.txt` therefore replaces a working CUDA
torch with one where `torch.cuda.is_available()` is `False`, silently, with no
error, and the service then takes minutes per phrase on the processor. The script
installs the packages and then puts the `+cu126` wheels of that same version back,
so one command leaves the machine in the state it needs. Running it again is safe:
it repairs that state if anything else has broken it since.

`--check` prints what is present, the torch build and whether a card is visible,
without installing anything. On this machine it reports:

```
  torch 2.6.0+cu126
  cuda  True  (NVIDIA GeForce RTX 4060 Ti)
```

`requirements.txt` holds the same package list for reading, with the same warning
at the top. Installing the small packages on their own later (for example
`uv pip install --python voice_service\.venv\Scripts\python.exe soundfile`) is safe
and does not touch torch — it is only `chatterbox-tts` that drags a torch wheel
with it.

The model weights are downloaded from Hugging Face on the **first** request, not
at install time, and they land in the usual Hugging Face cache
(`%USERPROFILE%\.cache\huggingface`). The first "Sing the lyrics" therefore pays
for the download as well as the load; later ones do not.

If an import fails with `module 'numpy' has no attribute '__version__'`, a copy of
numpy was interrupted half-extracted: its `dist-info` is there but
`numpy/__init__.py` is not. `uv pip install --python voice_service\.venv\Scripts\python.exe --reinstall numpy==1.26.4` fixes it
without touching torch. numpy 2.x is not an option — chatterbox's pins are for
1.26.

## Traps that cost real time here

Each of these failed in a way that pointed at something else. They are written
down because the symptoms are misleading, not obvious.

| Symptom | Cause | Fix |
| --- | --- | --- |
| Page says `WinError 10061 … actively refused it` | `flask` was never installed — only `chatterbox-tts` was — so the process existed, nothing was bound, and the launcher said *"still starting"* | `install.py` installs the full list |
| Launcher says *"still starting"* forever, port never opens | `app.py` imported `torch` at module level (22.9 s cold here) before Flask binds | the import is lazy; `/health` answers 4 s after the launcher, torch loads on first use |
| `TypeError: 'NoneType' object is not callable` at `from_pretrained` | `perth`, the watermarker chatterbox instantiates, imports `pkg_resources`; setuptools 81+ removed it, so `PerthImplicitWatermarker` was `None` | `setuptools<81` is pinned; the import is what gives the watermarker a class to call |
| `module 'numpy' has no attribute '__version__'` or `'ndarray'` | an interrupted install left numpy's `dist-info` without `numpy/__init__.py` | `install.py` (or `--reinstall numpy==1.26.4`) repairs it without touching torch |
| `import chatterbox.tts` "hangs" for minutes | three processes importing torch at once (service + probes) contend for the same DLLs; a single cold import is ~23 s | start one process at a time; nothing is actually deadlocked |

## Measured on this machine

RTX 4060 Ti 8 GB, torch `2.6.0+cu126`, weights cached in `%USERPROFILE%\.cache\huggingface`.

| Step | Time |
| --- | --- |
| cold `import torch` | 22.9 s |
| `import chatterbox` chain (4 177 modules) | 22.9 s total from start |
| model onto the card | 8.7 s |
| 13 words → 5.66 s of speech at 24 kHz | 32.5 s |
| first request ever (weights download, ~2.2 GB) | add several minutes |
| full `POST /api/song/voice`, 45 s song | **447 s** — lyrics, singing, backing, mix |

Output checked on a finished job: `45.0 s`, 48 kHz, stereo, peak `0.927` (no
clipping), and the vocal region shows ~64 energy swings per 8 s — syllabic, which
is what speech looks like in an envelope, not a sustained sung note.

That last number is the honest limit of this model: it **speaks** the lyrics over
the beat. A sung melody needs singing-voice conversion (RVC / so-vits-svc) and
stem separation, neither of which is installed.

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
