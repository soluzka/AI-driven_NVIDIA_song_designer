# internet

A video search engine that works over its own library: it searches a local SQLite
index of scraped videos, learns from the links you click, asks each listing which
genre it belongs to, and downloads what you want. Two ways in from one codebase:

- **desktop app** — Flask + Socket.IO + SQLite on your machine, live search,
  progress bars, yt-dlp downloads;
- **static site** — the same page served by a Cloudflare Worker from `public/`,
  searching a bundled index in the browser, with downloads handled by a small
  separately hosted service.

## The pieces

| Piece | What it does |
| --- | --- |
| `app.py` | Flask app: serves `public/`, socket stack, voice transcription |
| `ai_engine.py` | Scraper, SQLite/ML index, ranking, discovery worker, downloads, routes |
| `genre_model.py` | Infers a genre per video by questioning the listing |
| `public/` | The UI (`index.html`, styles, `config.js`, `data/videos.json`) |
| `tools/export_index.py` | Refreshes the committed index and precomputes genres |
| `worker.js` + `wrangler.jsonc` | Cloudflare Worker: static assets, Workers AI voice, download and song proxies |
| `downloader_service/` | Small Flask container that runs yt-dlp for the static site |
| `song_service/` | The local lyric and music models (GPU, 8090) |
| `tools/start_services.ps1` | The one launcher: downloader, song service, site, tunnel, publishes the address, keeps `songsvc://` registered |
| `tools/install_autostart.ps1` | Adds the logon entry and starts the watchdog |
| `tools/register_protocol.ps1` | Registers `songsvc://` for the site's Start the AI button |
| `start.cmd` / `start.ps1` | One click for the launcher, then opens the site |
| `data/video_index.db` | Committed snapshot of the index |
| `build/` | The check scripts — tracked; their output is not |

### The `build/` folder

The scripts are tracked so the checks move with the repository. What a build
*generates* stays local (`ai_engine/` is PyInstaller's 314 MB work folder,
`wrangler-tool/` the portable Node with a 103 MB `workerd.exe` — GitHub refuses
files over 100 MB), as do captured logs, test audio and JSON payloads.

| Script | What it checks |
| --- | --- |
| `deploy_and_check.ps1` | Deploys, then confirms the new version serves the page |
| `verify_live.ps1` | The live page holds this build, and the repo is clean |
| `asset_check.ps1` | Each `public/` file is served with the right content type |
| `service_state.ps1` | Both local services, with the GPU answer from `/health` |
| `autostart_status.ps1` | Startup entry, `songsvc://` target, services now |
| `race_test.ps1` | Two launcher runs leave exactly one song service |

They resolve the repo root from their own location. `race_test.ps1` restarts the
song service; run it when nothing is rendering.

### The index on the site

The 8,724 videos live in a Cloudflare D1 database (`video-index`, bound as `DB`),
and `/api/search` answers from it. The page asks for sixty rows per search
instead of downloading the 3 MB `public/data/videos.json`:

```
GET /api/search?q=dubstep+mix&limit=60&log=1
{ "query": "dubstep mix", "total": 69,
  "library": { "total_videos": 8724, "total_searches": 114, "total_clicks": 55 },
  "videos": [ { "u": "…", "t": "…", "th": "…", "c": 1 } ] }
```

Every word has to appear in the row, so two words narrow rather than widen, and
the order prefers what people clicked. `log=1` is sent by the page only: a real
search is written to `search_history`, a crawler following the URL is not.
`public/data/videos.json` stays as the fallback for a copy of the page with no
database behind it. Push the SQLite file up after a change:

```
python build/export_d1.py
build/wr.ps1 d1 execute video-index --remote --file build/d1_import.sql
```

`build/wr.ps1` is wrangler via the portable Node in `build/wrangler-tool`, which
is how this project deploys without a global install. A `deploy` through it also
measures the deployed page's voice fit and fails on a rumble (see
[The low end has a check](#the-low-end-has-a-check)); `--no-check` deploys
without that, `--config` (the pool worker) never runs it.

## Run it locally

```
uv venv .venv --python 3.11 --seed
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python app.py
```

Then open <http://127.0.0.1:5001>. `PORT` overrides the port, except `PORT=0`,
which falls back to 5001 so the server cannot end up on a random port.

### What `requirements.txt` installs

None of it is the music model — that lives in `song_service/` with its own
manifest.

| Group | Packages | Why |
| --- | --- | --- |
| Web framework | `Flask`, `Flask-Cors`, `Flask-SocketIO`, `python-socketio`, `python-engineio` | The site, its socket stack and the live search stream |
| HTTP clients | `aiohttp`, `requests` | Scraping the source sites |
| Video downloads | `yt-dlp`, `yt-dlp-ejs`, `curl-cffi`, `imageio-ffmpeg` | yt-dlp plus the impersonation and ffmpeg it needs |
| HTML parsing | `lxml` | Listings are parsed with lxml; there is no BeautifulSoup |
| Ranking | `numpy`, `scipy`, `scikit-learn` | The ML index behind search order and genre inference |
| Voice search | `faster-whisper` | Local transcription; falls back to the Windows recogniser without it |
| Build | `wheel`, `setuptools`, `pyinstaller`, `pyinstaller-hooks-contrib` | Only needed to freeze the desktop executable |

**Install into a virtual environment, always.** A partially installed global
interpreter makes PyInstaller freeze an empty package, which produces
`cannot import name 'emit' from 'flask_socketio'` in the frozen app. Check the
socket stack before building:

```
.venv\Scripts\python -c "from flask_socketio import emit, SocketIO"
```

Optional desktop build:

```
.venv\Scripts\python -m PyInstaller --clean --noconfirm ai_engine.spec
```

That freezes `app.py` into `dist/ai_engine.exe`, bundling `public/` and
`cinema.ico`. It runs the same Flask app on the same port; it does **not** bundle
the song models, which stay in `song_service/` and are reached over HTTP exactly
as the browser does.

## How searching works

- **Matching has no cap.** A query matches titles, descriptions, tags and
  sources; results stream in batches of 200 with `search_completed` marking the
  end.
- **Ranking** combines term scores with click history, so links you use rise.
- **Discovery** runs in one background worker after a search: it scrapes the
  source sites for that query, stores new videos and reports how many were added.
  One worker keeps a burst of searches from saturating the machine.
- **The index grows by itself.** The live database is `../video_index.db`, next to
  the project folder; `data/video_index.db` is the committed snapshot, and an
  empty live database is seeded from it on startup (~20 ms; a populated one is
  left alone). Building the index costs 0.7 s, and the genre model's fit over the
  same 8,724 documents (11.8 s) runs on a daemon thread.

### Genres

`genre_model.py` does not keyword-match. It asks each listing a set of questions —
does it name a scene or subgenre, which artists, labels, bass, drums, vocals,
instruments, place, mood, tempo — and weights the answers by how rare those
phrases are across the library (IDF), so a distinctive word counts more. Matching
is on word boundaries, so `dub` does not match `dubstep`, and a veto list of
workshop/catalogue words stops non-music pages being labelled unless a strong
music cue is present.

The veto list is split by how entries match: `manufactur` is a stem matched
anywhere; everything else matches on word boundaries. Matched as plain substrings,
`trim` was found inside Albanian `ushtrimeve` and `pipe` inside `Pipet`, refusing
two real dubstep tracks and a reggae one. The material sense of metal is named
too (`metal detector`, `detectorist`, `oxidation`/`oxydation`); re-exporting after
that change gives 1,542 genres → 1,541 and all six changed rows are right.

The percentage on a card is **decisiveness, not a probability**: it says how
clearly the questions were answered. `why?` shows the evidence. Inference is
~1.5 ms per video; the startup fit is ~8 s on a large index, on a daemon thread.

## The static site (Cloudflare)

`public/` is plain HTML/CSS/JS with no build step. The Worker serves it and
answers three things itself: `/api/transcribe` (Workers AI
`@cf/openai/whisper`), `/api/download` and `/download_file/...` (proxied to the
download service), and everything else (static files, SPA fallback to
`index.html`). The page first tries Socket.IO; if it cannot connect it searches
the bundled index in the browser, so the site works with no backend at all.

### When the internet is off

A strip appears above the header the moment the browser loses its connection:

```
●  You are offline. Search, downloads and the pool need a connection;
   a song can still render while this PC's model is running.
```

It is driven by `navigator.onLine`, re-read on `online`/`offline` and every
twenty seconds, because a link that lost its route can keep reporting `true`.
The model chips go to their own states (`Nothing is running`, `AI not connected
yet · warming`) and `Start a GPU` appears only when the pool is empty.

| Thing | Offline |
| --- | --- |
| Loading the deployed page | no — served from Cloudflare |
| The same page from this PC | yes — the desktop app serves it on `127.0.0.1:5001` |
| Rendering a song on this PC's model | yes — the song service is local on 8090 |
| Searching the library | yes from the local app, no from the Worker (D1) |
| Downloads, the pool, `Start a GPU`, voice search | no — all remote |

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\start_services.ps1
# then open http://127.0.0.1:5001  (the site, from public/)
```

The song service on 8090 keeps answering `/health`, so the header still shows
what the card is doing. A Kaggle kernel started from the site follows the same
rule: Kaggle turns the network off on API-pushed runs, so the offline boot
(`public/song_gpu_offline.py`) reads mounted datasets and says `no internet in
this kernel, which is expected here`.

Deploy after refreshing the index:

```
.venv\Scripts\python tools\export_index.py   # DB snapshot + videos.json (+ genres)
npx wrangler deploy                          # or let Workers Builds deploy on push
```

The exporter copies the live database over the snapshot, so it refuses to run
when the live database is much smaller than the snapshot it would replace: on
this machine an empty 36 KB database once replaced a 4.2 MB snapshot. The rule is
90% — it counts both sides first and exits with both numbers under nine tenths of
the snapshot's. A deliberate cut passes `--allow-shrink`.

**Re-exporting is not what labels the cards.** It reproduces 1,542 of 8,724 with a
genre because that is what the model returns; of the other 7,182, only 41 contain
any genre phrase at all and all but about three are non-music pages the model
vetoes on purpose. `unclassified` is the honest answer; only a better classifier
changes it.

Workers Builds deploys on push to `main` (`bun install`, then wrangler).
`package.json` declares `socket.io-client`, but the bundle is committed at
`public/vendor/socket.io.min.js`, so the page needs no CDN.

**The static index is a snapshot.** Results and genres on the deployed site are
frozen at export time; scraped videos appear after running the exporter again and
redeploying. Genres must be precomputed because the browser has no Python.
Live results are labelled by `downloader_service`, which imports the same
`genre_model.py` and puts a `genre` on every entry `/api/discover` and `/api/top`
return. A listing whose words name nothing stays unclassified: "Midnight Rain has
a bassline" is `None` while "Aggressive Metal Riff in Drop D" is `rock / metal`
at 0.77.

## Songs and lyrics

The song AI is **local only**: ACE-Step is a 3.5B checkpoint that needs CUDA, and
Workers AI has no music model, so nothing on Cloudflare renders a track. Workers
AI does handle voice search, the one model that runs in the site.

| Route (on 127.0.0.1:8090) | What it does | Model |
| --- | --- | --- |
| `POST /api/lyrics` | writes the words: the answer, or `202 {token}` while it writes | Qwen 2.5 1.5B Instruct |
| `GET /api/lyrics/<token>` | the words, once that write has finished | — |
| `POST /api/song` | sings those words | ACE-Step v1 3.5B |

Every render is written by the service itself instead of torchaudio (the
installed torchaudio dropped the soundfile backend, so generation died after the
whole diffusion had run), and that write path masters as well: four poles of
highpass at **40 Hz**, above the swell the model left under the bass, leaving
60-120 Hz where it was. It does not shift pitch: the renders sit low because
their bass is written low, which is a question for prompt and key.

A song with a voice is the **render itself** with only the mapping's difference
added, not the demucs stems re-summed: the stems are a mask of the render
(envelopes correlate 0.59, and demucs writes 44.1 kHz against a 48 kHz render), so
a rebuild cancels wherever the split is imperfect — **80 dips up to 12.7 dB deep**
at 1.5 ms resolution, heard as a quiet rattle. With the render as the base: **1
dip at -6.0 dB**. The **room** is a send that never attenuates the dry voice, at a
level measured to stop cancelling (0.26 wet left 46 dips, 0.15 left 9, 0.08 left
0; `ROOM_SEND` 0.30 maps the page's 0..1 room into that range).

The same path treats the model's **sub**, heard as "a hum like a low tuned
guitar". It is close to a sine and can hold much of the song: on
`build/measure/hum/new_render_3min.wav` (3:00, 24 steps, token `fc863e1b…`) the
bass pitch has a mode of **52.7 Hz**, holds within 6 dB of itself on **36%** of
the song's 0.68 s frames, and stands **12.9 dB** above the whole 20-120 Hz band.
Nothing under 6 kHz is a fixed tone — the stillest 1/24-octave band swings
**28.5 dB** across that song, against under 6 dB for a true hum — so there is
nothing to notch; the sub is the bass. `_fix_low_end` high-passes at **40 Hz**
(4 poles), holds 20-120 Hz to **-6.0 dB** of the song, with a stepped, re-measured
shelf at 160 Hz. Measured across the whole spectrum (`build/measure/test_sub_fix.py`)
on that render: **20-120 Hz goes from 25.5% of the power to 20.3%**, 20-32 Hz
drops **10.4 dB** (-24.7 to -35.1) and 32-45 Hz 2.3 dB, while **60-120 Hz moves
0.2 dB** and 45-60 Hz 0.5 dB. Nothing above the corner is filtered either:
120-250 Hz, 250 Hz-2 kHz and 5-16 kHz each move 0.3 dB, and that rise is a share
because the sub fell. The full band table, mono fold, flicker, rattle and tail are
in `song_service/README.md`; every render logs
`sub check: 20-120 Hz held ... now ... after ... dB under 160 Hz`.

### The tail

The same write path closes the model's **gated tail**. Across six 59.9 s requests
the audio truly ended at **55.3, 57.9, 58.2, 59.84, 59.85 and 59.9 s**, and in four
the last half second to 4.6 s was not a fade but a burst pattern — 10-120 ms of
level alternating with 10-120 ms at about -60 dB — heard as a **stutter before the
song stops**. `song_service/tailfix.py` finds the last continuous block, fades
300 ms into it and writes silence after the bursts, before the peak lift. A render
whose audio already runs to the end is returned untouched (`total - end < 0.15 s`).

Both models pick their device themselves: the lyric model runs on CUDA when torch
sees a card, in system RAM when it cannot. On the GPU it loads only after the song
model is released, because 7.9 GB of ACE-Step and 3 GB of Qwen do not fit in 8 GB
together — measured, a lyric request went from **3m42s to 10.5s**.

A write is a job, not one long request: Cloudflare answers **524** to a request
that has sent nothing for 100 seconds, which is what a cold lyric model looks like
from the page. So `POST /api/lyrics` waits `LYRICS_INLINE_SECONDS` (40) and then
returns `202 {"token": …}`; the page polls `GET /api/lyrics/<token>` every two
seconds. The words are the model's own either way — a slow write is never
replaced with stand-in lines.

```
GET /health → {"ok": true, "device": "cuda", "gpu": "NVIDIA GeForce RTX 4060 Ti",
 "vram_gb": 8.0, "loaded": true, "lyrics_loaded": true, "cpu_offload": true}
```

The page calls the service on `127.0.0.1:8090` and falls back to the sampled band
in the browser when it is not running. It also stops asking the Worker for the
song model after the first 501.

That band's guitars were tried with a bit-crush and taken back out: a quantising
shaper raised the share above 6 kHz from 0.041 to 0.070 and above 12 kHz from
0.0047 to 0.011, heard as an interference rattle; oversampling at 2x and a
low-pass after the shaper both left the alias band, so the crush is gone rather
than turned down.

### Installing the AI packages

The song service keeps its own environment, separate from the web app's venv,
because its dependencies are heavy and its CUDA wheels must not leak into Flask.

```
uv venv song_service\.venv --python 3.11 --seed                     # once
song_service\.venv\Scripts\python song_service\install.py --check   # report, change nothing
song_service\.venv\Scripts\python song_service\install.py           # packages, then weights
```

`install.py` reads `song_service/models.json`, the one place versions are
declared — a bump is a manifest edit, not a hand-run pip command.

| Invocation | What it does |
| --- | --- |
| `--check` | Prints which packages import and which weights are on disk |
| `--packages` | pip-installs from the manifest: `torch`/`torchaudio` from the **cu126** index, `transformers==4.57.6`, `huggingface-hub==0.36.2`, ACE-Step from git, flask, flask-cors, soundfile, librosa, hf_transfer |
| `--weights` | Downloads ACE-Step v1 3.5B (~7 GB) and Qwen2.5-1.5B-Instruct (~3.1 GB) into `song_service/models/` |
| *(no flag)* | Both, packages first |

- **No CUDA toolkit install.** The NVIDIA driver (560+) provides the runtime and
  the cu126 wheel carries its own CUDA libraries. PyPI's default Windows `torch`
  is CPU-only, which is why the index is declared per package.
- **Weights are resumable.** ~10 GB, gitignored under `song_service/models/`; a
  partial folder is measured and continued.
- **Verify the GPU** before blaming the app:
  `.venv\Scripts\python -c "import torch; print(torch.__version__, torch.cuda.is_available())"`
  should print `2.14.1+cu126 True`.
- **The web app's dependencies are separate.** Flask, the socket stack,
  faster-whisper and yt-dlp come from the root `requirements.txt`; the root app
  needs no torch.

## Starting the stack

`start.cmd` (which runs `start.ps1`, a thin wrapper around
`tools/start_services.ps1`) brings everything up in order and leaves anything
already running alone, so double-clicking twice is safe:

1. `downloader_service/app.py` on **8080** — yt-dlp, Top 100, the player;
2. `song_service/app.py` on **8090** — the lyric model and ACE-Step on the GPU;
3. `app.py` on **5001** — the site on this machine;
4. `cloudflared` — a Quick Tunnel giving the machine a public `https` address;
5. the browser, at <http://127.0.0.1:5001>.

The tunnel's address is printed **and published to the site**, so nothing has to
be pasted into a phone:

- `POST /api/song/endpoint` with `{"url": "https://….trycloudflare.com"}` and the
  header `x-song-key: <value of SONG_ENDPOINT_KEY>` stores it in the `STATS` KV
  namespace under `song_api`; without the header it answers **403**.
- `GET /api/song/endpoint` is public and returns the stored address, which the
  page reads on load.

The PC owns the tunnel and the key; the site only remembers the last address it
was told. If the address looks stale, re-run the launcher and reload the page —
the only step a phone needs.

`tools\start_services.ps1 -Restart` stops every role by pid and starts them
again, which is what a reinstall needs because a running service keeps the code
it loaded at start-up. `-NoTunnel` skips the tunnel for local-only work.

### One public address, pointing at 8090

The singing and voice-mapping work lives on the song service (`POST
/api/song/voice`); the voice service on **8095** is called by `song_service` over
loopback (`VOICE_SERVICE` in `song_service/app.py`). Nothing is tunnelled to 8095,
and nothing should be: a second tunnel would be a second public door to a model
that only needs to hear from this machine.

```
PowerShell -File tools\bin\cloudflared.exe tunnel --config tools\bin\quick-tunnel.yml ^
  --url http://127.0.0.1:8090 --no-autoupdate --logfile tunnel.log --loglevel info
```

To check the route end to end:

```powershell
$tunnel = (Invoke-RestMethod 'https://browser.bpiersing.workers.dev/api/song/endpoint').url
Invoke-RestMethod "$tunnel/health" | Select-Object ok, loaded, device
# a body too small to be a recording is refused before any job is queued, so this
# proves the singing route is reachable without spending ten minutes on the GPU
try { Invoke-RestMethod -Method Post -Uri "$tunnel/api/song/voice?prompt=test&seconds=20" -Body ([byte[]](1..8)) }
catch { $_.ErrorDetails.Message }   # {"error":"Send the voice recording as the request body."}
```

If `/health` answers but the last call 404s, an old `cloudflared` is holding a
tunnel pointed at the wrong port; stop it and re-run the launcher, which
republishes the address.

> A `config.yml` in `~/.cloudflared` disables Quick Tunnels: its ingress rules win
> over `--url` and the catch-all answers **404**. The launcher therefore runs
> with `--config tools/bin/quick-tunnel.yml`.

`cloudflared.exe` is gitignored (53 MB), so a fresh clone has none. The launcher
fetches it once at a pinned version (`2026.10.7`) into a `.download` file first:
Windows' signature check must report **Valid** *and* name Cloudflare, or the file
is discarded and the tunnel skipped with a line saying so. `latest` is
deliberately never used.

#### The endpoint key never goes to GitHub

`build\song_endpoint.key` is the only thing that can tell the site which tunnel
address is a real machine's. It is listed in `.gitignore`, `git ls-files` does
not contain it, and `git log --all -- build/song_endpoint.key` is empty, so no
clone, fork or pull request has ever carried a copy.

It is generated on the machine that owns it, read from disk by the launcher and
the song service, and written into the Worker's secret store (`SONG_JOIN_KEY`) by
`tools\set_kaggle.ps1` — never into a source file:

| Shipped file | What it carries | Where the key comes from |
| --- | --- | --- |
| `public/song_gpu.ipynb`, `deploy/colab/song_gpu.ipynb`, `public/song_gpu_cell.py` | `JOIN_KEY = ''` | pasted by whoever runs the cell |
| The Kaggle kernel `/api/gpu/start` pushes | the key, written in | the Worker secret `SONG_JOIN_KEY` |
| The page | nothing | it asks the song service beside it (`/api/pool/join`) |

### Starting the AI from the site

The page is on Cloudflare, the model is on your PC, and a browser tab cannot
start a program. Windows can, so the header carries a **Start the AI** button that
navigates to `songsvc://start`; the scheme is registered for the current user
(HKCU, no administrator) and points at `tools/start_services.ps1`. The launcher
then starts the downloader, the song service with `SONG_WARM=1` and the tunnel,
and the page polls `/health` — every five seconds while off or warming, every
thirty once up — until the button reads **AI ready**.

The badge beside it reports what the render will use, read from `/health`:

| `/health` says | Badge | Button |
| --- | --- | --- |
| `loaded: true`, `device: "cuda"` | `CUDA · GeForce RTX 4060 Ti`, green | `AI ready` |
| `loaded: false`, `warming_seconds: 95` | `CUDA · warming 1m 35s`, amber | `AI warming…` |
| nothing answers, but it did in the last four minutes | `CUDA · busy loading` | `AI warming…` |
| `device: "cpu"` | `CPU only`, amber | `AI ready` |
| nothing answers and nothing recent | `AI off` | `Start the AI` |

Measured on 13 Nov 2026: **51 seconds** from process start to `loaded: true` with
nothing competing. A five-to-ten-minute wait is a restart, not the model — the
probe allows twenty seconds (a three-second one once called a busy service dead
and started a rival copy), and the song service runs `AboveNormal` so the site
fitting its own models does not starve it. A compose pressed during the warm is
not dropped: the page waits up to three minutes.

![The header: Start the AI beside Song designer, and the CUDA badge reading the card from /health](public/gpu-chip.png)

| Trigger | How |
| --- | --- |
| Clicking **Start the AI** | `songsvc://` → `tools/start_services.ps1` |
| Logging in to Windows | `HKCU\...\Run` key and Startup entry, from `tools/install_autostart.ps1` |
| A service dying while the PC stays on | The watchdog re-runs the launcher within two minutes |
| Running it by hand | `tools/start_services.ps1` (safe to repeat) |

The launcher repairs the `songsvc://` registration on every run — missing, or
pointing at a folder that has moved — and writes one line per run to
`build\launcher_trace.txt`, which is how "the site started it" is checked rather
than assumed.

#### Is the autostart installed? Is it right?

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File build\autostart_status.ps1  # triggers, watchdog, services
(Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'AI song services').'AI song services'
Test-Path (Join-Path ([Environment]::GetFolderPath('Startup')) 'AI song services.cmd')
(Get-CimInstance Win32_OperatingSystem).LastBootUpTime      # was there a run at logon?
Get-Content build\launcher_trace.txt -Tail 5
```

A boot on 13 Nov 2026 read: boot `21:25:34` → launcher ran `21:26:10` → watchdog
`21:26:25` → tunnel published → `/health` `loaded: true` around `21:52`. No line
within a minute of the boot time means the entry did not run; re-install it:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\install_autostart.ps1
```

That is safe to repeat: it writes the same two entries and reuses a running
watchdog. None of this is Task Scheduler, because registering a task returns
*Access denied* without administrator rights: the logon entry is a Run key plus
the Startup folder entry, and the watchdog is the launcher with `-Watch`, sleeping
two minutes at a time and restarting a service whose port has nothing behind it —
a port with a process is left alone, so a service still importing torch is never
started twice. Verified on 8 Nov 2026: the song service was killed and `/health`
answered again 18 seconds after the watchdog noticed.

It cannot start anything on a machine without the scheme (your phone), start a
local program without a click (browsers only allow `songsvc://` from a user
gesture), or render a song without the PC.

### Reaching the machine from a phone

Nothing has to be pasted: the launcher publishes the address to the Worker after
every start and the page asks for it on load. The address is also kept per browser
under `songApi` in `localStorage` when someone pastes it into **Connect my model**;
that value wins over the published one, and pasting an empty value forgets it.

To give the deployed page a default instead, set `SONG_API` on the Worker — in
the dashboard (Workers & Pages → `browser` → Settings → Variables and Secrets) or
in `wrangler.jsonc`'s `vars` block, applied by the next deploy. The value is the
**origin only** (`https://name.trycloudflare.com`, no trailing slash, no path),
because the Worker and the page append `/api/song` themselves. It is only a
fallback for a site that has never been told about a tunnel; leave it unset unless
you run a named tunnel with a fixed hostname.

Cross-origin calls already work: `song_service` sends `Access-Control-Allow-Origin`
and answers the JSON preflight with 204.

### What the tunnel carries

The tunnel points at **8090**; that service forwards to the downloader on
loopback, so one address covers everything a phone cannot reach.

| Path | Reaches | Used for |
| --- | --- | --- |
| `/api/song`, `/api/song/<token>`, `/song_file/<token>` | the song service | composing, progress, saving |
| `/api/lyrics`, `/health` | the song service | lyrics, the badge |
| `/api/top` | downloader **8080** | the Top 100 button |
| `/pc/<anything>` | downloader **8080** | `api/play`, `download_file`, progress — TikTok and Dailymotion playback |

yt-dlp has to run on this machine (YouTube refuses a datacentre address), so the
file is fetched here and streamed back; `Range` is forwarded, which lets the
player seek. Without `/pc` a phone can play YouTube, whose embed is remote, and
nothing else.

### If the address goes stale

A Quick Tunnel gets a new name whenever `cloudflared` restarts, so the stored
address can point at nothing: a phone says **AI off**, Top 100 says "the
downloader on this PC is not answering", a video will not start — while the PC is
fine.

1. Run `tools\start_services.ps1`. It publishes only an address that answers
   `/health` **through Cloudflare** — six probes ten seconds apart, because a
   fresh tunnel took about 40 s to become routable here — and when nothing can be
   verified it removes the dead address from the pool by name.
2. Check what the site holds: `curl https://<worker>/api/song/endpoint`.
3. Reload the page on the phone; the address is only read on load.

To publish by hand:

```
Set-Content -Path endpoint.json -NoNewline -Value '{"url":"https://name.trycloudflare.com"}'
curl.exe -X POST https://<worker>/api/song/endpoint -H "Content-Type: application/json" `
  -H "x-song-key: $env:SONG_ENDPOINT_KEY" --data-binary "@endpoint.json"
```

Use `--data-binary @file`: inline JSON through PowerShell's curl mangled it and
the site rejected the write. A named tunnel on a domain you own removes the
problem — one hostname that survives restarts — at the cost of
`cloudflared tunnel login` and DNS.

**The honest limits:** the tunnel is up only while the PC, the service and
`cloudflared` are running; Quick Tunnels have no uptime guarantee and are meant
for development; and anything holding the address can render songs on your GPU
and use `yt-dlp` through `/pc`, since none of it has authentication. Sleeping is
on that list — this PC sleeps after fifteen minutes on power.

### What the deploy needs

One Workers AI binding and no secrets: `wrangler.jsonc` declares
`"ai": {"binding": "AI"}`, which voice search uses on the free allowance.

```
npx wrangler deploy
```

Workers Builds does the same on push to `main`. Nothing in `song_service/` is
uploaded. No build step involves the model address — `SONG_API` is optional and
the page can take one at runtime, so pointing the site at a new tunnel never needs
a redeploy.

### ACE-Step cannot be installed on Cloudflare

ACE-Step is Python, PyTorch and a 3.5B checkpoint; Workers run JavaScript/WASM
only and need CUDA for that model. It cannot be packaged into the Worker, so the
only place it runs is `song_service/` on this machine.

## Downloads on the deployed site

Static hosting cannot run yt-dlp, so downloads need a real backend:
`downloader_service/` — Flask, yt-dlp and a bundled ffmpeg, its own short
dependency list.

```
POST /api/download          {"url": "..."}  -> 202 {"token": "...", "status": "queued"}
GET  /api/download/<token>                  -> {"status": "queued|downloading|processing|complete|error", "percent": ...}
GET  /download_file/<token>                 -> the video file, then the job is deleted
GET  /health                                -> {"ok": true}
```

Downloads are asynchronous because one outlasts most hosts' request timeout: the
page starts a job, polls for progress, then fetches the file.

The shortest route needs no hosting: the tunnel carries the downloader under
`/pc`, which makes **Top 100** and TikTok or Dailymotion playback work on a phone.

To run it on a host instead:

1. Deploy somewhere that runs Docker — Render's free tier works and `render.yaml`
   at the repo root is a ready blueprint (New → Blueprint → pick the repo).
2. Put its URL in the Worker's `DOWNLOAD_API` variable (in `wrangler.jsonc` or the
   dashboard). The Worker proxies `/api/download` and `/download_file/...` to it,
   so the page stays same-origin and needs no CORS.
3. Only if the page is hosted without the Worker, set `window.DOWNLOAD_API` in
   `public/config.js`.

### The honest limits

- **Cloud IPs get refused.** YouTube answers datacenter addresses with "Sign in
  to confirm you're not a bot"; the same link usually downloads from the desktop
  app on a home connection. `YT_COOKIES_FILE` can help; never commit that file.
- Free instances sleep when idle, so the first download after a pause waits a few
  seconds. `https://<worker>/api/health` says whether the service is configured,
  reachable and holding cookies.
- The service caps work so a public endpoint is not a free download farm:
  `MAX_HEIGHT` (720), `MAX_ACTIVE_JOBS`, `MAX_JOBS_PER_IP`.

## Voice search

The microphone button records a clip and sends it for transcription: locally
faster-whisper, falling back to the Windows recogniser; on the Worker, Workers AI.
Silence below a confidence threshold is ignored rather than turned into a query.
The microphone needs a secure page — https or localhost; a plain
`http://192.168.x.x:5001` is refused by the browser, which is the browser's rule.
## Voice fit

Press **Fit voice** next to Search, record one phrase, and the same phrase comes
back in the key and tone of the song the page has open. A card's **Remix** panel
makes that card the target; with nothing open it fits to a neutral A minor at
110 BPM. All of it runs in the tab: no model, no upload, and the words are never
read — only the sound moved and shaped.

The target line says what is known and what is assumed. A listing with no genre
and no key is not tuned at all: the phrase keeps its own pitch and the line says
`no genre was inferred for this listing and it gives no key, so the phrase keeps
its own pitch`, because the key it would be fitted to is chosen by the neutral
style rather than read from anywhere. With a genre, the key the remix proposes is
printed plainly. A take also remembers which song it was fitted to and says so
when another card is opened.

1. **Pitch.** The phrase is moved into the song's key, measured not by its median
   frequency but by **which pitch classes it sings**: a histogram over the phrase
   at a tenth of a semitone, weighted by frame loudness. An octave error in a
   frame — the usual pitch-tracker failure on a voice — leaves the class alone.
   The class the phrase spends most of its sung time on is shifted exactly onto
   the song's root; the song's scale can veto that move but never invent one (if
   leaving the phrase where it is puts more of it on the scale, it stays), which
   matters because F major and D minor are the same seven notes. Shifting is
   resample plus a phase-vocoder stretch, so the length is kept.

   Measured on synthetic melodies the chosen shift matches the key change for C#
   minor → D minor (+1), Eb minor (−1), E minor (−2), B minor (+3), A major → D
   major (+5), and is zero for a phrase already in key, for F major into D minor
   and G major into A minor. A tone shifted by +0.5, +1.5, +3, +5 and +7 (and
   downwards) lands within a cent of the arithmetic answer with its level kept
   (0.212 before and after at 200 Hz); the shift is fractional, so a phrase sung
   44 cents flat is moved 44 cents.

   The stretch advances each bin by its own measured frequency (searching for
   grain placement re-locked the waveform to the source period and cancelled the
   shift: a 160 Hz tone came back at 160.0 after a resample to 164.5). Only the
   spectral peaks advance; every other bin takes its peak's phase plus the
   difference it already had (identity phase locking), so harmonics move whole
   instead of smearing. Measured on the voice reference shifted 0.93 semitones
   against the dry take: energy within ±1 bin of a harmonic centre -4.76 dB
   unlocked → -3.77 dB locked (-3.92 dB dry); envelope depth 32.14 → 37.63 dB
   (35.57 dry); correlation with the dry envelope 0.853 → 0.968. On 40 s of real
   singing the hop-rate (86 Hz) modulation sits at or under the recording's own:
   0.4/0.2/-1.0 dB at +3/+5/+7, 2.9/0.6/0.7 dB at -3/-5/-7, against 2.3 dB dry.

   Every frame must be **periodic** before it counts: the correlation at the
   chosen lag has to reach 0.3 of that frame's energy, and a histogram with fewer
   than a fifth of its frames voiced returns no shift. Measured: the voice
   reference has 33 of 40 frames voiced (82%) and shifts −0.1 semitones into D
   minor; white noise has 0 of 40 and room tone 2 of 40, and both return zero for
   every key with the panel saying no clean tone was found. 80 Hz and 110 Hz tones
   come back as 81 and 110 Hz (a 1024-sample frame once called an 80 Hz tone
   498 Hz).

   Two ways the fit added frequency nothing was singing. Aliasing from linear
   interpolation (a 17 kHz component at +7 folded to 18.6 kHz, RMS 0.177 in and
   0.177 out) is now handled by a Hamming-windowed sinc (63 taps, −61 dB; the same
   tone returns at RMS 0.0001, 65 dB down, while 200 Hz keeps its level and 10 kHz
   passes at −1.5 dB), and the filter only runs when folding is possible — from +5
   (16.5 kHz) and +7 (14.7 kHz), since a voice's top is ~17 kHz sibilance. At +0.5
   the fitted bands are unchanged by the skip (10-20 kHz −36.3/−29.4 dB against
   −36.3/−29.3 with it); the filter costs 136 ms of a 431 ms shift on a 30-second
   take and skipping it holds the shift at 367 ms. The second was the drive: its
   lowpass sat *before* the waveshaper, so generated harmonics folded back into the
   band and 10-20 kHz went from flatness −21.2 dB to −17.6 dB. The ceiling now
   runs after the shaper too, and that band reads −29.3 dB, less noise-like than
   the dry take. Both lowpasses stayed, measured rather than assumed: removing the
   pre-drive one leaves −17.9 dB at +5 and −19.0 dB at +7 against −25.3 and −27.4
   with it.

   Band by band on the reference (share of total power / flatness, dB):

   | band | dry | fit +0.5 | fit +5 | fit −5 |
   | --- | --- | --- | --- | --- |
   | under 60 Hz | −36.8 | −38.3 | −34.7 | −36.2 |
   | 60-120 Hz | −23.4 | −20.6 | −22.7 | −11.9 |
   | 500 Hz-2 kHz | −1.2 / −7.9 | −2.2 / −7.2 | −2.7 / −7.0 | −4.7 / −6.1 |
   | 2-5 kHz | −16.2 / −2.9 | −17.0 / −2.3 | −14.4 / −5.2 | −13.7 / −1.9 |
   | 5-10 kHz | −21.3 / −4.0 | −19.5 / −3.6 | −22.4 / −2.4 | −19.1 / −4.8 |
   | 10-20 kHz | −36.9 / −21.2 | −36.3 / −29.3 | −35.0 / −25.4 | −49.0 / −19.1 |

   The low end needed its own work. A vocoder builds energy where the phrase has
   none — the lowest bins drift against each other and beat, heard as rumble.
   Three changes: bins below 45 Hz are zeroed before summing, the two highpasses in
   the tone chain run at Q 0.54 instead of Web Audio's default 1 (two cascaded
   corners at Q 1 put a 6 dB bump at 90-120 Hz — rumble made by the filter meant to
   remove it), and a two-pole lowpass sits in front of an upward resample. The
   vocoder's output under 60 Hz went from -33.2 dB to -51.1 dB. The rumble people
   still heard came from the drive: on a 220 Hz + 1 kHz tone the 80-120 Hz band
   measured -101 dB in the tone, -63.5 dB through the filters and **-35.9 dB once
   the 0.18 drive was added** (difference tones). Routing the low end around the
   shaper fixed that tone and not a real voice (partials at 420/500/620/700 Hz put
   the band back at **-30.2 dB**); two highpasses at the split *after* the shaper
   are what remove them — at 120/160/200/260/400 Hz corners the band reads -32.5,
   -39.1, -43.9, -49.7 and -59.0 dB. It now measures -74 to -76 dB on that case and
   -57 to -62 dB on the tone, and a 110 Hz fundamental keeps its band.

   What this cannot fix: the whole phrase is moved by one number, its median. A
   sung melody wanders — the reference's frames span ~130-150 Hz — so its median
   can sit between notes and notes still beat against a chord. Per-note correction
   would be a different feature.
2. **Tone.** Per-genre drive, room and a half-beat echo: `dub` and `ambient` are
   mostly space, `trap` and `riddim` almost none. The profile table is
   `FIT_PROFILES` in `public/index.html`. The room is generated, not sampled, but
   it is not raw noise: the tail is lowpassed twice with the cutoff closing from
   6 kHz to ~900 Hz as it decays, starts twelve milliseconds late so a consonant is
   never inside its own blur, and is highpassed at 140 Hz. Its level is set by
   `FIT_ROOM_POWER` with the convolver's normalise off, because normalise scales
   the impulse to equal power whatever its shape. Measured on speech at the wettest
   profile, the tail above 4 kHz is 4 dB lower than the old noise tail, the 1-4 kHz
   part 2 dB lower, and the phrase's envelope still matches the dry take at 0.88.

   **The voice is not filtered.** The phrase no longer goes through `profile.lp`
   before the presence peak (4.2 kHz on `lo-fi`, 5.2 on `dub`) nor through a split
   that drove the top half: on white noise through `lo-fi`, 5-8/8-12/12-16 kHz sat
   **-14.9 / -33.4 / -52.8 dB** against -4.4 dB for 3-5 kHz, a bandpass; those
   bands now read 2.4, 3.7 and 3.6 dB *above* 3-5 kHz. The same ceiling was sent to
   the song service as `lowpass` and ran `butter(2, lowpass)` over the sung vocal;
   the page now sends `lowpass=0`, and the ceiling stays only on the drive send.

   The drive is a parallel send now, not a split. Replacing everything above 400 Hz
   with a driven copy glued two signals at a crossover (3 dB bump at 400-600 Hz,
   3.5 dB dip at 600-800) and `tanh(amount)` normalisation added +6.6 dB to that
   half. The dry voice reaches the compressor whole and the shaper emits only what
   it adds (`tanh(a*x)/a - x`), mixed at 0.5-1.3 of its level. On the neutral
   profile the chain measures within +2.4/-2.0 dB from 300 Hz to 12 kHz against
   flat noise, and [the low-end check](#the-low-end-has-a-check) still holds the
   grind case at -74 dB.

   **The model's vocal is not transposed.** The service used to match the model's
   register to the recording's, which came out as **-5.5 semitones** on one render:
   on that job's own separated vocal it put **+7.4 dB** into 60-120 Hz and took
   **5-6 dB** out of 250 Hz-3 kHz, and the pitch it matched on was not real (pyin's
   median was 70.0 Hz, exactly its search floor, against an autocorrelation of
   46 Hz). The model sings its own melody over its own backing, so the mapping now
   carries the room only and the measurement is still reported: a fresh lo-fi render
   logs `no transpose (already in key)` and `+0.00 semitones`.
3. **Output.** Play it, or save 16-bit mono WAV. The result is trimmed under full
   scale because dry, room and echo add up on a loud phrase.

Measured in the browser: a 220 Hz tone moved to 174.0 Hz at −4 semitones and
261.0 Hz at +3 with the length unchanged; a two-second phrase renders in ~60 ms.

### The low end has a check

A rumble under the fitted phrase shipped for weeks because nothing measured it.
The measurement lives in the page now: `window.fitSelfTest()` renders a 220 Hz +
1 kHz tone and a 110 Hz voice through the real `fitRender` and reports six bands,
and opening the page as `?fittest=1` writes the answer into the title and a `<pre>`
so a headless browser can read it.

```
.venv\Scripts\python tools\check_fit_low_end.py
.venv\Scripts\python tools\check_fit_low_end.py --url "https://.../?fittest=1&split=0"
```

It runs that page in headless Edge (or Chrome) and fails if the 80-120 Hz band
passes **-52 dB**. It measures every genre profile at 0 and -5 semitones, a
"grind" case whose partials sit 80-120 Hz apart above the split, and a 110 Hz
voice — 46 measurements, three profiles per page load because the renders are real
time. The fixed chain's worst is -56.9 dB; the drive-everything version measures
-37/-34/-41, so the threshold sits in the gap between "fixed" and "the rumble is
back". A low voice losing its own band fails too (floor -6 dB).

`build/wr.ps1 deploy` runs it after wrangler reports success, so a deploy that
brings the rumble back exits non-zero and says the live page can rumble (the
deploy has already gone out; the exit code is the alarm, not a rollback).
`--no-check` skips it and `FIT_CHECK_URL` points it at a preview or a deliberately
broken page. It needs a local Edge or Chrome and network access; with no browser it
fails the deploy rather than skipping quietly. `?fittest=1&split=0` is the built-in
way to see it fail.

### The model sings; your voice is the measurement

**Sing the lyrics** in the Fit voice panel — and **Generate** or **Song designer**
once a phrase has been fitted — does not put your recording in the track and does
not clone it. The model sings the lyrics itself, so melody, phrasing and timing
belong to the song:

1. the lyrics come from the language model, or from the designer's panel;
2. ACE-Step renders the whole song — its own sung vocal and the backing together;
3. `demucs` splits that render into a vocal stem and a backing stem;
4. the vocal is measured against your recording (both pitches, and the tone half of
   the fit) but **not transposed**: it is already in the song's key;
5. the vocal is laid back **on the render itself**: the song is the render plus only
   the difference the mapping made, so there is no lead gain on this path (`LEAD_DB`
   and `MAX_LEAD_GAIN` only drive the route that lays a recorded phrase over an
   instrumental). `DUCK_UNDER_VOICE` (15%) lowers the whole mix by **1.41 dB** under
   a word, so it cannot change the ratio. Measured from the separated stems of the
   twelve renders in `build/stems/htdemucs`, the voice sits **-5.9 to +1.7 dB** over
   the music under a word (mean **-0.4 dB**).

`tools/check_mix_balance.py` prints every separated render's voice-over-music figure
and the duck; with `--render <song.wav>` it rebuilds the mix through the service's
own `_mix_separated` and compares it with `render * (1 - duck * gate)`, exiting
nonzero if a lead gain or a rebuild from the stems has come back. The stems stay out
of the repository (`build/*.wav` is ignored), so they are made first:

```
<service-venv-python> -m demucs --two-stems=vocals -n htdemucs -o build/stems <install>/song_service/logs/song_<id>.wav
<service-venv-python> tools/check_mix_balance.py            # defaults to build/stems/htdemucs
```

Your recording is only a measurement: its pitch sets the semitone shift, the tone
profile sets the ceiling and the room. Nothing you said is in the song. That is what
makes it sung rather than spoken — the melody comes from ACE-Step and is locked to
the backing by construction.

`demucs` is installed by the song service's own `install.py` (listed in
`song_service/models.json`), so there is no fourth environment. `voice_service` is
still installed and started but is **no longer the singer**: see
`voice_service/README.md`. Only **8090** is ever published.

## API

**Socket.IO events** (desktop app): `search_query` → `search_results`,
`search_completed`, `search_error`; `client_protocol` negotiates batching;
`discovery_results`; `record_click`; `get_stats`; `download_video` →
`download_progress`, `download_complete`, `download_error`.

**HTTP**: `POST /api/search`, `GET /api/stats`, `POST /api/download`,
`GET /download_file/<token>`, `POST /api/transcribe`.

**Song service**: `POST /api/song`, `POST /api/song/mix`, `POST /api/song/voice`
(recording in the body, lyrics written by the model), `GET /api/song/<token>`,
`GET /api/song/latest`, `GET /song_file/<token>`, `POST /api/lyrics` (with
`GET /api/lyrics/<token>`), `POST /api/align`.

**Voice service** (loopback only, reached by the song service):
`POST /api/voice/sing?text=…` with the reference WAV in the body,
`GET /voice_file/<token>`, `GET /health`.

## Configuration

| Variable | Where | Meaning |
| --- | --- | --- |
| `PORT` | app | Port for the desktop app (default 5001) |
| `WHISPER_MODEL` | app | Local speech model (default `base.en`) |
| `RENDER` | app | Set by Render; skips ML rebuilds and uses eventlet |
| `DOWNLOAD_API` | Worker | URL of the download service |
| `SONG_API` | Worker | Optional default origin of the song service; a pasted address in the page wins |
| `songApi` | browser (`localStorage`) | The model address pasted into "Connect my model" |
| `MAX_HEIGHT` | downloader | Download resolution cap (default 720) |
| `MAX_ACTIVE_JOBS` / `MAX_JOBS_PER_IP` | downloader | Concurrency limits |
| `YT_COOKIES_FILE` / `YT_COOKIES` | downloader | Optional yt-dlp cookie file, or the same jar as text |
| `SONG_WARM` | song service | `1` loads the song model at startup instead of on the first song |
| `LYRICS_DEVICE` | song service | `cuda` or `cpu` for the lyric model; unset means cuda when torch sees a card |
| `LYRICS_INLINE_SECONDS` | song service | How long `POST /api/lyrics` holds before `202 {token}` (default 40 s) |
| `ACE_CPU_OFFLOAD` / `ACE_DTYPE` | song service | Page weights in and out (default `1`); model dtype (default `bfloat16`) |
| `ACE_STEPS` / `ACE_MAX_SECONDS` | song service | Diffusion steps (30); ceiling on song length (120 s) |
| `SONG_ENDPOINT_KEY` | Worker secret | Proves a tunnel publish came from this PC; without it `POST /api/song/endpoint` is **403** |
| `x-song-key` | request header | The same key, sent by the launcher when it publishes |
| `/api/health` (Worker) | Worker | Whether the download service is configured, awake and holding cookies |
| `/health` (song service) | song service | `device`, `gpu`, `vram_gb`, `loaded`, `warming`, `warming_seconds`, `warm_failed`, `lyrics_loaded` |
| `/api/song/endpoint` | Worker | The tunnel address last published; `GET` is public, `POST` needs `x-song-key` |
| `/pc/<path>` | song service | Forwards to the downloader on 8080 |

## Building the environments

### One script, one environment (the whole application)

**Double-click `install.cmd`.** It installs, starts the services and registers the
logon entry, so nothing is typed and a reboot brings everything back. Nothing has to
be installed first and no administrator prompt appears: a PC that never ran Python
gets a portable `uv` (15 MB) and `git` (40 MB) in `tools\bin`, and `uv` fetches
Python 3.11. It needs ~20 GB free for torch, the packages and the ~10 GB of weights,
and an NVIDIA driver 560+. Without a card it still finishes and runs on the
processor, saying so: minutes to hours per song instead of seconds.

```
.\install_all.ps1 -Check              # what is installed, what is missing; changes nothing
.\install_all.ps1 -Start -Autostart   # install, start now, come back at logon
.\install_all.ps1 -NoWeights          # packages only (~10 GB of weights skipped)
```

`install_all.ps1` fills the single root `.venv` with everything the three running
services need, then downloads the ACE-Step weights; `tools\start_services.ps1` picks
that environment up once the script writes its marker. `voice_service` is not folded
in: chatterbox pins `numpy==1.26.4` and torch 2.6.0, which pip cannot hold beside
`numpy==2.4.6` and torch 2.14.1+cu126.

### The per-service environments (the fallback)

A clone contains **none** of them: `.venv`, `song_service\.venv` and
`downloader_service\.venv` are gitignored, as are the weights. All use **Python
3.11** and **`uv`**, built from the repository root.

### Why `uv` and not `python -m venv`

On this machine `python` is the Microsoft Store stub at `…\WindowsApps\python.exe`,
which only answers *"Python was not found"*; `py -3.11 -m venv` fails too, because
the interpreter `uv` installed is registered with `py` but cannot be started through
it. `uv` lives at `%USERPROFILE%\.local\bin\uv.exe`; if missing, install it from
<https://astral.sh/uv>. `--seed` is not optional: without it `uv venv` creates an
environment with **no pip** and the next line dies with *"No module named pip"*.

| Environment | Built by | Used for |
| --- | --- | --- |
| `.venv` | you | the site on 5001 |
| `downloader_service\.venv` | you, or the launcher on first run | yt-dlp on 8080 |
| `song_service\.venv` | you — the launcher refuses to invent it | the model on 8090 |

**1. The site**

```
uv venv .venv --python 3.11 --seed
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -c "from flask_socketio import emit, SocketIO; print('site env ok')"
```

**2. The downloader** (the launcher builds this itself when the folder is missing)

```
uv venv downloader_service\.venv --python 3.11 --seed
downloader_service\.venv\Scripts\python -m pip install -r downloader_service\requirements.txt
downloader_service\.venv\Scripts\python -c "import flask, yt_dlp, curl_cffi, imageio_ffmpeg; print('downloader env ok', yt_dlp.version.__version__)"
```

**3. The song service** — deliberately manual, since it installs CUDA torch and ~10 GB
of weights; the launcher prints *"run song_service\install.py first"* and starts
nothing. `install.py` installs into whatever interpreter runs it and does **not**
create the venv:

```
uv venv song_service\.venv --python 3.11 --seed
song_service\.venv\Scripts\python song_service\install.py --check
song_service\.venv\Scripts\python song_service\install.py
song_service\.venv\Scripts\python song_service\gpu_check.py
```

The `song_service\` prefix matters: written without it Python answers *"can't open
file '…\scrape\install.py'"*. `--packages` and `--weights` install one half.
`song_service\requirements.txt` is the same list in readable form; only `install.py`
downloads weights.

**4. The voice service** (optional, separate on purpose so the working music model is
not at risk)

```
uv venv voice_service\.venv --python 3.11 --seed
voice_service\.venv\Scripts\python.exe voice_service\install.py --check
voice_service\.venv\Scripts\python.exe voice_service\install.py
```

It is written not to leave the machine on CPU torch: `chatterbox-tts` pins
`torch==2.6.0`, whose PyPI wheel is the CPU build, so a plain pip install would
silently replace CUDA torch; the script restores the `+cu126` wheels afterwards. On
this machine `--check` prints `torch 2.6.0+cu126` and
`cuda True (NVIDIA GeForce RTX 4060 Ti)`. The launcher skips the service when the
venv is missing. Details and limits: `voice_service/README.md`.

**5. Everything at once**

```
powershell -NoProfile -ExecutionPolicy Bypass -File tools\start_services.ps1
```

| Check | Expected |
| --- | --- |
| <http://127.0.0.1:5001/> | the page |
| <http://127.0.0.1:8080/health> | `{"active_jobs":0,"ok":true,"max_height":720,…}` |
| <http://127.0.0.1:8090/health> | `"device":"cuda"`, `"loaded":true` after ~51 s |
| `curl https://<worker>/api/song/endpoint` | the address the launcher just published |
| `build\launcher_trace.txt` | a line with the time of this run |

The launcher fetches `cloudflared` if missing (pinned, signature checked). What it
cannot supply is `build\song_endpoint.key`: without it the tunnel runs but the site
is never told the address. Copy the file across, or set `SONG_ENDPOINT_KEY` on the
Worker and write the same value into the file. Each service also runs on its own:

```
.venv\Scripts\python app.py                    # the site, port 5001
downloader_service\.venv\Scripts\python app.py # downloader, port 8080
song_service\.venv\Scripts\python app.py       # the model, port 8090
```

The downloader needs no torch, which is why it starts in seconds where the song
service takes a minute and 10 GB.

### Do not copy a venv between machines

A venv is not portable: `pyvenv.cfg` holds an absolute `home` path, and every stub in
`Scripts\` ends in it. When this machine's profile changed from `bpier` to
`soluzka_3eenj0v`, all three venvs still pointed at a `C:\Users\bpier\…\Python311`
that no longer existed. Recreate the environments instead of copying the folder.

## Two Workers: the site, and the pool

| Base | File | What it is |
| --- | --- | --- |
| `browser.bpiersing.workers.dev` | `worker.js` | the site: page, search, downloads, a copy of the model proxy |
| `song-pool.bpiersing.workers.dev` | `pool-worker.js` | **Cloudflare 2**: the GPU pool, and a model base of its own |

The second holds only the list of machines — no assets, no page — so it survives a
redeploy of the site, can live on another account, and forwards `/api/song`,
`/api/song/<token>`, `/api/song/voice`, `/api/lyrics`, `/song_file/<token>` and
`/last_song` to a machine from the pool. The weights cannot be on Cloudflare:
ACE-Step is ~10 GB and needs CUDA.

```
npx wrangler kv namespace create song-pool
npx wrangler deploy --config wrangler.pool.jsonc
npx wrangler secret put JOIN_KEY --config wrangler.pool.jsonc
```

The site reads the pool from there (`POOL_API`) and forwards every machine's
announcement to it, falling back to its own KV when the pool base is unreachable.
**Measured on Cloudflare 2:** `GET /api/song/latest` → `200` with the job JSON, and
`HEAD /last_song` → `200 audio/wav, Content-Length: 34542420`; with a dead address in
the pool three of three `/last_song` requests still answered `200`.

A machine joins by running the agent once (Windows `install.cmd`, Linux `install.sh`
with the systemd unit). A browser cannot hand its GPU to a site and WebGPU runs
inside the visiting tab, which cannot hold this model. After that it rejoins on its
own: every launcher run posts its address to the site and to Cloudflare 2, so the
pool lists every awake agent.

### The badge in the header

`Pool · N GPUs` reads the pool from Cloudflare 2 every minute. Pressing it starts
this PC's agent through `songsvc://` (registered per user under
`HKCU\Software\Classes`, **no administrator prompt**), and with no GPU online its
tooltip says so.

### Adding a machine without much work

```
curl -fsSL https://browser.bpiersing.workers.dev/join.sh | bash -s -- --key <join key>
```

It installs the environment, CUDA wheels and models, the boot service, and starts the
stack; the machine then publishes itself to Cloudflare 2. On a machine with a
cloud-init field, paste `deploy/gpu/cloud-init.yaml` with three values filled in.

### Starting a GPU from the site (Kaggle)

When the pool is empty the header shows **Start a GPU**, which asks the site to boot a
free Kaggle GPU running this project's own boot; it opens a tunnel and joins the pool
by itself. Colab cannot be started this way — Google gives no API that starts a
runtime — while Kaggle's token can.

**Worker secrets** (never in the repository):

| Secret | Where it comes from |
| --- | --- |
| `KAGGLE_USERNAME` | the `username` field of your `kaggle.json` |
| `KAGGLE_KEY` | the `key` field of the same file |
| `SONG_JOIN_KEY` | `build\song_endpoint.key`, so the started card can join the pool |

**Requirements at Kaggle** — all hit for real before the button worked:

| Requirement | Why | What it looks like when missing |
| --- | --- | --- |
| A verified phone number | Kaggle gives no GPU *and* no internet to an unverified account | **Queued** forever, or CPU with `Could not resolve host: github.com` |
| A free GPU hour left this week | ~30 hours, shared across all kernels | refused or queued indefinitely |
| `enableInternet` granted | the kernel clones and installs ~10 GB | `NO INTERNET in this kernel` |
| A token that may **read** the kernel | `kaggle kernels output …` fetches the log | `Permission 'kernels.get' was denied` |
| A private kernel | the pushed code carries the join key | a public kernel would publish the key |

**Building `build\kaggle.json`, once per machine**

1. <https://www.kaggle.com/settings/api> → **Create New Token**; it downloads
   `{"username": "your-kaggle-name", "key": "a long hex string"}`.
2. Put it at `build\kaggle.json` (a placeholder is already there). It is gitignored
   and must stay at that path.
3. Store the secrets and deploy:

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File tools\set_kaggle.ps1
   powershell -NoProfile -ExecutionPolicy Bypass -File build\wr.ps1 deploy
   ```

   The script refuses the placeholder, feeds each value through a temporary file on
   stdin (never the command line or console) and deletes it.
4. Check without starting anything:

   ```powershell
   curl.exe -s https://browser.bpiersing.workers.dev/api/gpu/status
   # {"lastStart":0,"status":null,"configured":true}
   ```

`configured: false` means no Kaggle secret yet. `started` in `POST /api/gpu/start`
means Kaggle accepted the run, not that a GPU is on the pool: the card has to boot,
install ~10 GB of weights and publish.

**Pushing a kernel version by hand** makes the same call the button does:

```bash
curl -s -X POST https://browser.bpiersing.workers.dev/api/gpu/start
# {"configured":true,"started":true,"note":"…","said":"{\"versionNumberNullable\":15,…}"}
```

No body and no key: the token is a Worker secret. `said` is Kaggle's own answer,
where `error` shows a refusal and `invalidDatasetSources` names a dataset it could
not mount. A spent KV write allowance does not block the push (the cooldown
timestamps are best-effort).

This **replaces** the running session: Kaggle attaches the new version to the same
kernel, so pushing kills a live card — its tunnel dies, the pool empties, and the new
boot warms again. It also spends the week's GPU hours.

**When the card must run code you just committed**, a push alone is not enough: the
kernel never clones GitHub, it runs `repo/public/song_gpu_offline.py` out of the
`song-gpu-code` **dataset**, staged by `tools\kaggle_pack.ps1`. Repack, then push:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\kaggle_pack.ps1 -CodeOnly -Create
curl.exe -s -X POST https://browser.bpiersing.workers.dev/api/gpu/start
```

`-CodeOnly` uploads the repository copy and its unpacked Linux packages, not the
10.7 GB of weights. The exact tree Kaggle will mount is the staging folder, so it can
be checked first:

```powershell
Select-String -Path build\kaggle_pack\code\repo\public\song_gpu_offline.py -Pattern 'the video downloader'
```

**Limits:** one start per thirty minutes (the `gpu:started_at` timestamp lives in the
**pool** Worker's KV at `/state/gpu:started_at`, because the site's own KV runs out of
daily writes), the weekly allowance is Kaggle's, the kernel is private to your
account, and a started card ends when its session ends.

```bash
curl -s https://song-pool.bpiersing.workers.dev/state/gpu:started_at   # {"key":…,"value":"…"}
```

Reads are open; a write needs `x-join-key`, which only the site Worker sends. A token
starting with `KGAT` is a bearer token and is sent as `Authorization: Bearer` (basic
auth is answered `401`); the `username` field may hold a display name while the API
wants the lowercase handle, so the Worker reduces it to that form. No name is written
in this repository.

#### What is never pushed

| Path | Why it is local | Where it belongs instead |
| --- | --- | --- |
| `build\song_endpoint.key` | publishes a machine into the pool | nowhere: `tools\set_kaggle.ps1` writes `SONG_JOIN_KEY` as a Worker secret |
| `build\kaggle.json` | boots a GPU on your account | nowhere: your Kaggle account — **never share this file** |
| `build\tunnel_url.txt` | one session's address | nowhere |
| `song_service\models\` | ~10 GB of weights, plus the packer's metadata | Hugging Face, re-fetched by `install.py --weights` |
| `build\kaggle_pack\` | 11 GB of repo copy, Linux packages, ACE-Step source | your private Kaggle dataset, uploaded by `-Create` |
| `build\kaggle_pack\requirements-offline.txt` | the filtered list | regenerated on every `-Packages` run |
| `.venv\`, `song_service\.venv\` | the environments | rebuilt by `install.cmd` |
| `tools\bin\*.exe` | `cloudflared.exe`, 53 MB | fetched again on first run |

Name files when committing (`git add worker.js tools/set_kaggle.ps1 README.md`) rather
than `git add -A`; if a credential is ever committed, revoke it at the provider first.
The packer is tracked; only what it produces is ignored.

#### If someone else wants the same setup

Each person makes their own datasets under their own account with the same script;
nothing here names an account: verify the phone number, create a token, get the
weights with `install.py --weights`, then run the packer commands in
[The offline datasets](#the-offline-datasets-mounting-instead-of-downloading).

`-Create` makes `<handle>/song-gpu-code` and `<handle>/song-gpu-weights` **private**
(`isPrivate` in the metadata), so the account name is not published with the weights;
`worker.js` derives both slugs from the token's account and can be pointed elsewhere
with `KAGGLE_CODE_DATASET` and `KAGGLE_WEIGHTS_DATASET`. The packer runs on Windows
(robocopy, uv) and says so if either is missing.

### The offline datasets (mounting instead of downloading)

A pushed kernel does not always get the network, so the packer builds two private
datasets mounted at `/kaggle/input`:

| Dataset | Contents | Read at |
| --- | --- | --- |
| `<handle>/song-gpu-code` | the repo, `pylibs` (Linux packages), `ace-step` source, `cloudflared-linux-amd64` | `/kaggle/input/song-gpu-code/repo` |
| `<handle>/song-gpu-weights` | `song_service\models` uploaded as it is | `/kaggle/input/song-gpu-weights/ACE-Step-v1-3.5B` |

The weights dataset is `song_service\models` itself with a `dataset-metadata.json`
written beside the models; it is gitignored, so nothing reaches GitHub.

```powershell
powershell -ExecutionPolicy Bypass -File tools\kaggle_pack.ps1              # repo + ACE-Step + cloudflared
powershell -ExecutionPolicy Bypass -File tools\kaggle_pack.ps1 -Packages    # + the Linux packages
powershell -ExecutionPolicy Bypass -File tools\kaggle_pack.ps1 -Weights     # report the weight sizes
powershell -ExecutionPolicy Bypass -File tools\kaggle_pack.ps1 -Create      # create/update both datasets
```

`-Create` reads `build\kaggle.json` for the token, so the key is never typed into a
shell.

#### The package target, exactly

The kernel runs **Python 3.13.16** on a **Tesla T4 (15360 MiB)** with **torch
2.11.0+cu130** already installed, so the packed packages target that:

```
uv pip install --target build/kaggle_pack/code/pylibs ^
    --python-version 3.13 ^
    --python-platform x86_64-manylinux_2_28 ^
    --only-binary :all: --no-cache ^
    -r build/kaggle_pack/requirements-offline.txt
```

`--only-binary :all:` is what makes the result usable offline; when it cannot be
satisfied the fix is to pin a version with a `cp313` manylinux wheel, not to drop the
flag. Three things are left out:

| Left out | Why |
| --- | --- |
| `torch`, `torchaudio`, `torchvision` | the image already has torch 2.11.0+cu130; the repo pins a CUDA-index build that cannot be reproduced offline |
| `chatterbox-tts` | it pins `transformers==5.2.0`, ACE-Step needs `4.57.6` |
| `cutlet` | its dependency `mojimoji` is a source-only C extension with no Linux wheel; ACE-Step imports it lazily for Japanese romanisation |

```
grep -n -i "cutlet\|mojimoji\|py3langid" song_service/requirements.txt voice_service/requirements-shared.txt
grep -v -i '^cutlet$' build/kaggle_pack/requirements-offline.txt > /tmp/r2.txt
```

#### The pin that broke the first real run

The first run that reached a worker stopped at
`ImportError: tokenizers>=0.22.0,<=0.23.0 is required … but found tokenizers==0.23.3`.
Kaggle's image carries 0.23.3, transformers 4.57.6 refuses anything above 0.23.0, and
`pip install transformers` will not correct an already "satisfied" version, so
`tools\kaggle_pack.ps1` appends `tokenizers==0.22.1` to the offline list and the
offline boot prefers the mounted `pylibs` before anything the image ships. 0.22.1 and
not 0.23.0 because PyPI has no 0.23.0 release.

### One installer, and what it installs

`join.cmd` is the only install the site hands out, and it installs **everything** —
the site on 5001, the downloader on 8080, the song service on 8090, the CUDA wheels
and both checkpoints — from a machine that has cloned nothing.

| Step | What happens |
| --- | --- |
| double-click | `join.cmd` fetches `join.ps1` from the Worker |
| fetch | the repository zip (~40 MB) is unpacked into `%LOCALAPPDATA%\internet-gpu` |
| install | `install_all.ps1`: portable uv and git, Python 3.11, torch cu126, every package, ~10 GB of weights |
| update | with `.venv\all_services.json` **and** `song_service\models\ACE-Step-v1-3.5B` present, the refreshed files mean a restart instead of an install |
| start | the site, the downloader, the song service with `SONG_WARM=1`, and a Quick Tunnel |
| announce | the address goes to the site and to the pool with the join key |
| return | the logon entry brings it back after a reboot |

It takes 20–60 minutes and ~20 GB on a normal connection, printing each step so a slow
download looks slow rather than broken. Running it again is an update; a half-finished
install is repaired by installing. Without an NVIDIA card the same models run on the
processor — free and correct, just slow.

### Free: a Colab T4, one click

`deploy/colab/song_gpu.ipynb` (also served at
`https://browser.bpiersing.workers.dev/song_gpu.ipynb`) is the no-cost card: open it in
Colab, choose a free T4, press **Run all**; it installs the song service on the
notebook's machine, opens a tunnel and prints an address. Nothing is installed on your
computer and nothing is rented.

That address is usable two ways: press **Connect my model** in the header and paste it
(no key needed), or paste the join key when the notebook asks and the card joins the
pool so every visitor's click can use it. A T4 has 16 GB — twice this card — so renders
are faster while the session lasts; when Colab stops it, the card leaves the pool,
which is what "free" costs. It is also the only free card that can run this model:
Workers AI has no music model, Containers have no GPU, a browser tab cannot lend its
card to a page.

### Timing the words against the vocal

`song_service/align.py` measures when each line is sung instead of slicing the clock
evenly (both an even slice and a bar-weighted one drift). It is not a new model:

1. **Separate the voice** — demucs `htdemucs`, the same separator the voice map uses.
2. **Find the phrases** — a 20 ms RMS envelope of that stem, gated against its own
   peak; runs under 150 ms are dropped and gaps under 120 ms closed.
3. **Fit the lines** — each line gets a share of the *sung* time by syllable count, and
   each boundary moves onto a phrase edge within 0.4 s.

On the full mix a drone keeps the meter above the threshold for the whole song, and the
result is the flat split again. Measured on a test song whose four phrases begin at
1.2, 3.8, 7.0 and 10.8 s, the separated run returned 1.20, 3.82, 7.02 and 10.82 s, and
the first line started at 1.20 s rather than 0.

```
POST /api/align        multipart: audio=<file>, lines=<json array of strings>
  -> { lines: [{start, end}, ...], seconds, phrases: [[start, end], ...],
       source: "the separated vocal" | "the song itself", seconds_taken }
```

```
song_service\.venv\Scripts\python.exe song_service\align.py song.wav --lines lines.json
song_service\.venv\Scripts\python.exe song_service\align.py song.wav --lines lines.json --no-separate
```

The page keeps the schedule with the saved song, so the card, the player and the
recorded video use one timing. When the service is not running the in-page estimate
stays in charge; the song is never blocked on alignment. `Re-time the words` and
`Record the video` measure again, which is how an older song gets its words right.
Limits: line-level, not word-level — word timing needs a recogniser with timestamps, a
different download and a different set of mistakes; and a video already recorded has
the old words burned in, so it must be recorded again.

### The 10 GB on Cloudflare

Storage is not the obstacle — execution is. The checkpoint fits in R2 (free storage, no
egress) and `pool-worker.js` serves it streamed at `/weights/<key>`, so a machine can
fetch the model from Cloudflare instead of Hugging Face. Live today the route answers
`501 No R2 bucket is bound…` until a bucket exists; the API currently answers
`code 10042 Please enable R2 through the Cloudflare Dashboard`. After that:

```
npx wrangler r2 bucket create song-weights
npx wrangler r2 object put song-weights/ace-step/model.safetensors --file=<path>
```

then uncomment the `r2_buckets` block in `wrangler.pool.jsonc`. R2 cannot run
inference: a Worker is a 128 MB isolate with no GPU.

### Start at boot, before anyone logs in (optional, needs admin once)

```
powershell -NoProfile -ExecutionPolicy Bypass -File tools\install_startup_task.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File tools\install_startup_task.ps1 -Remove
```

That needs administrator rights **once**. It registers `AI song services (boot)` with
trigger AtStartup, principal S4U (the user's profile, no stored password), no window
and unlimited runtime, so the stack is serving before the lock screen is answered.

## Running the whole stack on a GPU host

Cloudflare cannot run these models: a Worker is a 128 MB isolate with no GPU,
Containers stop at 4 vCPU / 12 GiB with no GPU, and Workers AI's catalog of 73 models
has no music model. `deploy\gpu\` runs everything in one container from one
environment — site (5001), downloader (8080), song service (8090) and `cloudflared` —
so no inbound port has to be opened:

```
docker compose -f deploy\gpu\compose.yaml up -d --build
docker compose -f deploy\gpu\compose.yaml logs -f
```

A named tunnel gives it a permanent hostname; `cloudflared tunnel token song-ai` goes
into `deploy\gpu\.env` as `TUNNEL_TOKEN`. Then point the Worker at it once:

```
npx wrangler deploy --var SONG_API:https://ai.example.com
```

On a Linux GPU host it starts at boot with nobody logged in:

```
./deploy/gpu/install_service.sh          # needs root once, for the unit
systemctl status song-stack
```

The full walkthrough — tunnel commands, GPU reservation, systemd unit, image contents
— is in `deploy\gpu\README.md`.

## Troubleshooting

- **"database is locked"** — connections use WAL and a 30 s busy timeout; the app and
  the exporter can both touch the database safely.
- **Genres missing on the live site** — run `tools/export_index.py`, confirm it prints
  `genres N/M`, then redeploy.
- **Download answers 405 or opens the source page** — `DOWNLOAD_API` is unset or
  unreachable; the page falls back to the original link.
- **Voice search does nothing** — the microphone needs a secure origin.
- **The badge says "AI off"** — the song service is not running. Press **Start the AI**,
  or run `tools\start_services.ps1`.
- **The badge says `CUDA · warming 2m 30s` and keeps counting** — fifty-one seconds is
  normal here, two minutes means something else is using the machine, four means look
  at `warm_failed` in `/health` and `song_service\logs\service_err.log`.
- **The badge flips to "AI off" on reload, then comes back** — the probe is measured at
  twenty seconds in the launcher and twelve in the page; a host that misses both while
  loading reads `CUDA · busy loading` rather than lost.
- **A phone says "AI off" while the PC is fine** — stale or dead tunnel:
  `curl https://<worker>/api/song/endpoint` shows what the site holds; re-run the
  launcher, then reload the page.
- **TikTok or Dailymotion will not play on a phone, but YouTube does** — the player
  needs `/pc/api/play`; check `https://<tunnel>/pc/health` answers 200, else the tunnel
  points elsewhere.
- **Nothing started after a boot** — compare `LastBootUpTime` with
  `build\launcher_trace.txt`; no line within a minute means the entry did not run
  (re-run `tools\install_autostart.ps1`), a line but no services means the head of
  `build\launcher_run.log` says why.
- **Start the AI does nothing** — `songsvc://` is not registered; any run of
  `tools/start_services.ps1` registers it.
- **Lyrics take minutes instead of seconds** — the lyric model is on the CPU; `/health`
  shows the device, unset `LYRICS_DEVICE` to let it pick CUDA.
- **"No lyric was written"** — the toast carries the model's reason: "returned too
  little text" means three answers failed the checks; "still writing when the wait ran
  out" means the card was busy for five minutes. A write that outlasts the tunnel is
  normal and is collected by polling.
- **A song takes about ten minutes** — 90 seconds of audio at 16 steps on one card;
  `ACE_STEPS` trades quality for time, `ACE_MAX_SECONDS` for length.
- **The tunnel answers 404 with no body** — cloudflared picked up
  `~/.cloudflared/config.yml`; run it with `--config tools\bin\quick-tunnel.yml`.
- **"tunnel: could not fetch cloudflared"** — the first run needs GitHub reachable for
  the 53 MB binary; it is retried every launcher run, or put `cloudflared.exe` in
  `tools\bin\` by hand.
- **"tunnel: the download failed its signature check"** — something other than the
  signed release answered for GitHub; the partial file is deleted, so check the network
  or proxy rather than editing the check away.

`vercel.json` describes the same stack as four Vercel **services**: `site` on
`/(.*)`, and `song`, `voice` and `downloader` published under `/song/`, `/voice/`
and `/downloader/` — the catch-all comes last so the specific prefixes win. The
calling services get their targets through bindings, which fill
`SONG_SERVICE_URL`, `VOICE_SERVICE` and `DOWNLOADER_URL` with each target's
internal URL; nothing is set by hand and nothing is hardcoded, because every one
of those variables also accepts the tunnel address when the stack runs here. Each
service strips its public prefix in a small WSGI wrapper, so the same app answers
at `/` locally and at `/song/api/song` (for example) behind the internet.

`song` and `voice` build there from `requirements-vercel.txt`, which is Flask and
nothing else: their model imports are lazy, `/health` reports
`model_installed: false`, and the render routes answer **503** with that reason
instead of the import taking the service down. A published API with no model is
honest about it; the GPU install is unchanged and still uses `install.py`,
`models.json` and `requirements.txt`. The Cloudflare Worker does not read
`vercel.json` at all — it keeps serving `worker.js` and `public/`, and the two
deployments are independent.

## License

MIT — see [`LICENSE`](LICENSE).
