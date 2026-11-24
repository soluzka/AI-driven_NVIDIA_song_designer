# Song service

Two different things live near this folder, and it matters which is which.

## This service is the only song AI in the project

The deployed Worker runs one model, and it is not this one: Workers AI handles
voice search (`@cf/openai/whisper`). Nothing at the edge composes music — the
Workers AI catalog has no music model — so everything that writes words or sings
them happens here, on this machine.

The page calls `http://127.0.0.1:8090` for both halves of a song: the lyric model
first, then ACE-Step for the sung audio. That works from the deployed https page,
not just from localhost, because the service answers the browser's private
network preflight with `Access-Control-Allow-Private-Network`. When the service
is not running the page waits up to three minutes for it to come up (a boot takes
about a minute) and only then falls back to the sampled band in
`public/index.html` — instruments only, no voice.

On a phone or another computer `127.0.0.1` is that device, so the page uses the
tunnel instead: the launcher starts `cloudflared`, publishes its address to the
site, and the page reads it on load. This service is what the tunnel points at,
which is why it also forwards the downloader's routes — see `/api/top` and `/pc`
below.

## ACE-Step is the music model

This folder can also run a full model on your own GPU. That is **not** installed
to the website and cannot be: Workers have no Python, no CUDA and no gigabytes
of memory, and Cloudflare's containers are CPU-only, where a 3.5B diffusion
model is minutes to hours per song rather than seconds.

It is still useful locally, and it is free per song.

### Install

Everything is declared in `models.json`, so a fresh machine needs one command
and no manual cloning or pip archaeology. The file holds two lists:

| `packages` | Why it is there |
| --- | --- |
| `torch`, `torchaudio` (cu126 index) | the CUDA build; PyPI's default on Windows is CPU-only |
| `transformers==4.57.6`, `huggingface-hub==0.36.2` | the lyric model, pinned as a pair that works together |
| `git+https://github.com/ace-step/ACE-Step.git` | the music model's code |
| `flask`, `flask-cors` | the HTTP service the page calls |
| `soundfile`, `librosa` | writing the wav and ACE's audio loading |

| `models` | Size | Licence |
| --- | --- | --- |
| `ACE-Step/ACE-Step-v1-3.5B` — music, sings the lyrics | ≈7 GB | Apache-2.0 |
| `Qwen/Qwen2.5-1.5B-Instruct` — writes the lyrics | ≈3.1 GB | Apache-2.0 |

The commands below assume `song_service\.venv` exists; create it first, because
`requirements.txt` in this folder lists the packages and their pins — torch and
torchaudio from the CUDA index, `transformers==4.57.6`, `huggingface-hub==0.36.2`,
ACE-Step from git, and the small helpers. It is the readable copy: `install.py` is
what actually installs, because it also fetches the ~10 GB of weights, which no
requirements file can do. Read the list when you want to know what is installed;
run the installer when you want it installed.

`install.py` installs into whatever interpreter runs it and does not build its own.
`uv` rather than `python -m venv` is deliberate: on Windows `python` is often the
Microsoft Store stub, and `uv venv` needs `--seed` to put `pip` inside the
environment. `py -3.11 -m venv` also fails on this machine, where the interpreter
`uv` installed is registered with the `py` launcher but cannot be started through
it.

```
uv venv song_service\.venv --python 3.11 --seed                # once, on a new machine
song_service\.venv\Scripts\python.exe song_service\install.py            # packages + both models
song_service\.venv\Scripts\python.exe song_service\install.py --packages   # code only
song_service\.venv\Scripts\python.exe song_service\install.py --weights    # models only
song_service\.venv\Scripts\python.exe song_service\install.py --check      # report, change nothing
song_service\.venv\Scripts\python.exe song_service\gpu_check.py            # confirms CUDA
```

These are written from the repository root, which is why every script carries the
`song_service\` prefix; run from inside this folder instead and the same commands
work without it. `--check` on the machine here reports torch `2.14.1+cu126`,
transformers `4.57.6`, both weight folders and *"Everything the service needs is
installed."*

`--check` is the quick answer to "is everything there?": it names each package,
each weight folder, and the installed torch/transformers versions, and exits
non-zero if anything is missing. Both models are fetched from Hugging Face into
`song_service/models/`, which is gitignored — they are package downloads, not
repository content.

Neither the venv nor the models should ever be copied between machines: the venv's
`pyvenv.cfg` holds an absolute path to the interpreter that built it, so a copied
one fails with a missing python, and the weights are 10 GB. On this repo that
happened when the Windows profile changed — all three venvs still pointed at the
old user's Python and every service refused to start.

One order matters: the lyrics model writes the words, then ACE-Step sings that
exact text, so both are needed for a sung song.

No CUDA toolkit install is needed on Windows: the driver supplies the runtime
and the torch wheel carries its own CUDA libraries. Check `models.json` →
`cuda` for the index and the minimum driver.

### Run

Start it with the launcher rather than by hand, because the launcher also warms
the model and keeps the site's Start the AI button registered:

```
powershell -NoProfile -ExecutionPolicy Bypass -File tools\start_services.ps1
```

By hand, from the repo root:

```
song_service\.venv\Scripts\python.exe song_service\app.py
```

Listens on `127.0.0.1:8090`:

```
GET  /health                                        -> device, card, loaded, warm clock
POST /api/lyrics {genre, subject, seed, ...}        -> {sections, lines, model}
   ... or 202 {token} while it writes, then:
GET  /api/lyrics/<token>                            -> {status, sections, lines, model}
POST /api/song   {prompt, lyrics, seconds, steps}   -> 202 {token}
GET  /api/song/<token>                              -> {status, progress, error}
GET  /api/song/latest                               -> the newest job, or {status: idle}
GET  /song_file/<token>                             -> the wav
GET  /last_song                                     -> the last render, after a restart
POST /api/top    {query | url, limit}                -> forwarded to the downloader on 8080
ANY  /pc/<path>                                      -> forwarded to the downloader on 8080
```

The last two exist because this service owns the tunnel. yt-dlp has to run on
this machine, so `Top 100` and the video player cannot be served from the edge;
the tunnel carries them here and these routes hand them to the downloader on
loopback. `/pc` forwards the method, body, `Range` and content headers, so a
video streams and seeks exactly as it does locally.

`/health` is the honest answer to "is it on the GPU":

```json
{"ok": true, "device": "cuda", "gpu": "NVIDIA GeForce RTX 4060 Ti",
 "vram_gb": 8.0, "loaded": true, "lyrics_loaded": true, "cpu_offload": true,
 "warming": false, "warming_seconds": null, "warm_failed": null}
```

`loaded` is what matters, and `warming` / `warming_seconds` are why the badge need
not guess: with `SONG_WARM=1` the service answers within seconds while the weights
are still going onto the card, and it reports how long that has been running so
the page can say `CUDA · warming 1m 35s`. Measured here: **51 seconds** to
`loaded: true` with nothing competing. `warm_failed` carries the reason if the
load raised.

8 GB of VRAM is tight for a 3.5B model, so `ACE_CPU_OFFLOAD=1` is the default: it
pages weights in and out instead of running out of memory, at some cost in speed.

### Which model gets the card

Only one of the two fits. ACE-Step holds about 7.9 GB of the 8 GB card and Qwen
needs 3 GB, so whoever loads second pages — and paging was expensive: on this
machine a lyric request took **3m42s** that way.

The lyric model now loads only after the song model has been released, which
brings the same request to **10.5s**, and the song model is reloaded for the
render (~40s). `LYRICS_DEVICE=cpu` writes the words in system RAM instead, for a
machine whose card cannot be used at all.

`SONG_WARM=1` loads the song model at startup, so the first compose does not also
pay for the load. The launcher sets it; on its own the service answers health
immediately and loads on first use.

One launcher starts this service: `tools\start_services.ps1`, which the logon
entry, `songsvc://` and `start.cmd` all run. It starts the process at
`AboveNormal` priority on purpose — the minute after a boot is a race between
torch importing, these weights paging in and the site fitting its own models over
12 000 videos, and the warm loses that race at normal priority.

### Singing the lyrics in a recorded voice

`POST /api/song/voice` is the route behind **Sing the lyrics**, and behind
**Generate** and **Song designer** once a phrase is fitted. A recording is the
request body and the query string carries the prompt, length and the tone half of
the voice fit (`lowpass`, `room`). Each stage is a status the page polls:

| Status | What is happening |
| --- | --- |
| `writing` | the lyric model writes the words (skipped when the designer sends its own) |
| `loading`, `generating` | ACE-Step renders the song **with its own sung vocal** |
| `separating` | `demucs` splits that render into a vocal stem and a backing stem |
| `mixing` | the vocal is mapped (room only) and laid back over the backing |

The model sings; your recording is a measurement. Both pitches are still measured
and reported - the recording's and the sung vocal's - but the difference is **not
applied**: the model sings its own melody over its own backing, so its vocal is
already in the song's key, and a fractional shift moves the words off the music. One
render came out at **-5.5 semitones** that way, and on that job's own separated
vocal the shift put **+7.4 dB** into 60-120 Hz and took **5-6 dB** out of 250 Hz-3
kHz: a soft, muffled, low voice under the track. The number it matched on was not
real either - pyin's median on that stem was **70.0 Hz**, exactly its search floor,
against an autocorrelation of 46 Hz and a 90th percentile of 233 Hz. The log line is
now `pitch: recording … Hz, model vocal … Hz, no transpose (already in key)` and the
map carries the room only.

The shift machinery is still in `_map_vocal` and would hold the formants if it were
ever called with a non-zero value - phase vocoder for pitch and texture, then the
source's own spectral envelope divided back over the result (cepstral lifter 2 ms,
gain limited to ±18 dB a band). Measured back when it ran on a +3 semitone shift:
F1/F2/F3 moved ×0.993/×0.966/×0.976, against ×1.205/×0.915/×0.978 for the
uncorrected vocoder, and spectral flatness came out 23% *below* the source's - which
is why it was used rather than a WORLD resynthesis (WORLD held the formants at
×1.019/×0.986/×0.995 but measured +281% flatness, a grain or grind). Nothing you said into the microphone is in the track and no voice is
cloned: the vocal is ACE-Step's own, separated from the music, mapped, and laid back
**on the render itself** - see the next section - so the balance is the model's own.
No lead gain is applied on this path: `LEAD_DB` and `MAX_LEAD_GAIN` only drive
`_layout_vocal`, the route that lays a recorded phrase over an instrumental. The one
thing that moves the level under a word is `DUCK_UNDER_VOICE` (15%), and it scales the
whole mix - voice and music together - by **-1.41 dB**, so it cannot change the ratio
a listener hears.

Measured from the separated stems of the twelve renders in `build/stems/htdemucs`, at
the frames where the voice is sounding, the voice sits **-5.9 to +1.7 dB** over the
music, mean **-0.4 dB**; the mix is **1.41 dB** lower under a word in every one of them
(the duck). That is the honest number: the model does not always put its own voice on
top, and the mix no longer changes that.

Re-measure it after any change to the mix with `tools/check_mix_balance.py` at the
repository root. It reads `build/stems/htdemucs/<song>/{no_vocals,vocals}.wav`,
which `demucs` writes from a render in this service's logs:

```
<service-venv-python> -m demucs --two-stems=vocals -n htdemucs -o build/stems logs/song_<id>.wav
<service-venv-python> tools/check_mix_balance.py
```

Add `--render <song.wav>` and it also rebuilds the mix through `_mix_separated` and
compares it with `render * (1 - duck * gate)`, exiting nonzero if anything else - a lead
gain, or a rebuild from the stems - has come back into the path. Measured on the render
whose stems are still on disk, that rebuild matched to **3.1e-05**.

The finished song is the **render itself**, not the stems re-summed. Rebuilding it as
`no_vocals + vocal` cannot be exact - the two stems are a mask of the render rather than
the render (their envelopes correlate **0.59** with it, and demucs writes them at
**44.1 kHz** against a 48 kHz render) - so wherever the split is imperfect the sum
cancels. Measured against the render at 1.5 ms resolution, that was **80 dips** up to
**12.7 dB** deep; with the render as the base and only the mapping's difference added it
is **1 dip at -6.0 dB**. The vocal is also mapped per channel rather than folded to mono:
the fold lost 6.4% of one stem by more than 6 dB, up to 17.2 dB, because the model's
vocal is wide.

The **room** is a send, and a quieter one. It is a 0.45 s decaying noise tail, RMS-matched
to the voice; crossfaded as `(1-wet)*dry + wet*tail` it cut the dry voice and cancelled
against it - at the page's lo-fi 0.26 that was the quiet rattle heard as a dip in the
chorus. Adding that tail over the song leaves dips below the render by wet:

| wet | dips below the render (1.5 ms, > 6 dB) |
| --- | --- |
| 0.26 | 46 |
| 0.15 | 9 |
| 0.08 | 0 |

So the dry voice is never attenuated by the room, and the page's 0..1 room is read as a
fraction of that send (`ROOM_SEND` 0.30: 0.26 becomes 0.078).

The same write path treats the **sub**. It is close to a sine and it held most of the
song: on a 180 s render the bass pitch was 63 Hz with its second harmonic **13.6 dB**
down, and **20-120 Hz carried 60% of the power** - heard as a low held tone under the
track rather than as a note. `_fix_low_end` high-passes at **40 Hz** (4 poles: 35 Hz down
5.8 dB, 60 Hz untouched, which took 20-32 Hz from -16.2 dB of the song to -31.6) and
then holds 20-120 Hz to **-6.0 dB**, a quarter of the song's power, with a shelf at
160 Hz. The shelf is stepped and **re-measured** rather than trusted: a shelf at 160 Hz
does not deliver its full depth at 60-120 Hz, and the first pass of -2.4 dB moved the
share only 1.4 dB. The trim is limited to 8 dB, and a render that is not sub-heavy is
left alone. Measured with the shipped helper (`build/measure/test_sub_fix.py`), one method for
every band, on the newest render and on its mix. The figures are dB as a share of the
song's own power:

| band | render before | render after | mix before | mix after |
| --- | --- | --- | --- | --- |
| 20-32 Hz | -16.2 | -31.6 | -17.2 | -30.7 |
| 32-45 Hz | -5.7 | -13.3 | -6.9 | -12.9 |
| 45-60 Hz | -9.4 | -13.5 | -10.3 | -12.9 |
| 60-120 Hz | -5.5 | -7.5 | -6.8 | -8.2 |
| 80-120 Hz | -11.1 | -11.6 | -12.2 | -12.7 |
| 20-120 Hz | -1.6 | -5.7 | -2.8 | -5.9 |
| 120-250 Hz | -11.2 | -7.4 | -10.1 | -8.1 |
| 250 Hz-2 kHz | -7.0 | -3.2 | -4.9 | -2.9 |
| 2-5 kHz | -17.8 | -14.6 | -15.8 | -14.2 |
| 5-16 kHz | -17.1 | -14.0 | -15.4 | -13.8 |

The sub band went from **68.6% of the power to 26.9%** on the render (shelf -8.0 dB,
the limit) and from **52.5% to 25.5%** on its mix (-4.4 dB). Nothing above the shelf's
corner is filtered - those rows rise only because the sub fell and the shares are
relative - so the top is not dulled by this: 5-16 kHz moves from -17.1 to -14.0 dB as a
share, 3.1 dB louder against the track it sits in.

Every render logs the numbers it measured - `sub check: 20-120 Hz held -2.1 dB of the
song (61%), now -5.7 dB (27%) after -8.0 dB under 160 Hz`. The
measurement that decided this: nothing under 6 kHz is a fixed tone - the stillest
1/24-octave band swings **19.7 dB** across the song - so there is nothing to notch, and
the sub is the bass itself, near-sine and 13.6 dB above its own second harmonic.
`build/measure/test_sub_fix.py` at the repository root runs the shipped helper over
renders on disk and prints every band before and after; `hum_scan.py`, `hum_persist.py`
and `sub_check.py` are the measurements above.

The write path also closes a **gated tail**. The model does not always fill the length
it was asked for: six 59.9 s requests truly ended at 55.3-59.9 s, and four of them
ended in a burst pattern (10-120 ms of level, 10-120 ms at about -60 dB) rather than
a fade, heard as a stutter before the stop. `tailfix.py` - called from `_write_samples`
before the peak lift - finds the last continuous block, fades 300 ms into it and
writes silence after it; a take that already runs to the end of the file is left
alone. `build/measure/test_tailfix.py` at the repository root runs it over every
render on disk.

`demucs` is installed by this service's `install.py` — it is in `models.json`, so a
fresh install covers it. The first separation downloads the htdemucs weights
(about 80 MB). `voice_service` is no longer called by this route; it keeps its own
endpoints and its own README.

### Reaching this from the deployed page

Mixed content is not the obstacle it looks like: browsers treat loopback as
potentially trustworthy, so an `https://` page may call `http://127.0.0.1:8090`.
What Chrome adds is a private network preflight, and this service answers it in
an `after_request` hook with `Access-Control-Allow-Private-Network: true`, plus
the usual `Access-Control-Allow-Origin` and a 204 for the JSON preflight.

That is why the deployed page can render songs with no tunnel at all. The tunnel
the launcher starts is only for reaching the model from a *different* device,
where `127.0.0.1` means the phone rather than this machine.
