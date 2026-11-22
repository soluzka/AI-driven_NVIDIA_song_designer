# internet

A video search engine that works over its own library. It searches a local
SQLite index of scraped videos, learns from the links you click, asks each
listing which genre it belongs to, and downloads the videos you want.

It runs two ways from one codebase:

- **desktop app** — Flask + Socket.IO + SQLite on your machine, with live
  search, progress bars and yt-dlp downloads;
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
| `tools/install_autostart.ps1` | Adds the logon entry and starts the watchdog, so both return and stay up |
| `tools/register_protocol.ps1` | Registers `songsvc://` so the site's Start the AI button can launch it |
| `start.cmd` / `start.ps1` | One click for the same launcher, then opens the site |
| `data/video_index.db` | Committed snapshot of the index |
| `build/` | The check scripts: what a build generates is ignored, the scripts are not — see below |

### The `build/` folder

The scripts in here are tracked, so the checks move with the repository. What a
build *generates* stays local, because no commit can hold it: `ai_engine/` is
PyInstaller's work folder with a 314 MB package in it, and `wrangler-tool/` is
the portable Node install with a 103 MB `workerd.exe`, and GitHub refuses any
file over 100 MB. Captured logs, test audio, JSON payloads and the
`commit_msg*.txt` notes are left out for the same reason a receipt is not kept
with the product.

The ones worth knowing about:

| Script | What it checks |
| --- | --- |
| `deploy_and_check.ps1` | Deploys, then confirms the new version serves the page |
| `verify_live.ps1` | The live page holds this build, and the repo is clean |
| `asset_check.ps1` | Each `public/` file is served with the right content type |
| `service_state.ps1` | Both local services, with the GPU answer from `/health` |
| `autostart_status.ps1` | Startup entry, `songsvc://` target, services now |
| `race_test.ps1` | Two launcher runs leave exactly one song service |

They resolve the repo root from their own location, so any of them runs from
wherever you are. `race_test.ps1` restarts the song service; run it when nothing
is rendering.

### The index on the site

The 8,724 videos live in a Cloudflare D1 database (`video-index`, bound as `DB`
in `wrangler.jsonc`), and `/api/search` answers from it. The page asks for sixty
rows per search instead of downloading `public/data/videos.json` — three
megabytes the browser used to fetch in full before it could show one card.

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
database behind it. To push the SQLite file's contents up after a change:

```
python build/export_d1.py
build/wr.ps1 d1 execute video-index --remote --file build/d1_import.sql
```

`build/wr.ps1` is wrangler via the portable Node in `build/wrangler-tool`, which
is how this project deploys without a global install.

## Run it locally

```
uv venv .venv --python 3.11 --seed
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python app.py
```

Then open <http://127.0.0.1:5001>. `PORT` overrides the port, except `PORT=0`,
which falls back to 5001 so the server cannot end up on a random port.

### What `requirements.txt` installs

One file, and none of it is the music model — that lives in `song_service/`
with its own manifest. This is the web app:

| Group | Packages | Why |
| --- | --- | --- |
| Web framework | `Flask`, `Flask-Cors`, `Flask-SocketIO`, `python-socketio`, `python-engineio` | The site, its socket stack and the live search stream |
| HTTP clients | `aiohttp`, `requests` | Scraping the source sites |
| Video downloads | `yt-dlp`, `yt-dlp-ejs`, `curl-cffi`, `imageio-ffmpeg` | yt-dlp plus the impersonation and ffmpeg it needs |
| HTML parsing | `lxml` | Listings are parsed with lxml; there is no BeautifulSoup |
| Ranking | `numpy`, `scipy`, `scikit-learn` | The ML index behind search order and genre inference |
| Voice search | `faster-whisper` | Local transcription; the app falls back to the Windows recogniser without it |
| Build | `wheel`, `setuptools`, `pyinstaller`, `pyinstaller-hooks-contrib` | Only needed to freeze the desktop executable |

**Install into a virtual environment, always.** A partially installed or broken
global interpreter makes PyInstaller freeze an empty package, which is what
produces `cannot import name 'emit' from 'flask_socketio'` in the frozen app.
Check the socket stack before building:

```
.venv\Scripts\python -c "from flask_socketio import emit, SocketIO"
```

Optional desktop build:

```
.venv\Scripts\python -m PyInstaller --clean --noconfirm ai_engine.spec
```

That freezes `app.py` into `dist/ai_engine.exe`, bundling `public/` and
`cinema.ico` per `ai_engine.spec`. The executable runs the same Flask app and
opens on the same port; it does **not** bundle the song models, which stay in
`song_service/` and are reached over HTTP exactly as the browser does.

## How searching works

- **Matching has no cap.** A query matches videos whose title, description, tags
  or source contain its terms; results stream in batches of 200 with
  `search_completed` marking the end. There is no fixed result limit.
- **Ranking** combines term scores with click history, so links you use rise.
- **Discovery** runs in one background worker after a search: it scrapes the
  source sites for that query, stores new videos and reports how many were
  added. One worker keeps a burst of searches from saturating the machine.
- **The index grows by itself.** The live database is `../video_index.db`, next
  to the project folder; `data/video_index.db` is the committed snapshot.

### Genres

`genre_model.py` does not keyword-match. It asks each listing a set of questions
— does it name a scene or subgenre, which artists, which labels, bass, drums,
vocals, instruments, place, mood, tempo — and weights the answers by how rare
those phrases are across the whole library (IDF), so a distinctive word counts
more than a common one. Word-boundary matching keeps `dub` out of `dubstep`, and
a veto list of workshop/catalogue words stops non-music pages from being labelled
unless a strong music cue is present.

The percentage on a card is **decisiveness, not a probability**: it says how
clearly the questions were answered. `why?` on a chip shows the evidence.

On the desktop app the model fits in a background thread at startup (~8 s on a
large index) so startup is not delayed; inference is ~1.5 ms per video.

## The static site (Cloudflare)

`public/` is plain HTML/CSS/JS with no build step. The Worker serves it and
answers three things itself:

- `/api/transcribe` — voice search via Workers AI (`@cf/openai/whisper`);
- `/api/download`, `/download_file/...` — proxied to the download service
  (see below);
- everything else — the static files, with SPA fallback to `index.html`.

The page first tries Socket.IO (the desktop app). If it cannot connect it falls
back to the bundled index in `public/data/videos.json` and searches in the
browser, so the site works with no backend at all.

### When the internet is off

The page says so itself: a strip appears above the header the moment the browser
loses its connection, and disappears when it comes back.

```
●  You are offline. Search, downloads and the pool need a connection;
   a song can still render while this PC's model is running.
```

It is driven by `navigator.onLine`, re-read on the `online`/`offline` events and
every twenty seconds, because a link that has lost its route can keep reporting
`true` for a while. Nothing is hidden and nothing is faked: the model chips go to
their own states (`Nothing is running`, `AI not connected yet · warming`) and
`Start a GPU` appears only when the pool is empty.

What still works with the cable out:

| Thing | Offline |
| --- | --- |
| Loading the deployed page | no - it is served from Cloudflare |
| The same page from this PC | yes - the desktop app serves it on `127.0.0.1:5001` |
| Rendering a song on this PC's model | yes - the song service is local on 8090 |
| Searching the library | yes from the local app (bundled index), no from the Worker (D1) |
| Downloads, the pool, `Start a GPU`, voice search | no - all of them are remote |

To keep working with no internet, use the local copy:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\start_services.ps1
# then open http://127.0.0.1:5001  (the site, from public/)
```

The song service on 8090 keeps its own `/health` answering, so the header still
shows what the card is doing even though the site itself was loaded from disk.

The same rule applies to a Kaggle kernel started from this site: Kaggle turns the
network off on API-pushed runs, so the offline boot (`public/song_gpu_offline.py`)
reads everything from mounted datasets and says `no internet in this kernel, which
is expected here` instead of failing at the first download.

Deploy after refreshing the index:

```
.venv\Scripts\python tools\export_index.py   # DB snapshot + videos.json (+ genres)
npx wrangler deploy                          # or let Workers Builds deploy on push
```

This repo is connected to Cloudflare Workers Builds, which runs `bun install`
and deploys on push to `main`. `package.json` declares `socket.io-client`; the
browser bundle is committed at `public/vendor/socket.io.min.js` so the page
needs no CDN and works even if a build installs nothing.

**The static index is a snapshot.** Search results and genres on the deployed
site are frozen at export time; scraped videos appear there only after running
the exporter again and redeploying. Genres must be precomputed because the
browser has no Python to ask.

## Songs and lyrics

The song AI is **local only**: ACE-Step is a 3.5B checkpoint that needs CUDA, and
Workers AI has no music model in its catalog, so nothing on Cloudflare can render
a track. Workers AI does handle **voice search** (`@cf/openai/whisper`), which is
the one model that genuinely runs in the site.

The page asks two things of `song_service/`, which runs on your own machine:

| Route (on 127.0.0.1:8090) | What it does | Model |
| --- | --- | --- |
| `POST /api/lyrics` | writes the words: the answer, or `202 {token}` while it writes | Qwen 2.5 1.5B Instruct |
| `GET /api/lyrics/<token>` | the words, once that write has finished | — |
| `POST /api/song` | sings those words | ACE-Step v1 3.5B |

Both models pick their device themselves: the lyric model runs on CUDA when
torch can see a card and in system RAM when it cannot. On the GPU the lyric model
is loaded only after the song model has been released, because 7.9 GB of ACE-Step
and 3 GB of Qwen do not fit in 8 GB together and paging between them cost more
than reloading — measured here, a lyric request went from **3m42s to 10.5s**.

A write is a job, not one long request. The card is reached through a Cloudflare
tunnel, and Cloudflare answers **524** to any request that has sent nothing for
100 seconds — which is what a cold lyric model looks like from the page. So
`POST /api/lyrics` waits `LYRICS_INLINE_SECONDS` (40) for the words and, if the
model is still writing, returns `202 {"token": …}`; the page then polls
`GET /api/lyrics/<token>` every two seconds until the sections arrive. The words
are the model's own either way: a slow write is never replaced with stand-in
lines.

`GET /health` reports what is running where:

```json
{"ok": true, "device": "cuda", "gpu": "NVIDIA GeForce RTX 4060 Ti",
 "vram_gb": 8.0, "loaded": true, "lyrics_loaded": true, "cpu_offload": true}
```

The page calls the service on `127.0.0.1:8090` and falls back to the sampled band
in the browser when the service is not running. It also stops asking the Worker
for the song model after the first 501, so a compose goes straight to the machine
that can actually render.

That band's guitars are the one part that was tried with a bit-crush and taken back
out. A quantising shaper on the electric parts raised the share of the bounce above
6 kHz from 0.041 to 0.070 and above 12 kHz from 0.0047 to 0.011, heard as an
interference rattle under the whole song rather than as grit; oversampling at 2x
and a low-pass after the shaper both left that alias band in place, so the crush is
gone rather than turned down.

### Installing the AI packages

The song service keeps its own environment, separate from the web app's venv,
because its dependencies are heavy and its CUDA wheels must not leak into the
Flask app. From the repo root:

```
uv venv song_service\.venv --python 3.11 --seed              # once; see "Building the environments"
song_service\.venv\Scripts\python song_service\install.py --check   # report, change nothing
song_service\.venv\Scripts\python song_service\install.py          # packages, then weights
```

`install.py` reads `song_service/models.json`, which is the one place versions
are declared — a bump is a manifest edit, not a hand-run pip command.

| Invocation | What it does |
| --- | --- |
| `--check` | Prints which packages import and which weights are on disk |
| `--packages` | pip-installs from the manifest: `torch`/`torchaudio` from the **cu126** index, `transformers==4.57.6`, `huggingface-hub==0.36.2`, ACE-Step from git, flask, flask-cors, soundfile, librosa, hf_transfer |
| `--weights` | Downloads ACE-Step v1 3.5B (~7 GB) and Qwen2.5-1.5B-Instruct (~3.1 GB) into `song_service/models/` |
| *(no flag)* | Both, packages first |

Things worth knowing before you start it:

- **No CUDA toolkit install.** On Windows the NVIDIA driver (560 or newer)
  provides the runtime and the cu126 torch wheel carries its own CUDA libraries.
  PyPI's default `torch` on Windows is CPU-only, which is why the index is
  declared per package in the manifest rather than left to pip's defaults.
- **Weights are resumable.** About 10 GB in total, gitignored under
  `song_service/models/`. Rerun `--weights` after an interruption: a partial
  folder is measured and continued rather than started over.
- **Verify the GPU** before blaming the app:
  `.venv\Scripts\python -c "import torch; print(torch.__version__, torch.cuda.is_available())"`
  should print `2.14.1+cu126 True`.
- **The web app's dependencies are separate.** Flask, the socket stack,
  faster-whisper and yt-dlp come from the root `requirements.txt` in the root
  venv; `song_service/` needs none of them, and the root app needs no torch.

### Start everything with one click

`start.cmd` (which runs `start.ps1`, a thin wrapper around
`tools/start_services.ps1`) brings up the whole stack in order and leaves
anything already running alone, so double-clicking it twice is safe:

1. `downloader_service/app.py` on **8080** — yt-dlp, Top 100 and the player;
2. `song_service/app.py` on **8090** — the lyric model and ACE-Step on the GPU;
3. `app.py` on **5001** — the site on this machine;
4. `cloudflared` — a Quick Tunnel giving the machine a public `https` address;
5. the browser, at <http://127.0.0.1:5001>.

The tunnel's address is printed **and published to the site**, so nothing has to
be pasted into a phone or a second computer. It changes every time the tunnel
restarts, and each run re-publishes the new one:

- `POST /api/song/endpoint` with `{"url": "https://….trycloudflare.com"}` and the
  header `x-song-key: <value of SONG_ENDPOINT_KEY>` stores it in the `STATS` KV
  namespace under `song_api`. A write without that header answers **403**.
- `GET /api/song/endpoint` is public and returns the stored address, which is what
  the page reads on load before probing anything.

So the pairing is: the PC owns the tunnel and the key, the site only remembers
the last address it was told. If the address looks stale, run
`tools\start_services.ps1` again — it republishes — and reload the page, which is
the only step a phone needs.

`tools\start_services.ps1 -NoTunnel` skips the tunnel entirely when you only want
the local services.

### Reaching the voice through it

There is **one** public address, and it points at **8090**. The singing and
voice-mapping work lives on that same service (`POST /api/song/voice`), and the
voice service on **8095** is called by `song_service` over loopback - see
`VOICE_SERVICE` in `song_service/app.py`. So nothing has to be tunnelled to
8095, and nothing should be: a second tunnel would be a second public door to a
model that only ever needs to hear from this machine.

Starting the tunnel by hand, and checking the route end to end, in PowerShell:

```powershell
Start-Process -FilePath tools\bin\cloudflared.exe -WindowStyle Hidden -ArgumentList @(
    'tunnel', '--config', 'tools\bin\quick-tunnel.yml',
    '--url', 'http://127.0.0.1:8090', '--no-autoupdate',
    '--logfile', 'tunnel.log', '--loglevel', 'info'
)

# The address the site is telling the page to use
$tunnel = (Invoke-RestMethod 'https://browser.bpiersing.workers.dev/api/song/endpoint').url
$tunnel

# The song service, through the tunnel
Invoke-RestMethod "$tunnel/health" | Select-Object ok, loaded, device

# The singing route, through the tunnel. A body too small to be a recording is
# refused before any job is queued, which is what makes this safe to run: the
# answer proves the route is reachable without spending ten minutes on the GPU.
try {
    Invoke-RestMethod -Method Post -Uri "$tunnel/api/song/voice?prompt=test&seconds=20" -Body ([byte[]](1..8))
} catch {
    $_.ErrorDetails.Message   # {"error":"Send the voice recording as the request body."}
}
```

If the first command answers but the last one 404s, an old `cloudflared` is still
holding a tunnel pointed at the wrong port: stop it and rerun
`tools\start_services.ps1`, which republishes the address as well.

> A config.yml in `~/.cloudflared` disables Quick Tunnels: its ingress rules win
> over `--url` and the catch-all answers **404**, so nothing reaches the service.
> The launcher therefore runs cloudflared with `--config tools/bin/quick-tunnel.yml`,
> an empty config that keeps the tunnel bound to port 8090.

`cloudflared.exe` is gitignored (53 MB), so a fresh clone has none. The launcher
fetches it once on first run, at a pinned version
(`$cloudflaredVersion = '2026.10.7'`), into a `.download` file first: Windows'
signature check must report **Valid** *and* name Cloudflare, or the file is
discarded and the tunnel is skipped with a line saying so. Nothing unpinned is
ever executed, and `latest` is deliberately not used. On 14 Nov 2026 the fetch
took 28 seconds from a clone with no binary at all, then the tunnel started and
published its address.

#### The endpoint key never goes to GitHub

`build\song_endpoint.key` is the only thing that can tell the site which tunnel
address is a real machine's, and it is **never committed**: it is listed in
`.gitignore`, `git ls-files` does not contain it, and `git log --all --
build/song_endpoint.key` is empty, so no clone, fork or pull request has ever
carried a copy. GitHub has no version of it.

The key is generated on the machine that owns it, read from disk by
`tools\start_services.ps1` and by the song service, and written into the
Worker's secret store (`SONG_JOIN_KEY`) by `tools\set_kaggle.ps1` — never into a
source file. Anything that ships publicly starts with it blank and has it filled
in at run time:

| Shipped file | What it carries | Where the key comes from |
| --- | --- | --- |
| `public/song_gpu.ipynb`, `deploy/colab/song_gpu.ipynb`, `public/song_gpu_cell.py` | `JOIN_KEY = ''` | pasted by whoever runs the cell, and only then |
| The Kaggle kernel `/api/gpu/start` pushes | the key, written in | the Worker secret `SONG_JOIN_KEY` at push time |
| The page | nothing | it asks the song service beside it (`/api/pool/join`), which reads the file |

So a public notebook that somebody runs does not put their card in this site's
pool, and nobody reading the repository can publish a machine of their own into
it.

### Starting the AI from the site

The deployed page is on Cloudflare, the song AI is on your PC, and a browser tab
cannot start a program. Windows can, so the header carries a **Start the AI**
button that navigates to `songsvc://start`; that scheme is registered for the
current user (HKCU, no administrator) and points at `tools/start_services.ps1`.

What the click does, in order: Windows runs the launcher, the launcher starts the
downloader, the song service with `SONG_WARM=1` and the tunnel, and the page polls
`/health` — every five seconds while it is off or warming, every thirty once it is
up — until the button reads **AI ready**.

Next to it a badge reports what the render will use, read from `/health` rather
than guessed, and it separates *answering* from *ready* because the two are about
fifty seconds apart on this machine:

| `/health` says | Badge | Button |
| --- | --- | --- |
| `loaded: true`, `device: "cuda"` | `CUDA · GeForce RTX 4060 Ti`, green | `AI ready` |
| `loaded: false`, `warming_seconds: 95` | `CUDA · warming 1m 35s`, amber | `AI warming…` |
| nothing answers, but it did in the last four minutes | `CUDA · busy loading` | `AI warming…` |
| `device: "cpu"` | `CPU only`, amber | `AI ready` |
| nothing answers and nothing recent | `AI off` | `Start the AI` |

Measured on 13 Nov 2026: **51 seconds** from process start to `loaded: true` with
nothing competing. Any five-to-ten-minute wait is a restart, not the model — the
launcher used to probe `/health` with a three-second limit, call a busy service
dead, and start a rival copy that could not bind 8090, which restarted the warm.
The probe now allows twenty seconds and the song service runs `AboveNormal` so the
site fitting its own models does not starve it.

A compose pressed during the warm is not dropped: the page waits up to three
minutes for the service to answer, counting in the status line, instead of falling
back to a song with no AI in it.

![The header: Start the AI beside Song designer, and the CUDA badge reading the card from /health](public/gpu-chip.png)

Four things start it, and none of them need you to run a script:

| Trigger | How |
| --- | --- |
| Clicking **Start the AI** | `songsvc://` → `tools/start_services.ps1` |
| Logging in to Windows | `HKCU\...\Run` key and Startup entry, from `tools/install_autostart.ps1` |
| A service dying while the PC stays on | The watchdog re-runs the launcher within two minutes |
| Running it by hand | `tools/start_services.ps1` (safe to repeat) |

The launcher repairs the `songsvc://` registration on every run — missing, or
pointing at a folder that has moved — so the button keeps working without
re-registering. It writes one line per run to `build\launcher_trace.txt`, which
is how "the site started it" can be checked rather than assumed.

#### Is the autostart installed? Is it right?

These are the checks to run when a boot looks like nothing happened. The full
transcript of the last run is `build\launcher_run.log`; the one-line-per-run
history is `build\launcher_trace.txt`.

```powershell
# every registered trigger, the watchdog, and whether the services answer now
powershell -NoProfile -ExecutionPolicy Bypass -File build\autostart_status.ps1

# the two entries on their own
(Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'AI song services').'AI song services'
Test-Path (Join-Path ([Environment]::GetFolderPath('Startup')) 'AI song services.cmd')

# was there a run at logon? compare a boot with the trace
(Get-CimInstance Win32_OperatingSystem).LastBootUpTime
Get-Content build\launcher_trace.txt -Tail 5
```

A boot on 13 Nov 2026 read: boot `21:25:34` → `launcher ran 21:26:10` →
`watchdog started 21:26:25` → tunnel published → `/health` `loaded: true` at
`21:52`-ish. If the trace has no line within a minute of the boot time, the entry
did not run; re-install it:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\install_autostart.ps1
```

That is safe to repeat: it writes the same two entries and reuses a running
watchdog instead of adding a second one.

None of these is Task Scheduler, because registering a task returns *Access
denied* without administrator rights. The logon entry is a Run key plus the
Startup folder entry, and the watchdog is the launcher started with `-Watch`: it
sleeps two minutes at a time and restarts a service whose port has nothing behind
it. A port with a process behind it is left alone, so a service that is still
importing torch is never started twice. Verified on 8 Nov 2026: the song service
was killed outright and `/health` answered again 18 seconds after the watchdog
noticed.

That logon entry matters because the Startup folder alone has already failed
here: on 8 Nov 2026 the PC booted at 18:41:26, Explorer logged in at 18:41:38,
and the entry produced no run at all until the launcher was called by hand.

Three things it cannot do: start anything on a machine where the scheme is not
registered (your phone, another computer), start a local program without a click
— browsers only allow `songsvc://` from a user gesture, so opening the page can
detect the AI but not boot it — and render a song without the PC, since no
Cloudflare model composes music, as the section further down explains.

The badge is the quickest way to answer "is it actually on the GPU?" — it says
so in the header, from the service's own answer, without opening a console.

### If the site must reach this machine over the internet

Everything above works because the page runs on the same computer as the model.
A phone or another laptop needs a public address to reach instead, which the
tunnel started by `tools/start_services.ps1` provides.

**Nothing has to be pasted.** The launcher publishes the address it was given to
the Worker (`POST /api/song/endpoint`, key `SONG_ENDPOINT_KEY`) after every start,
and the page asks for it on load. That is why a new tunnel name still works on a
phone after a reload and not after anything else.

### What the tunnel carries

The tunnel points at **8090**, the song service. That service also forwards to the
downloader on loopback, so one address covers everything a phone cannot reach:

| Path | Reaches | Used for |
| --- | --- | --- |
| `/api/song`, `/api/song/<token>`, `/song_file/<token>` | the song service itself | composing, progress, saving |
| `/api/lyrics`, `/health` | the song service itself | lyrics, the badge |
| `/api/top` | downloader **8080** | the Top 100 button |
| `/pc/<anything>` | downloader **8080** | `api/play`, `download_file`, progress — TikTok and Dailymotion playback |

yt-dlp has to run on this machine (YouTube refuses a datacentre address), so the
file is fetched here and streamed back through that path — `Range` is forwarded,
which is what lets the player seek. Without the `/pc` route a phone can play
YouTube, whose embed is remote, and nothing else.

### If the address goes stale

A Quick Tunnel gets a new name whenever `cloudflared` restarts, so the stored
address can point at nothing. The symptoms are a phone that says **AI off**, a Top
100 that returns "the downloader on this PC is not answering", or a video that
will not start, while the PC itself is fine.

1. Run `tools\start_services.ps1`. It starts or reuses the tunnel and republishes
   the current address by itself.
2. Check what the site now holds: `curl https://<your worker>/api/song/endpoint`.
3. Reload the page on the phone. The new address is only read on load.

To publish by hand, with the key from the Worker's secrets:

```
Set-Content -Path endpoint.json -NoNewline -Value '{"url":"https://name.trycloudflare.com"}'
curl.exe -X POST https://<your worker>/api/song/endpoint `
  -H "Content-Type: application/json" `
  -H "x-song-key: $env:SONG_ENDPOINT_KEY" `
  --data-binary "@endpoint.json"
```

Use `--data-binary @file` rather than inline JSON: on PowerShell, quoting inline
JSON through curl mangled it and the site rejected the write.

A named tunnel on a domain you own removes this problem entirely — one hostname
that survives restarts — at the cost of a `cloudflared tunnel login` and DNS.

The address is also kept per browser under `songApi` in `localStorage` when
someone pastes it into **Connect my model** in the header. That value is tried
before the published one and wins over it; paste an empty value to forget it.

To give the deployed page a default instead, set `SONG_API` on the Worker, in
one of two places:

- **Dashboard:** Workers & Pages → `browser` → Settings → Variables and Secrets →
  **Add** → name `SONG_API`, value the tunnel origin → Save. Then Deploy for the
  variable to be picked up by that version.
- **Repo:** the `vars` block in `wrangler.jsonc`, applied by the next deploy.

Either way the value is the **origin only** — `https://name.trycloudflare.com`,
with no trailing slash and no path, because the Worker and the page append
`/api/song` themselves. A pasted address in the page still wins over it.

`SONG_API` is only a fallback for a site that has never been told about a tunnel,
because a published address replaces it as the page's first choice. Leave it unset
unless you run a named tunnel with a hostname that does not change.

Cross-origin calls already work: `song_service` sends
`Access-Control-Allow-Origin` and answers the JSON preflight with 204, so the
page's `POST /api/song` is not blocked from the Worker's origin.

The honest limits: the tunnel is only up while your PC, the service and
`cloudflared` are all running; Quick Tunnels have no uptime guarantee and are
meant for development; and anything holding the address can render songs on your
GPU and use `yt-dlp` through `/pc`, since none of it has authentication. Sleeping
is part of that list — this PC is set to sleep after fifteen minutes on power,
which takes the tunnel and the model off the network until it wakes.

### What the deploy needs

One Workers AI binding and no secrets. `wrangler.jsonc` declares
`"ai": {"binding": "AI"}`, which voice search uses on the free allowance:

```
npx wrangler deploy
```

Workers Builds does the same on push to `main`: it runs `bun install` (which
installs `wrangler` from `devDependencies`) and deploys. Nothing in
`song_service/` is uploaded — that directory is local-only and gitignored for
models.

No build step involves the model address: `SONG_API` is optional and the page
can take one at runtime, so pointing the site at a new tunnel never needs a
redeploy.

### ACE-Step cannot be installed on Cloudflare

Worth stating plainly, because it comes up: ACE-Step is Python, PyTorch and a
3.5B checkpoint — Workers run JavaScript/WASM only, and need CUDA for that model.
It cannot be packaged into the Worker, so the only place it runs is
`song_service/` on this machine.

## Downloads on the deployed site

Static hosting cannot run yt-dlp, so downloads need a real backend. That is
`downloader_service/` — Flask, yt-dlp and a bundled ffmpeg, no ML, its own short
dependency list.

```
POST /api/download          {"url": "..."}  -> 202 {"token": "...", "status": "queued"}
GET  /api/download/<token>                  -> {"status": "queued|downloading|processing|complete|error", "percent": ...}
GET  /download_file/<token>                 -> the video file, then the job is deleted
GET  /health                                -> {"ok": true}
```

Downloads are asynchronous because one outlasts the request timeout of most
hosts: the page starts a job, polls it for progress, then fetches the file.

The shortest route needs no hosting at all: the tunnel on this machine already
carries the downloader under `/pc`, which is what makes **Top 100** and TikTok or
Dailymotion playback work on a phone. Nothing to deploy, nothing to paste — just
`tools\start_services.ps1` with the tunnel up.

To run it on a host instead:

1. Deploy the service somewhere that runs Docker — Render's free tier works and
   `render.yaml` at the repo root is a ready blueprint (New → Blueprint → pick
   the repo).
2. Put its URL in the Worker's `DOWNLOAD_API` variable — either in
   `wrangler.jsonc`, or in the dashboard under Settings → Variables. The Worker
   then proxies `/api/download` and `/download_file/...` to it, so the page stays
   same-origin and needs no CORS.
3. Only if the page is hosted somewhere without the Worker, set
   `window.DOWNLOAD_API` in `public/config.js` instead; the page then calls the
   service directly.

The same page works against the desktop app with nothing configured, because
there the app answers on its own origin.

### The honest limits

- **Cloud IPs get refused.** YouTube answers datacenter addresses with "Sign in
  to confirm you're not a bot". The identical link usually downloads fine from
  the desktop app on a home connection. `YT_COOKIES_FILE` can help; never commit
  that file.
- Free instances sleep when idle, so the first download after a pause waits a
  few seconds for the container to wake. `https://<your worker>/api/health`
  says whether the service is configured, reachable and holding cookies, which
  is faster than diagnosing a failed download.
- The service caps work so a public endpoint cannot be used as a free download
  farm: `MAX_HEIGHT` (720 by default), `MAX_ACTIVE_JOBS`, `MAX_JOBS_PER_IP`.

## Voice search

The microphone button records a clip and sends it for transcription. Locally it
uses faster-whisper, falling back to the Windows speech recogniser; on the
Worker it uses Workers AI. Silence below a confidence threshold is ignored
rather than turned into a query.

## Voice fit

Press **Fit voice** next to Search, record one phrase, and the same phrase comes
back in the key and tone of the song the page has open. A card's **Remix** panel
makes that card the target; with nothing open it fits to a neutral A minor at
110 BPM.

All of it runs in the tab — no model, no upload, and the words are never read:
only the sound is moved and shaped.

1. **Pitch.** The median fundamental of the phrase is measured, then moved to the
   nearest octave of the song's root, so a voice never has to jump an octave to
   land in the key. Shifting is resample plus a correlate-and-overlap time
   stretch, so the phrase keeps its length. The stretch lays four grains over
   every sample (a quarter-grain hop): at half overlap the splice between grains
   is left bare, and on a held note it flutters at the grain rate, which is the
   rattle heard when a fitted phrase is played back. The position search that
   keeps the grains continuous is bounded to a quarter hop: a grain allowed to
   wander a whole hop (11.6 ms) lands a pitch period or more out of phase with
   the older grains still underneath it, and the four sum with a beat at the hop
   rate - the interference under the whole phrase. Measured on a real 2.5 s
   reference, the 86 Hz modulation added by a shift falls from 20.9/19.0/11.7 dB
   to 15.0/14.3/3.1 dB for +3/-3/+5 semitones, against a 12.1 dB floor that the
   reference's own syllables put there.
2. **Tone.** Per-genre filters, drive, room and a half-beat echo: `dub` and
   `ambient` are mostly space, `trap` and `riddim` almost none, `lo-fi` is a 4.2
   kHz ceiling, `trance` and `house` keep the top end. The profile table is
   `FIT_PROFILES` in `public/index.html`.
   The room is generated, not sampled, but it is not raw noise: the tail is
   lowpassed twice with the cutoff closing from 6 kHz to about 900 Hz as it
   decays, starts twelve milliseconds late so a consonant is never inside its own
   blur, and is highpassed at 140 Hz so it carries the room rather than the
   voice's low end. Its level is set by `FIT_ROOM_POWER` with the convolver's own
   normalise off, because normalise scales the impulse to equal power whatever
   its shape and a darker tail would then land louder on a voice. Measured on
   speech at the wettest profile, the part of the tail above 4 kHz is 4 dB lower
   than the old noise tail, the 1-4 kHz part 2 dB lower, and the phrase's own
   envelope still matches the dry take at 0.88.
3. **Output.** Play it, or save 16-bit mono WAV. The result is trimmed under full
   scale because dry, room and echo add up on a loud phrase.

The microphone needs a secure page: the deployed https address or the app on this
PC. A plain `http://192.168.x.x:5001` on the phone will be refused by the browser
— that is the browser's rule, not a setting here.

Measured in the browser: a 220 Hz tone moved to 174.0 Hz at −4 semitones and
261.0 Hz at +3, with the phrase length unchanged; a two-second phrase renders in
about 60 ms.

### The model sings; your voice is the measurement

**Sing the lyrics** in the Fit voice panel — and **Generate** or **Song designer**
once a phrase has been fitted — does not put your recording in the track, and does
not clone it. The model sings the lyrics itself, so the melody, phrasing and
timing belong to the song it is singing over:

1. the lyrics come from the language model, or from the designer's panel;
2. ACE-Step renders the whole song — its own sung vocal and the backing together;
3. `demucs` splits that render into a vocal stem and a backing stem;
4. the vocal is measured against your recording: the service takes the median
   pitch of each, folds the difference into the nearest register (a voice an
   octave away is the same voice) and applies it as a semitone shift on a
   half-semitone grid, never beyond ±7 — and the shift is **formant
   preserving**: the pitch moves, then the source's own spectral envelope is
   divided back over it, so the same singer sounds higher or lower rather than
   thin;
5. the shifted vocal is laid back over the same backing, matched to it and the bed
   ducked lightly under it: `LEAD_DB` (0 dB) and `DUCK_UNDER_VOICE` (20%). Measured
   on all twelve real renders in the service's logs and their separated vocals,
   that leaves the voice 1.3-2.4 dB over the music under a word (mean 1.7), the
   music dipping 1.6-1.9 dB; at 1.5 dB over a 45% duck the same songs measured
   2.8-3.9 dB with the music dipping 4.0-5.1 dB, and the song was hard to hear
   under the lyric.

To re-measure that after any change to the mix, `tools/check_mix_balance.py`
prints every separated render's figure with the current constants and with the
pair they replaced, and exits nonzero if the mix no longer matches the service's
own `_mix_separated`. It reads the stems `demucs` writes, which stay out of the
repository (`build/*.wav` is ignored), so they are made from the renders in the
service's logs first:

```
<service-venv-python> -m demucs --two-stems=vocals -n htdemucs -o build/stems <install>/song_service/logs/song_<id>.wav
<service-venv-python> tools/check_mix_balance.py            # defaults to build/stems/htdemucs
```

Your recording is only a measurement: its pitch sets the semitone shift, and the
Fit voice panel's tone profile sets the low-pass ceiling and the room. Nothing you
said into the microphone is in the song, and no voice is copied — the singer is
the model's own.

That is what makes it sung rather than spoken: the melody comes from ACE-Step and
is locked to the backing by construction, instead of a text-to-speech line being
laid over an unrelated track.

`demucs` is installed by the song service's own `install.py` (it is listed in
`song_service/models.json`), so there is no fourth environment to set up.
`voice_service` is still installed and started, but it is **no longer the
singer**: see `voice_service/README.md`. Only **8090** is ever published.

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
`GET /api/lyrics/<token>` for the words when the write outlasts the tunnel).

**Voice service** (loopback only, reached by the song service): `POST
/api/voice/sing?text=…` with the reference WAV in the body, `GET
/voice_file/<token>`, `GET /health`.

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
| `YT_COOKIES_FILE` | downloader | Optional yt-dlp cookies file |
| `YT_COOKIES` | downloader | The same cookie jar as text, for hosts that only give you environment variables |
| `SONG_WARM` | song service | `1` loads the song model at startup instead of on the first song (set by the launcher) |
| `LYRICS_DEVICE` | song service | `cuda` or `cpu` for the lyric model; unset means cuda when torch sees a card |
| `LYRICS_INLINE_SECONDS` | song service | How long `POST /api/lyrics` holds the request before answering `202 {token}` (default 40 s) |
| `ACE_CPU_OFFLOAD` / `ACE_DTYPE` | song service | Page the weights in and out (default `1`), and the model dtype (default `bfloat16`) |
| `ACE_STEPS` / `ACE_MAX_SECONDS` | song service | Diffusion steps (30) and the ceiling on song length (120 s) |
| `SONG_ENDPOINT_KEY` | Worker secret | Proves a tunnel publish came from this PC; `POST /api/song/endpoint` without it is **403** |
| `x-song-key` | request header | The same key, sent by the launcher when it publishes |
| `/api/health` (Worker) | Worker | Reports whether the download service is configured, awake and holding a cookie jar |
| `/health` (song service) | song service | `device`, `gpu`, `vram_gb`, `loaded`, `warming`, `warming_seconds`, `warm_failed`, `lyrics_loaded` |
| `/api/song/endpoint` | Worker | The tunnel address the PC last published; `GET` is public, `POST` needs `x-song-key` |
| `/pc/<path>` | song service | Forwards to the downloader on 8080, so a phone can use Top 100, the player and downloads |

## Building the environments

### One script, one environment (the whole application)

**Double-click `install.cmd`.** It installs, starts the services and registers the
logon entry, so nothing is ever typed and a reboot brings everything back. That is
the whole setup on a machine with an NVIDIA card.

Nothing has to be installed first, and no administrator prompt appears: a PC that
has never run Python gets a portable `uv` (15 MB) and a portable `git` (40 MB) in
`tools\bin`, and `uv` fetches its own Python 3.11. The only thing the script asks
of the machine is about 20 GB of free disk for torch, the packages and the ~10 GB
of weights, and an NVIDIA driver 560 or newer. Without a card it still finishes and
runs on the processor, and says so: minutes to hours per song instead of seconds.

Before it downloads anything, `-Check` reports the card, the driver version and the
free space, plus every package the environment already has:

```
.\install_all.ps1 -Check
```

The same thing from a shell, or with pieces skipped:

```
.\install_all.ps1 -Check              # what is installed, what is missing; changes nothing
.\install_all.ps1 -Start -Autostart   # install, start now, come back at logon
.\install_all.ps1 -NoWeights          # packages only (~10 GB of weights skipped)
```

`install_all.ps1` fills the single `.venv` at the root with everything the three
running services need — the site, the downloader and the song service — and then
downloads the ACE-Step weights. `tools\start_services.ps1` picks that one
environment up on its own once the script has written its marker.

`voice_service` is not folded in: chatterbox pins `numpy==1.26.4` and
torch 2.6.0, which pip cannot hold beside the song service's `numpy==2.4.6` and
torch 2.14.1+cu126, and nothing in the singing path calls it any more. It keeps
its own environment, described below.

### The per-service environments (fallback, and what the launcher used to do)

There are three Python environments, not one, one per service. A clone contains
**none** of them: `.venv`, `song_service\.venv` and `downloader_service\.venv` are
all gitignored, as are the model weights.

All three use **Python 3.11** and are built with **`uv`**, in this repository
root. Run the commands from the root, in this order.

### Why `uv` and not `python -m venv`

On this machine `python` is not Python: it resolves to the Microsoft Store stub
at `…\WindowsApps\python.exe`, which only answers *"Python was not found; run
without arguments to install from the Microsoft Store"*. `py -3.11 -m venv` fails
too — the interpreter `uv` installed is registered with the `py` launcher but
cannot be started through it. `uv` is the one that works, installed at
`%USERPROFILE%\.local\bin\uv.exe`; if it is missing, use its installer from
<https://astral.sh/uv>.

`--seed` is not optional. `uv venv` without it creates an environment with **no
pip**, and the install on the next line dies with *"No module named pip"*.

| Environment | Built by | Used for |
| --- | --- | --- |
| `.venv` | you (step 1) | the site on 5001 |
| `downloader_service\.venv` | you, or the launcher on first run (step 2) | yt-dlp on 8080 |
| `song_service\.venv` | you (step 3) — the launcher refuses to invent it | the model on 8090 |

### 1. The site — `.venv`

```
uv venv .venv --python 3.11 --seed
.venv\Scripts\python -m pip install -r requirements.txt
```

Check it before going on — this is the exact import that a partially installed
environment breaks, and the reason `requirements.txt` carries the note about
PyInstaller:

```
.venv\Scripts\python -c "from flask_socketio import emit, SocketIO; print('site env ok')"
```

### 2. The downloader — `downloader_service\.venv`

```
uv venv downloader_service\.venv --python 3.11 --seed
downloader_service\.venv\Scripts\python -m pip install -r downloader_service\requirements.txt
```

Check:

```
downloader_service\.venv\Scripts\python -c "import flask, yt_dlp, curl_cffi, imageio_ffmpeg; print('downloader env ok', yt_dlp.version.__version__)"
```

You can skip this step: `tools\start_services.ps1` builds this environment
itself when the folder is missing, using the same two commands. Doing it by hand
is only for when the launcher's interpreter is not the one you want.

### 3. The song service — `song_service\.venv`

This one is deliberately manual. It installs a CUDA build of torch and about
10 GB of weights, so the launcher does not guess: it prints *"run
song_service\install.py first (it installs the CUDA torch)"* and starts nothing.
And `install.py` installs into whatever interpreter runs it — it does **not**
create the venv, so the venv has to exist first:

```
uv venv song_service\.venv --python 3.11 --seed
song_service\.venv\Scripts\python song_service\install.py --check    # what is present, changes nothing
song_service\.venv\Scripts\python song_service\install.py            # packages, then both models
```

The path matters: `install.py` lives in `song_service\`, so from the repository
root it needs the `song_service\` prefix. Written without it — the way this README
first had it — Python answers *"can't open file '…\scrape\install.py'"*.

Checks, after it finishes:

```
song_service\.venv\Scripts\python install.py --check     # every package and both weight folders
song_service\.venv\Scripts\python gpu_check.py           # torch sees the card
```

`install.py` also takes `--packages` (code only) and `--weights` (models only)
when only one half is missing. The weights land in `song_service/models/`, which
is gitignored.

`song_service\requirements.txt` is the same package list in readable form — torch
and torchaudio from the CUDA index, the transformers/hub pins, ACE-Step from git,
and the helpers — for when you want to see the pins without opening `models.json`.
It is not the installer: only `install.py` downloads the weights.

### 4. The voice service — `voice_service\.venv`

Optional, and separate from the song environment on purpose: it installs the
cloning model and its own torch, and folding that into `song_service\.venv` would
risk the 3.5B music model that already works. Without it the page still writes
songs; it just has no singer to put the lyrics in your voice.

```
uv venv voice_service\.venv --python 3.11 --seed
voice_service\.venv\Scripts\python.exe voice_service\install.py --check    # report, change nothing
voice_service\.venv\Scripts\python.exe voice_service\install.py            # install everything
```

`install.py` is the whole install, and it is written to not leave the machine on
CPU torch: `chatterbox-tts` pins `torch==2.6.0`, and PyPI's wheel for that version
is the CPU build, so a plain `pip install -r requirements.txt` replaces CUDA torch
with one that falls back to the processor — silently. The script installs the
packages and then restores the `+cu126` wheels of the same version. `--check`
reports the torch build and whether a card is visible; on this machine it prints
`torch 2.6.0+cu126` and `cuda True (NVIDIA GeForce RTX 4060 Ti)`.

Weights download on the first request rather than at install time. The launcher
tests for `voice_service\.venv` and skips the service when it is missing, so an
uninstalled voice model never stops the rest of the stack from starting. Full
detail, including the limits of the model, is in `voice_service/README.md`.

### 5. Everything at once

```
powershell -NoProfile -ExecutionPolicy Bypass -File tools\start_services.ps1
```

What success looks like:

| Check | Expected |
| --- | --- |
| <http://127.0.0.1:5001/> | the page |
| <http://127.0.0.1:8080/health> | `{"active_jobs":0,"ok":true,"max_height":720,…}` |
| <http://127.0.0.1:8090/health> | `"device":"cuda"`, and `"loaded":true` after about 51 s |
| `curl https://<worker>/api/song/endpoint` | the tunnel address the launcher just published |
| `build\launcher_trace.txt` | a line with the time of this run |

The launcher also fetches `cloudflared` if it is missing (pinned and signature
checked). What it cannot supply is `build\song_endpoint.key`: without that the
tunnel runs but the site is never told the address, so a phone still reports the
AI as off. Copy the file across, or set `SONG_ENDPOINT_KEY` on the Worker to a new
value and write the same value into the file.

Each service can also be run on its own, from its own folder, with the
interpreter of its own environment:

```
.venv\Scripts\python app.py                                  # the site, port 5001
downloader_service\.venv\Scripts\python app.py               # downloader, port 8080
song_service\.venv\Scripts\python app.py                     # the model, port 8090
```

The downloader needs no torch — `downloader_service\requirements.txt` is Flask,
`yt-dlp`, `curl_cffi`, `imageio-ffmpeg` (a bundled ffmpeg) and `requests` — which
is why it starts in seconds where the song service takes a minute and 10 GB.

### Do not copy a venv between machines

A venv is not portable: `pyvenv.cfg` holds an absolute `home` path to the
interpreter that built it, and every script in `Scripts\` is a stub ending in that
path too. This repository has already been bitten once — when the Windows profile
here changed from `bpier` to `soluzka_3eenj0v`, all three venvs still pointed at
`C:\Users\bpier\AppData\…\Python311`, which no longer existed, and every service
failed to start until those paths were repointed. Recreate the environments on the
new machine rather than copying the folder.

## Two Workers: the site, and the pool

There are two Cloudflare bases, and they do different jobs.

| Base | File | What it is |
| --- | --- | --- |
| `browser.bpiersing.workers.dev` | `worker.js` | the site: page, search, downloads, and a copy of the model proxy |
| `song-pool.bpiersing.workers.dev` | `pool-worker.js` | **Cloudflare 2**: the GPU pool, and a model base of its own |

The second one holds only the list of machines — no assets, no page — so it
survives a redeploy of the site, can live on another account, and serves the AI
by itself: `/api/song`, `/api/song/<token>`, `/api/song/voice`, `/api/lyrics`,
`/song_file/<token>` and `/last_song` are forwarded to a machine from the pool.

The weights are not on Cloudflare and cannot be: ACE-Step is ~10 GB and needs
CUDA. Cloudflare holds the address book and forwards the requests; the cards run
the model.

```
npx wrangler kv namespace create song-pool
npx wrangler deploy --config wrangler.pool.jsonc
npx wrangler secret put JOIN_KEY --config wrangler.pool.jsonc
```

The site reads the pool from there (`POOL_API` in `wrangler.jsonc`) and forwards
every machine's announcement to it, falling back to its own KV when the pool base
is unreachable.

**Measured on Cloudflare 2 itself:** `GET /api/song/latest` → `200` with the job
JSON, and `HEAD /last_song` → `200 audio/wav, Content-Length: 34542420`. With a
dead address added to the pool, three of three `/last_song` requests still
answered `200`; removing it returned the pool to one machine.

A machine joins by running the agent once (on Windows, `install.cmd`; on Linux,
`install.sh` with the systemd unit). A browser cannot hand its GPU to a site, and
WebGPU runs inside the visiting tab, which cannot hold this model — so "joining"
is that one local step. After it, the machine rejoins on its own: every launcher
run posts its address to the site and to Cloudflare 2 directly, so the pool lists
every agent that is awake, and nothing depends on the owner's PC.

### The badge in the header

`Pool · N GPUs` reads the pool from Cloudflare 2 every minute. Pressing it starts
this PC's agent through `songsvc://`, which is registered per user under
`HKCU\Software\Classes` — **no administrator prompt** — and with no GPU online its
tooltip says so. That is the only thing anyone clicks: the site already answers
without it when another machine is in the pool.

### Adding a machine without much work

The site serves the installer for a fresh Linux GPU box, so nothing has to be
cloned or unpacked by hand:

```
curl -fsSL https://browser.bpiersing.workers.dev/join.sh | bash -s -- --key <join key>
```

It installs the environment, the CUDA wheels and the models, installs the boot
service and starts the stack; the machine then publishes itself to Cloudflare 2 and
the site uses it. On a machine with a cloud-init field, paste
`deploy/gpu/cloud-init.yaml` with the three values filled in: the same thing
happens with no terminal at all.

### Starting a GPU from the site (Kaggle)

When the pool is empty the header shows one more button, **Start a GPU**. It asks
the site to boot a free Kaggle GPU, which runs this project's own boot, opens a
tunnel and joins the pool by itself — so `AI ready · Pool` can appear with nobody
at a keyboard.

Colab is not what happens here, and cannot be: Google gives no API that starts a
Colab runtime, so a web page can never boot one. Kaggle's token can, which is why
the button is wired to Kaggle.

**What has to exist on the site**, as Worker secrets (never in the repository):

| Secret | Where it comes from |
| --- | --- |
| `KAGGLE_USERNAME` | the `username` field of your `kaggle.json` |
| `KAGGLE_KEY` | the `key` field of the same file |
| `SONG_JOIN_KEY` | `build\song_endpoint.key`, so the started card can add itself to the pool |

**Requirements at Kaggle** — all of these were hit for real before the button did
anything useful:

| Requirement | Why | What it looks like when missing |
| --- | --- | --- |
| A verified phone number (kaggle.com/settings → Phone Verification) | Kaggle gives no GPU *and* no internet to an unverified account | the version sits on **Queued** and never starts, or starts on CPU with `Could not resolve host: github.com` |
| A free GPU hour left this week | the free tier allows ~30 hours, shared across all kernels | the run is refused or queues indefinitely |
| `enableInternet` actually granted | the kernel clones this repository and installs ~10 GB | `NO INTERNET in this kernel` printed by the kernel's first check |
| A token that may **read** the kernel, not only push it | `kaggle kernels output …` fetches the log | `Permission 'kernels.get' was denied` — pushing a version still works, so this can look like a broken kernel |
| A private kernel | the pushed code is your account's, and it carries the join key | a public kernel would publish the key with the code |

The queue is Kaggle's, not the site's: **Queued** means no worker has been granted
yet, the log stays empty until it becomes **Running**, and a version that has sat
there for half an hour does not pick itself up — cancel it and start again.

**Building `build\kaggle.json`, once per machine**

1. Sign in at <https://www.kaggle.com/settings/api> and press **Create New Token**.
   A `kaggle.json` downloads; it looks like this, and nothing else is needed:

   ```json
   { "username": "your-kaggle-name", "key": "a long hex string" }
   ```

2. Put that file in this project as `build\kaggle.json`. A placeholder with
   `PASTE_…` values is already there: open it in Notepad and replace the two
   values with the ones from that page, or simply overwrite the file with the
   downloaded one. It must stay at that path; `build\kaggle.json` is in
   `.gitignore`, so it cannot be committed.

3. Store the secrets and deploy:

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File tools\set_kaggle.ps1
   powershell -NoProfile -ExecutionPolicy Bypass -File build\wr.ps1 deploy
   ```

   The script refuses the placeholder text rather than storing a fake key, feeds
   each value to wrangler through a temporary file on standard input (never the
   command line, never the console), and deletes that file afterwards. If wrangler
   is missing it says to run `build\wr.ps1 deploy` once first.

4. Check it took, without starting anything:

   ```powershell
   curl.exe -s https://browser.bpiersing.workers.dev/api/gpu/status
   # {"lastStart":0,"status":null,"configured":true}
   ```

   `configured: false` means the Worker has no Kaggle secret yet, and the button
   answers with exactly that instruction. `started` in `POST /api/gpu/start`
   means Kaggle accepted the run, not that a GPU is on the pool yet: the card has
   to boot, install, load ~10 GB of weights and publish, which takes minutes.

**Pushing a kernel version by hand** — the button is not the only way; it makes the
same call this does. Use it when the button is not on the page (a card is already in
the pool, so the header hides it) or when a running boot has to pick up code that
changed after it cloned the repository:

```bash
curl -s -X POST https://browser.bpiersing.workers.dev/api/gpu/start
# {"configured":true,"started":true,"note":"…","said":"{\"versionNumberNullable\":15,…}"}
```

```powershell
Invoke-RestMethod -Method Post -Uri https://browser.bpiersing.workers.dev/api/gpu/start
```

No body and no key: the Kaggle token is a Worker secret and the endpoint reads it
there. `started: true` means Kaggle accepted the version, not that a card is
running — `said` is Kaggle's own answer, where `error` shows a refusal and
`invalidDatasetSources` names a dataset it could not mount. A spent KV write
allowance does not block the push: the cooldown and boot-window timestamps are
best-effort, so the request still reaches Kaggle (it used to answer `error code:
1101` instead).

This **replaces** the session that is running. Kaggle attaches the new version to
the same kernel rather than running it beside the old one, so pushing kills a live
card: its tunnel dies, the pool empties, and the new boot has to warm again before
songs render. It also spends the week's GPU hours.

**When the card has to run code you just committed** — a push alone is not enough.
The pushed kernel never clones GitHub: it runs `repo/public/song_gpu_offline.py` out
of the `song-gpu-code` **dataset**, staged by `tools\kaggle_pack.ps1` with robocopy
from this working tree. A commit changes nothing on that card until the dataset is
rebuilt, so the order is repack, then push:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\kaggle_pack.ps1 -CodeOnly -Create
curl.exe -s -X POST https://browser.bpiersing.workers.dev/api/gpu/start
```

`-CodeOnly` uploads the repository copy and its unpacked Linux packages, not the
10.7 GB of weights. The exact tree Kaggle will mount is the staging folder, so it can
be checked before the upload:

```powershell
Select-String -Path build\kaggle_pack\code\repo\public\song_gpu_offline.py -Pattern 'the video downloader'
```

A dataset Kaggle cannot mount is named in the push reply as `invalidDatasetSources`
(for example a weights dataset that does not exist on that account); the boot then
has only the kernel's own Output and, if the kernel has a network, Hugging Face.

5. Push the code that makes this work:

   ```powershell
   git add worker.js tools/set_kaggle.ps1 README.md .gitignore
   git commit -m "Start a free GPU when the pool is empty"
   git push
   ```

   The credential file is deliberately **not** in that list. Never `git add
   build\kaggle.json` or `git add -A` with it present; if it was ever committed,
   revoke the token at Kaggle, create a new one, and treat the old key as burnt.

**Limits worth knowing:** one start per thirty minutes (the cooldown is the
`gpu:started_at` timestamp, kept in the **pool** Worker's KV at `/state/gpu:started_at`
because the site's own KV runs out of daily writes and every GPU request failed once it
did), the weekly GPU allowance is Kaggle's, the kernel is private to your account, and a
started card ends when its kernel session ends. The pool entry disappears with it, and
the header goes back to `Start a GPU`.

```bash
curl -s https://song-pool.bpiersing.workers.dev/state/gpu:started_at   # {"key":…,"value":"…"}
```

Reads are open; a write needs `x-join-key`, which only the site Worker sends.

A token that starts with `KGAT` is a bearer token, not a username-and-key pair;
the Worker sends it as `Authorization: Bearer`, and only falls back to basic auth
for an older `kaggle.json`. Sending a `KGAT` token as basic auth is answered with
`401 Unauthorized`. The `username` field may hold a display name (it can contain
spaces and capitals) while the API wants the account handle (lowercase, no
spaces), so the Worker reduces it to that form before it is used in a slug. No
name is written anywhere in this repository; the handle is only ever read from
the Worker's secret.

#### What is never pushed

Every credential and every machine-specific address is gitignored, so a clone
does not carry one and `git status` stays quiet about them:

| Path | Why it is local | Where it belongs instead |
| --- | --- | --- |
| `build\song_endpoint.key` | publishes a machine into the pool | nowhere: `tools\set_kaggle.ps1` writes `SONG_JOIN_KEY` as a Worker secret |
| `build\kaggle.json` | boots a GPU on your account | nowhere: your Kaggle account. **This is the one file that must never be shared**, and the packer reads it rather than taking a key on the command line |
| `build\tunnel_url.txt` | one session's tunnel address, useless tomorrow | nowhere |
| `song_service\models\` | ~10 GB of ACE-Step weights, and the `dataset-metadata.json` the packer writes there | Hugging Face (`ACE-Step/ACE-Step-v1-3.5B`), re-fetched by `song_service\install.py --weights` |
| `build\kaggle_pack\` | 11 GB of repo copy, Linux packages and ACE-Step source, all rebuilt by the packer | your own private Kaggle dataset, uploaded by `tools\kaggle_pack.ps1 -Create` |
| `build\kaggle_pack\requirements-offline.txt` | the filtered list (`tokenizers==0.22.1`, no torch, no chatterbox, no cutlet) | regenerated on every `-Packages` run |
| `.venv\`, `song_service\.venv\` | the Python environments | rebuilt by `install.cmd` (see [One script, one environment](#one-script-one-environment-the-whole-application)) |
| `tools\bin\*.exe` | `cloudflared.exe`, a 53 MB binary fetched on first run | fetched again on first run |

When committing, name the files (`git add worker.js tools/set_kaggle.ps1 README.md`) rather
than `git add -A`; if a credential is ever committed, revoke it at the provider
first and treat the old value as burnt.

**The packer is tracked; only what it produces is ignored.** `tools\kaggle_pack.ps1`
and `public/song_gpu_offline.py` are normal files in the repository — the packer is
the thing that rebuilds everything in the table above from a clone. Nothing it
generates has to be committed for it to work, and nothing it reads from the
account is ever committed.

#### If someone else wants the same setup

The datasets are not shared, and they do not need to be: each person makes their
own, under their own account, with the same script. Nothing in this repository
names an account.

1. Sign up at kaggle.com and **verify the phone number**: Kaggle only grants GPUs
to verified accounts, and an unverified one is what leaves a pushed kernel
sitting at *Queued* forever.
2. Account → Settings → API → **Create New Token**, and save the file as
   `build\kaggle.json` (`tools\set_kaggle.ps1` does this without printing the key).
3. Get the weights, since `song_service\models\` is ignored:
   `.venv\Scripts\python song_service\install.py --weights`
4. Run the four packer commands listed in [The offline datasets](#the-offline-datasets-mounting-instead-of-downloading)
   above, in the order they appear there.

`-Create` makes `<handle>/song-gpu-code` and `<handle>/song-gpu-weights` **private**
(`isPrivate` in the metadata the packer writes), so the account name is not
published alongside the weights. `worker.js` derives both slugs from the account
that owns the token and can be pointed elsewhere with `KAGGLE_CODE_DATASET` and
`KAGGLE_WEIGHTS_DATASET`.

The packer runs on Windows: it uses `robocopy` for the repository copy and `uv`
for the package target, and says so at the top if either is missing.

### The offline datasets (mounting instead of downloading)

A pushed kernel does not always get the network, so the packer builds two private
datasets that the kernel mounts at `/kaggle/input` and never has to download from:

| Dataset | Contents | Read at |
| --- | --- | --- |
| `<handle>/song-gpu-code` | the repo, `pylibs` (Linux packages), `ace-step` source, `cloudflared-linux-amd64` | `/kaggle/input/song-gpu-code/repo` |
| `<handle>/song-gpu-weights` | `song_service\models` uploaded as it is | `/kaggle/input/song-gpu-weights/ACE-Step-v1-3.5B` |

The weights dataset is `song_service\models` itself, with a `dataset-metadata.json`
written beside the models. It is gitignored, so the packer never stages an 11 GB copy
and nothing about it reaches GitHub.

```powershell
powershell -ExecutionPolicy Bypass -File tools\kaggle_pack.ps1              # repo + ACE-Step + cloudflared
powershell -ExecutionPolicy Bypass -File tools\kaggle_pack.ps1 -Packages    # + the Linux packages
powershell -ExecutionPolicy Bypass -File tools\kaggle_pack.ps1 -Weights     # report the weight sizes
powershell -ExecutionPolicy Bypass -File tools\kaggle_pack.ps1 -Create      # create/update both datasets
```

`-Create` reads `build\kaggle.json` for the token (written by `tools\set_kaggle.ps1`),
so the key is never typed into a shell or a file that gets committed.

#### The package target, exactly

The kernel runs **Python 3.13.16** on a **Tesla T4 (15360 MiB)** with **torch 2.11.0+cu130**
already installed, so the packed packages are built for that interpreter and platform:

```
uv pip install --target build/kaggle_pack/code/pylibs ^
    --python-version 3.13 ^
    --python-platform x86_64-manylinux_2_28 ^
    --only-binary :all: --no-cache ^
    -r build/kaggle_pack/requirements-offline.txt
```

`--only-binary :all:` is what makes the result usable offline: a package that could
only be built from source would be built for Windows here and fail on Kaggle. When it
cannot be satisfied, the fix is to pin the version that has a `cp313` `manylinux`
wheel, not to drop the flag.

Three things are deliberately left out of `requirements-offline.txt`:

| Left out | Why |
| --- | --- |
| `torch`, `torchaudio`, `torchvision` | the image already has torch 2.11.0+cu130; the repo pins a CUDA-index build that cannot be reproduced offline |
| `chatterbox-tts` | it pins `transformers==5.2.0`, ACE-Step needs `4.57.6`, so they can never share an environment; the voice weights also come from the Hub at runtime |
| `cutlet` | its dependency `mojimoji` is a source-only C extension with no Linux wheel; ACE-Step imports it lazily, for Japanese romanisation only |

Checking which requirement pulls those in:

```
grep -n -i "cutlet\|mojimoji\|py3langid" song_service/requirements.txt voice_service/requirements-shared.txt
grep -v -i '^cutlet$' build/kaggle_pack/requirements-offline.txt > /tmp/r2.txt
```

The second line is how the filtered list was produced after `cutlet` was found at
`song_service/requirements.txt:45`.

#### The pin that broke the first real run

The first run that reached a worker got as far as importing ACE-Step and stopped at:

```
ImportError: tokenizers>=0.22.0,<=0.23.0 is required for a normal functioning of this
module, but found tokenizers==0.23.3.
```

Kaggle's image carries `tokenizers` 0.23.3, transformers 4.57.6 refuses anything above
0.23.0, and `pip install transformers` alone will not correct a version that is already
"satisfied". `tools\kaggle_pack.ps1` therefore appends `tokenizers==0.22.1` to the
offline list, and the offline boot prefers the mounted `pylibs` directory before
anything the image ships.

0.22.1 and not 0.23.0, even though that is the ceiling transformers names: PyPI has
no 0.23.0 release, so asking for it fails the whole resolution with `there is no
version of tokenizers==0.23.0`.

### One installer, and what it installs

`join.cmd` is the only install the site hands out, and it installs **everything**:
the site on 5001, the downloader on 8080, the song service on 8090, the CUDA
wheels and both model checkpoints — the same result as `install.cmd` in this
repository, from a machine that has cloned nothing. It also writes the join key,
starts the stack, publishes the tunnel address to the site and to Cloudflare 2,
and registers the logon entry.

| Step | What happens |
| --- | --- |
| double-click | `join.cmd` fetches `join.ps1` from the Worker |
| fetch | the repository zip (~40 MB) is unpacked into `%LOCALAPPDATA%\internet-gpu` |
| install | `install_all.ps1`: portable uv and git if missing, Python 3.11, torch cu126, every package, ~10 GB of weights |
| start | the site, the downloader, the song service with `SONG_WARM=1`, and a Quick Tunnel |
| announce | the tunnel address goes to the site and to the pool with the join key |
| return | the logon entry brings it all back after a reboot |

It takes 20–60 minutes and about 20 GB of disk on a normal connection, and the
window prints each step, so a slow download looks slow rather than broken. The
only thing it cannot supply is an NVIDIA card: without one the same models run on
the processor, which is free and correct, just slow.

### Free: a Colab T4, one click

`deploy/colab/song_gpu.ipynb` (also served at
`https://browser.bpiersing.workers.dev/song_gpu.ipynb`) is the no-cost card. Open it
in Colab, choose a free T4, press **Run all**: it installs the song service on the
notebook's own machine, opens a tunnel and prints an `https://…trycloudflare.com`
address. Nothing is installed on your computer and nothing is rented.

That address is usable two ways, and the first needs no key and no permission:

1. press **Connect my model** in the site's header and paste it. The page
   then writes lyrics and renders songs on that T4 until the notebook stops;
2. or paste the join key when the notebook asks, and the card joins the pool, so
   every visitor's click can use it with nothing pasted anywhere.

A T4 has 16 GB — twice the card here — so songs render faster while the session
lasts. The trade is the session: when Colab stops it, the card leaves the pool, and
that is what "free" costs. It is also the only free card that can run this model:
Workers AI has no music model, Containers have no GPU, and a browser tab cannot
lend its card to a page.

The trade is the session: when Colab stops it, the card leaves the pool. That is
what "free" costs, and it is the only free GPU that can run this model: Workers AI
has no music model, Containers have no GPU, and a browser tab cannot lend its card
to a page.

### Timing the words against the vocal

The lyric video used to guess when each line is sung: an even slice of the clock
per line, then a slice weighted by the bars of its section. Both drift, and a
drifting lyric video is worse than none.

`song_service/align.py` measures it instead. It is not a new model — it is the
model the service already runs, applied to a new question:

1. **Separate the voice.** demucs `htdemucs`, the same separator the voice map
   uses (`--two-stems=vocals`), leaves a stem with the singing and almost no band.
2. **Find the phrases.** A 20 ms RMS envelope of that stem, gated against its own
   peak, gives the runs where the voice is present; runs shorter than 150 ms are
   dropped and gaps shorter than 120 ms are closed.
3. **Fit the lines.** Every line is given a share of the *sung* time (not the wall
   clock) by syllable count, and each boundary is moved onto a phrase edge when
   one is within 0.4 s — a line in a lyric video starts when the singing does, and
   never creeps through an instrumental gap.

Because the input is the separated vocal, the gate has something to gate: on the
full mix a drone keeps the meter above the threshold for the whole song and the
result is the flat split again. Measured on a test song whose four voice phrases
begin at 1.2 s, 3.8 s, 7.0 s and 10.8 s, the separated run returned phrases at
1.20, 3.82, 7.02 and 10.82 s, and the first line started at 1.20 s rather than 0.

**HTTP**, from the page:

```
POST /api/align        multipart: audio=<file>, lines=<json array of strings>
  -> { lines: [{start, end}, ...], seconds, phrases: [[start, end], ...],
       source: "the separated vocal" | "the song itself", seconds_taken }
```

**Command line**, for a finished song:

```
song_service\.venv\Scripts\python.exe song_service\align.py song.wav --lines lines.json
song_service\.venv\Scripts\python.exe song_service\align.py song.wav --lines lines.json --no-separate
```

The page keeps the returned schedule with the saved song, so the card's picture,
the player and the recorded video all use one timing. When the service is not
running — a phone, another machine, the model down — the in-page estimate stays
in charge; the song is never blocked on alignment. `Re-time the words` on a saved
song measures it again, and `Record the video` measures first and then records,
which is how an older song gets its words in the right place.

Limits, stated plainly: this is line-level, not word-level. Word-level timing
needs a speech recogniser with timestamps, which is a different download and a
different set of mistakes; the aligner here trades that for no new weights at
all. And a video that was already recorded has the old words burned into it —
re-timing updates the stored schedule, and the video has to be recorded again
from it.

### The 10 GB on Cloudflare

Storage is not the obstacle — execution is. The checkpoint fits in R2 (free
storage tier, no egress fee), and `pool-worker.js` serves it at
`/weights/<key>`, streamed, so a machine can download the model from Cloudflare
instead of Hugging Face. Confirmed live: the route answers
`501 No R2 bucket is bound…` until a bucket exists.

R2 has to be enabled once in the dashboard; on this account the API currently
answers `code 10042 Please enable R2 through the Cloudflare Dashboard`. After
that:

```
npx wrangler r2 bucket create song-weights
npx wrangler r2 object put song-weights/ace-step/model.safetensors --file=<path>
```

and uncomment the `r2_buckets` block in `wrangler.pool.jsonc`. What R2 cannot do
is run inference: a Worker is a 128 MB isolate with no GPU, so the 10 GB can be
stored and delivered from Cloudflare, never executed there.

### Start at boot, before anyone logs in (optional, needs admin once)

The logon Run key needs somebody to sign in first. A Scheduled Task does not:

```
powershell -NoProfile -ExecutionPolicy Bypass -File tools\install_startup_task.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File tools\install_startup_task.ps1 -Remove
```

That needs administrator rights **once** — a task that runs at startup cannot be
created otherwise. It registers `AI song services (boot)` with trigger AtStartup,
principal S4U (the user's profile, no stored password), no window and an unlimited
run time, so the site, the downloader, the song service and the tunnel are serving
before the lock screen is answered.

## Running the whole stack on a GPU host

Cloudflare cannot run these models: a Worker is a 128 MB isolate with no GPU,
Containers stop at 4 vCPU / 12 GiB and have no GPU, and Workers AI's catalog of
73 models has no music model. The GPU is a machine — this one, or a rented one.

`deploy\gpu\` runs **everything** in one container from one environment: the site
(5001), the downloader (8080), the song service (8090) and `cloudflared`, so no
inbound port has to be opened on the host:

```
docker compose -f deploy\gpu\compose.yaml up -d --build
docker compose -f deploy\gpu\compose.yaml logs -f
```

A named tunnel gives it one permanent hostname, and `cloudflared tunnel token
song-ai` goes into `deploy\gpu\.env` as `TUNNEL_TOKEN`. Then point the Worker at
that hostname once and the page talks to its own origin for the model:

```
npx wrangler deploy --var SONG_API:https://ai.example.com
```

On a Linux GPU host it starts at boot with no terminal and nobody logged in:

```
sudo ./deploy/gpu/install_service.sh
sudo systemctl status song-stack
```

The full walkthrough — tunnel commands, GPU reservation, the systemd unit, what the
image contains — is in `deploy\gpu\README.md`.

## Troubleshooting

- **"database is locked"** — the connections use WAL and a 30 s busy timeout;
  the app and the exporter can both touch the database safely.
- **Genres missing on the live site** — run `tools/export_index.py`, confirm it
  prints `genres N/M`, then redeploy.
- **Download answers 405 or opens the source page** — `DOWNLOAD_API` is unset or
  unreachable; the page falls back to the original link.
- **Voice search does nothing** — the microphone needs a secure origin (https or
  localhost). On a LAN IP the browser blocks it.
- **The badge says "AI off"** — the song service is not running. Press **Start
  the AI**, or run `tools\start_services.ps1`; the badge turns green when
  `/health` answers.
- **The badge says `CUDA · warming 2m 30s` and keeps counting** — the model is
  still loading; fifty-one seconds is normal on this card, two minutes means
  something else is using the machine, four means look at `warm_failed` in
  `/health` and in `song_service\logs\service_err.log`.
- **The badge flips to "AI off" every time the page is reloaded, then comes
  back** — the service is answering too slowly for the probe. It is measured at
  twenty seconds in the launcher and twelve in the page; a host that misses both
  while loading is reported as `CUDA · busy loading` rather than lost.
- **A phone says "AI off" while the PC is fine** — the tunnel address is stale or
  the tunnel is down. `curl https://<worker>/api/song/endpoint` shows what the site
  holds; re-run the launcher to republish, then reload the page.
- **TikTok or Dailymotion will not play on a phone, but YouTube does** — the
  player needs `/pc/api/play` through the tunnel. Check
  `https://<tunnel>/pc/health` answers 200; if it does not, the tunnel is pointing
  somewhere else and a republish fixes it.
- **Nothing started after a boot** — compare `LastBootUpTime` with
  `build\launcher_trace.txt`. No line within a minute means the entry did not run:
  re-run `tools\install_autostart.ps1`. A line but no services means the head of
  `build\launcher_run.log` says why.
- **Start the AI does nothing** — `songsvc://` is not registered on this machine.
  Any run of `tools/start_services.ps1` registers it, including the Startup entry.
- **Lyrics take minutes instead of seconds** — the lyric model is running on the
  CPU. `/health` shows the device; unset `LYRICS_DEVICE` to let it pick CUDA.
- **"No lyric was written"** — the toast carries the model's own reason. "the
  lyric model returned too little text" means it answered three times with words
  that failed the checks; "still writing when the wait ran out" means the card was
  busy for the whole five minutes. A write that outlasts the tunnel is normal and
  is collected by polling, so it no longer reads as the model not answering.
- **A song takes about ten minutes** — 90 seconds of audio at 16 steps on one
  card. `ACE_STEPS` trades quality for time, `ACE_MAX_SECONDS` for length.
- **The tunnel answers 404 with no body** — cloudflared picked up
  `~/.cloudflared/config.yml`. Run the tunnel with
  `--config tools\bin\quick-tunnel.yml`, which the launcher already does.
- **"tunnel: could not fetch cloudflared"** — the first run needs GitHub
  reachable to download the 53 MB binary. It is retried on every launcher run;
  put `cloudflared.exe` in `tools\bin\` by hand if the machine is offline.
- **"tunnel: the download failed its signature check"** — something answered for
  GitHub that is not the signed release. The partial file is deleted; check the
  network or proxy rather than editing the check away.

`vercel.json` is a leftover deployment target that routes everything to
`ai_engine`; the Cloudflare Worker is the supported static deployment.
