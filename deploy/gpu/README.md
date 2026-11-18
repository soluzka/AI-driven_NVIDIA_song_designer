# Running the whole application on a GPU host

Cloudflare cannot run this AI. A Worker has a 128 MB isolate and no GPU, Containers
top out at 4 vCPU / 12 GiB with no GPU, and Workers AI's catalog of 73 models does
not include ACE-Step. The GPU is a real machine — yours, or a rented one — and
this directory runs **everything** on it in one container:

| Port | What |
| --- | --- |
| 5001 | the site: Flask, Socket.IO, the SQLite index |
| 8080 | the downloader: Flask + yt-dlp |
| 8090 | the song service: ACE-Step on the GPU, the lyric model, demucs, librosa |

plus `cloudflared`, so Cloudflare reaches the container without any inbound port
being opened on the host.

## 0. One command on any GPU machine (no Docker)

`install.sh` does the whole install itself: one environment, the CUDA wheels, the
three services' packages, the ACE-Step weights, then it starts everything and
joins the tunnel.

```bash
./deploy/gpu/install.sh --check        # what this machine has, installs nothing
./deploy/gpu/install.sh                # install, then run the stack
./deploy/gpu/install.sh --no-weights   # packages only
```

The variables it reads: `TUNNEL_TOKEN` (+ `TUNNEL_HOSTNAME`) for a named tunnel,
`QUICK_TUNNEL=1` for a throwaway address, `SONG_ENDPOINT_KEY` so this machine
publishes its address to the site, and `SITE_URL` if you deployed your own copy.

## 1. Or in a container

```bash
cd deploy/gpu
cp .env.example .env        # fill in TUNNEL_TOKEN (step 2); QUICK_TUNNEL=1 works meanwhile
docker compose up -d --build
docker compose logs -f
```

A cold start downloads ~10 GB of ACE-Step weights into `song_service/models`
(mounted, so a rebuild does not repeat it) and takes a few minutes; the health
check answers on 8090 after that.

## 2. The stable address (named tunnel)

A quick tunnel changes name on every restart. A named tunnel keeps one hostname,
which is what "Cloudflare recognises it" means in practice:

```bash
cloudflared tunnel login                       # opens a browser, once
cloudflared tunnel create song-ai              # writes a credentials file
cloudflared tunnel route dns song-ai ai.example.com
cloudflared tunnel token song-ai               # paste that into .env as TUNNEL_TOKEN
```

Then `docker compose up -d` again. The container joins the tunnel and
`https://ai.example.com/health` should answer with the ACE-Step JSON.

`.env` for the container:

```
TUNNEL_TOKEN=<token from cloudflared tunnel token song-ai>
SONG_WARM=1
```

## 3. Point the Worker at it

The Worker already proxies the model paths (`worker.js`, `SONG_ROUTES`):
`/api/lyrics`, `/api/song`, `/api/song/<token>`, `/api/song/voice`,
`/song_file/<token>` and `/last_song`. Give it the address once:

```bash
npx wrangler deploy --var SONG_API:https://ai.example.com
```

or set `SONG_API` in the dashboard (Settings → Variables). After that the page
calls its own origin for everything — no CORS, no mixed content, no address in the
browser — and Cloudflare is the only front door to the GPU.

## How a browser reaches it

1. This machine starts and publishes its address: `POST /api/song/endpoint` with
   the key, stored in KV as `song_api`.
2. The page reads `GET /api/song/endpoint` and gets that address.
3. `worker.js` proxies `/api/song`, `/api/song/<token>`, `/api/song/voice`,
   `/api/lyrics`, `/song_file/<token>` and `/last_song` to it, streaming the
   response, so the browser only ever talks to Cloudflare and a 34 MB WAV is
   streamed rather than buffered.

Verified against the live site: `GET /api/song/latest` answered 200 with the job
JSON, and `GET /last_song` answered `200 audio/wav, Content-Length: 34542420`
through Cloudflare, with the address served by `/api/song/endpoint`.

## What Cloudflare does and does not do

It serves the page, stores the address, and proxies every model call. It does not
install anything on the GPU machine and cannot run these models itself, so the
one install command above is what turns a GPU box into the AI the site uses.

## What is deliberately missing

`voice_service` (chatterbox) is not in this image: its pins (numpy 1.26, torch
2.6.0) cannot share an environment with the song service (numpy 2.4.6, torch
2.14.1), and the singing path never calls it.
