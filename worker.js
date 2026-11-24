import { videoKey } from './public/lib/video-key.js';

/**
 * Cloudflare Worker for the video discovery site.
 *
 * This deployment is deliberately AI-free on Cloudflare's side: no Workers AI
 * binding, no hosted model calls. The AI in this project is the local one —
 * song_service/ runs the lyric and music models on your own GPU, and the page
 * talks to it directly (see public/config.js).
 *
 * What is left here is only what a static host cannot do for itself:
 *
 *   /api/download,        proxied to the hosted downloader in DOWNLOAD_API.
 *   /download_file/...    Static hosting cannot run yt-dlp, so downloads are
 *                         handled by that separate service (downloader_service/
 *                         in this repo). Proxying them through the Worker keeps
 *                         the page same-origin: no CORS, no config in the page.
 */

// Paths that belong to the download service rather than to assets.
const DOWNLOAD_ROUTES = [
  /^\/api\/download(?:\/[a-z0-9]+)?$/i,
  /^\/download_file\/[a-z0-9]+$/i,
];

// Speech-to-text runs here, on Cloudflare's Whisper, so voice search works from
// a browser whether or not anything is running on the visitor's machine. The
// clip never leaves Cloudflare's edge, and the free neuron allowance covers far
// more than a person can speak in a day.
const TRANSCRIBE_ROUTE = /^\/api\/transcribe$/i;

// Open this in a browser to see whether the download service is configured,
// awake and holding a cookie jar, instead of guessing from a failed download.
const HEALTH_ROUTE = /^\/api\/health$/i;

// The video index, answered from D1 instead of from the 3 MB videos.json the page
// used to download in full before it could show anything.
const SEARCH_ROUTE = /^\/api\/search$/i;

// One click on one video. This is how the library changes without a re-import:
// the count the cards are ordered by goes up, and the pair of query and video is
// kept in learned_preferences.
const CLICK_ROUTE = /^\/api\/click$/i;

// Videos found by the machine's own yt-dlp, handed up to be stored. YouTube is
// the reason this exists: a Worker's address is refused by YouTube, so the
// search that reaches it happens on the user's PC and only the result travels.
const INGEST_ROUTE = /^\/api\/ingest$/i;

// A short code that ties one person's saved search to several devices. There are
// no accounts on this site, and browser storage never leaves its own browser, so
// the code is the one thing that can travel: type it once on another device and
// the same saved search follows.
const SYNC_ROUTE = /^\/api\/sync$/i;

// The music model cannot run here: it needs a GPU that a Worker does not have.
// These paths exist so the page gets a straight answer instead of a 405 from
// the static asset handler, and so an address can be supplied with SONG_API
// when the model is reachable from the internet.
// A Quick Tunnel gets a new name every time it restarts, so the PC publishes the
// current address here and the page asks its own origin for it. Without this,
// every device away from home needed the new address pasted in by hand. Reading
// is public because the address itself is public; writing needs the key the
// launcher holds in build\song_endpoint.key.
const ENDPOINT_ROUTE = /^\/api\/song\/endpoint$/i;

// The pool, as the site and its owner can see it: which machines have joined and
// when each last published. Answers from KV, never proxied to a GPU.
const POOL_ROUTE = /^\/api\/song\/pool$/i;

const SONG_ROUTES = [
  /^\/api\/lyrics$/i,
  /^\/api\/song$/i,
  /^\/api\/song\/[a-z0-9]+$/i,
  /^\/song_file\/[a-z0-9]+$/i,
  /^\/last_song$/i,
];
const WHISPER_MODEL = '@cf/openai/whisper-large-v3-turbo';
const MAX_CLIP_BYTES = 8 * 1024 * 1024;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (DOWNLOAD_ROUTES.some((route) => route.test(url.pathname))) {
      return download(request, env, url);
    }

    if (TRANSCRIBE_ROUTE.test(url.pathname)) {
      return transcribe(request, env);
    }

    if (HEALTH_ROUTE.test(url.pathname)) {
      return downloaderHealth(env);
    }

    if (SEARCH_ROUTE.test(url.pathname)) {
      try {
        return await cachedSearch(env, request, url);
      } catch (err) {
        // An uncaught throw here becomes a blank Cloudflare 500 with no clue in
        // it. The reason is worth more than the tidiness - and it goes to the log
        // as well, because a 500 that only travels in the response cannot be
        // diagnosed after the fact (one was seen and not reproducible).
        console.error('[search] unhandled:', (err && (err.stack || err.message)) || err);
        return json({ error: String((err && err.message) || err) }, 500);
      }
    }

    if (CLICK_ROUTE.test(url.pathname)) {
      return recordClick(env, request);
    }

    if (INGEST_ROUTE.test(url.pathname)) {
      return ingestVideos(env, request);
    }

    if (SYNC_ROUTE.test(url.pathname)) {
      const answer = await syncSession(env, request, url);
      // The copy of the page served by the desktop app on the PC is a different
      // origin from this site, and without these headers its reads fail as
      // "Failed to fetch" - which is what Sync looked like: broken. The record is
      // addressed by a code and carries nothing private, so any origin may ask.
      const cors = new Response(answer.body, answer);
      cors.headers.set('Access-Control-Allow-Origin', '*');
      cors.headers.set('Access-Control-Allow-Headers', 'Content-Type');
      cors.headers.set('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
      return cors;
    }

    if (url.pathname === '/api/song/ticket' && (request.method === 'GET' || request.method === 'POST')) {
      // Public on purpose: an install has no way to sign in to Cloudflare, and the ticket
      // is what it holds instead of the key. It expires in ten minutes and authorises
      // only the address announcement below.
      const minted = await mintTicket(String(env.SONG_ENDPOINT_KEY || '').trim());
      if (!minted) return json({ error: 'This Worker has no SONG_ENDPOINT_KEY, so no ticket can be minted.' }, 503);
      return json({ ok: true, ticket: minted.ticket, expires: minted.expires });
    }

    if (ENDPOINT_ROUTE.test(url.pathname)) {
      return songEndpoint(request, env);
    }

    if (POOL_ROUTE.test(url.pathname)) {
      const pool = await readPool(env);
      return json({
        count: pool.length,
        // Passed through from the pool Worker: a register that could not be read
        // is not an empty pool, and the page needs to be able to tell the two
        // apart instead of claiming no GPU has joined.
        degraded: Boolean(lastPoolNote),
        error: lastPoolNote || undefined,
        machines: pool.map((entry) => ({ url: entry.url, seen: entry.seen })),
      });
    }

    if (url.pathname === '/api/song/probe') {
      return songProbe(url);
    }

    if (SONG_ROUTES.some((route) => route.test(url.pathname))) {
      return song(request, env, url);
    }

    if (url.pathname === '/api/gpu/start' || url.pathname === '/api/gpu/status') {
      return startGpu(request, env, url);
    }

    const asset = await env.ASSETS.fetch(request);
    // The page is what reports whether the model is green, so a stored copy of it
    // keeps reporting the previous behaviour - which is how a page that had just
    // been fixed went on saying "ready" in grey for hours. HTML is never stored:
    // not by the browser, not by the edge.
    if ((asset.headers.get('content-type') || '').includes('text/html')) {
      const copy = new Response(asset.body, {
        status: asset.status,
        statusText: asset.statusText,
        headers: asset.headers,
      });
      copy.headers.set('Cache-Control', 'no-store, no-cache, must-revalidate');
      copy.headers.set('CDN-Cache-Control', 'no-store');
      copy.headers.set('Pragma', 'no-cache');
      return copy;
    }
    return asset;
  },
};

// --- starting a GPU on its own ---------------------------------------------
// Kaggle's API can start a notebook run on a free GPU; Colab's cannot be started
// by anything except a person opening it. The kernel pushed here runs this
// project's own boot - clone the repo, run the song service, open a tunnel, join
// the pool - so what appears in the pool is this machine's model, not a stranger's.
const GPU_SLUG = 'song-gpu-pool';
const GPU_COOLDOWN_MS = 30 * 60 * 1000;

function kaggleAuth(env) {
  const user = String(env.KAGGLE_USERNAME || '').trim();
  const key = String(env.KAGGLE_KEY || '').trim();
  if (!key) return null;
  // Kaggle now issues access tokens that start with KGAT, and those are bearer
  // tokens; the older username+key pair is basic auth. Sending the new kind as
  // basic was answered with 401 Unauthorized, which read like a wrong key.
  const header = /^KGAT/i.test(key)
    ? 'Bearer ' + key
    : (user ? 'Basic ' + btoa(user + ':' + key) : null);
  if (!header) return null;
  // The username field in kaggle.json can hold a display name (spaces, capitals);
  // the API wants the account handle (lowercase, no separators). Kaggle silently
  // sanitises whatever is in a slug, which turned the returned address into a
  // different one. Nothing here is written down: it all comes from the secret.
  const handle = user.toLowerCase().replace(/[^a-z0-9_-]/g, '');
  return { user, key, handle, header };
}

// The kernel is a plain script: a few lines that clone the repo and run the same
// cell the Colab notebook runs, with the join key written in so the card adds
// itself to the pool without anybody pasting an address.
function gpuKernelScript(joinKey, codeDataset, weightsDataset) {
  // The pushed code is a dozen lines: it can never reach the network, so it runs the
  // boot script from the mounted dataset instead of carrying one. Everything the
  // run needs - the repository, the Linux packages, the ACE-Step source, the
  // cloudflared binary and the weights - arrives as dataset sources, which are
  // mounted at /kaggle/input without any connection.
  return [
    'import os, runpy, sys',
    '',
    "CODE = '/kaggle/input/" + codeDataset.split('/')[1] + "/repo'",
    'script = os.path.join(CODE, \'public\', \'song_gpu_offline.py\')',
    'os.environ[\'SONG_JOIN_KEY\'] = ' + JSON.stringify(joinKey || ''),
    '# cpu_offload streams a 7 GB transformer through the CPU one layer at a time, which\n'
    + '# is what turned a warm on a 15 GB T4 into ten minutes of "warming". Only a card\n'
    + '# that cannot hold the model needs it, so it is switched off when the card can.\n'
    + 'try:\n'
    + '    import torch as _torch\n'
    + '    if _torch.cuda.is_available() and _torch.cuda.get_device_properties(0).total_memory >= 12 * 1024 ** 3:\n'
    + "        os.environ['ACE_CPU_OFFLOAD'] = '0'\n"
    + "        print('cpu_offload: off (the card holds the model)', flush=True)\n"
    + 'except Exception as _exc:\n'
    + "    print('cpu_offload: left at its default (' + str(_exc) + ')', flush=True)\n"
    + "print('offline boot:', script, flush=True)",
    'if not os.path.isfile(script):',
    "    print('the code dataset is not mounted at', CODE, flush=True)",
    "    print('attach " + codeDataset + " and " + weightsDataset + " to this kernel', flush=True)",
    '    sys.exit(2)',
    'sys.path.insert(0, CODE)',
    "runpy.run_path(script, run_name='__main__')",
    '',
  ].join('\n');
}

function gpuKernelScriptOnline(joinKey) {
  return [
    'import os, re, socket, subprocess, sys',
    '',
    'JOIN_KEY = ' + JSON.stringify(joinKey || ''),
    "REPO = 'https://github.com/soluzka/AI-driven_NVIDIA_song_designer.git'",
    '',
    '# Kaggle turns off the internet (and the GPU) on an account that has not had a',
    '# phone number verified, and a push that asks for it is simply run without it.',
    '# The first run died at "Could not resolve host: github.com" and said nothing',
    '# about why, so this says it plainly before anything else.',
    'def internet():',
    '    try:',
    "        socket.create_connection(('github.com', 443), timeout=10).close()",
    '        return True',
    '    except Exception as exc:',
    "        print('NO INTERNET in this kernel:', exc, flush=True)",
    "        print('Kaggle disables internet and GPU until the account has a verified phone number:', flush=True)",
    "        print('  kaggle.com/settings -> Phone Verification. Then start the GPU again from the site.', flush=True)",
    '        return False',
    '',
    'if not internet():',
    '    sys.exit(2)',
    '',
    'def sh(cmd, **kw):',
    "    print('+', cmd, flush=True)",
    '    subprocess.check_call(cmd, **kw)',
    '',
    "if not os.path.isdir('repo'):",
    "    sh(['git', 'clone', '--depth', '1', REPO, 'repo'])",
    "os.chdir('repo')",
    "cell = os.path.join('public', 'song_gpu_cell.py')",
    "source = open(cell, encoding='utf-8').read()",
    "source = re.sub(r'^JOIN_KEY\\s*=\\s*.*$', 'JOIN_KEY = ' + repr(JOIN_KEY), source, count=1, flags=re.M)",
    "boot = '/kaggle/working/boot.py' if os.path.isdir('/kaggle/working') else 'boot.py'",
    "open(boot, 'w', encoding='utf-8').write(source)",
    'print(\'boot: \' + boot + \' (join key \' + (\'set\' if JOIN_KEY else \'empty\') + \')\', flush=True)',
    "sh([sys.executable, boot])",
    '',
  ].join('\n');
}

async function kaggleStatus(env, auth) {
  // /kernels/status/<owner>/<slug> answers 404; the list carries what is actually
  // available - the kernel's ref and when it last ran - so that is what is read.
  try {
    const answer = await fetch('https://www.kaggle.com/api/v1/kernels/list?user='
      + encodeURIComponent(auth.handle), { headers: { authorization: auth.header } });
    if (!answer.ok) return null;
    const body = await answer.json();
    const mine = Array.isArray(body) ? body.find((k) => String(k.ref || '').endsWith('/' + GPU_SLUG)) : null;
    // Deliberately no ref: it is "<handle>/song-gpu-pool", and this route is public.
    // The account name is not something this site hands out.
    return mine ? { lastRunTime: mine.lastRunTime || null } : null;
  } catch (err) {
    return null;
  }
}

async function startGpu(request, env, url) {
  const auth = kaggleAuth(env);
  // These two timestamps used to live in this Worker's STATS KV, whose daily write
  // allowance runs out - and once it did, the cooldown and the boot-window clock were
  // gone for the rest of the day, which is what made the announce answer 500, Start
  // Kaggle answer error 1101, and the boot guard stick. They now go to the pool
  // Worker's store: a different namespace that kept answering the whole time. STATS
  // stays as the fallback for a deployment with no pool binding, and for a value
  // written before this change.
  const remember = async (key, value) => {
    try {
      const answer = await poolRequest(env, '/state/' + encodeURIComponent(key), {
        method: 'POST',
        headers: {
          'content-type': 'application/json',
          'x-join-key': poolJoinKey(env),
        },
        body: JSON.stringify({ value: String(value) }),
      });
      if (answer.ok) return true;
    } catch (err) {}
    try {
      await env.STATS.put(key, value);
      return true;
    } catch (err) {
      return false;
    }
  };
  const recall = async (key) => {
    try {
      const answer = await poolRequest(env, '/state/' + encodeURIComponent(key));
      if (answer.ok) {
        const body = await answer.json();
        if (body && body.value) return String(body.value);
      }
    } catch (err) {}
    try {
      return await env.STATS.get(key);
    } catch (err) {
      return null;
    }
  };
  const state = async () => ({
    lastStart: Number(await recall('gpu:started_at') || 0),
    status: auth ? await kaggleStatus(env, auth) : null,
    configured: Boolean(auth),
  });

  if (request.method === 'OPTIONS') return new Response('', { status: 204 });
  if (url.pathname === '/api/gpu/status') return json(await state());
  if (request.method !== 'POST') return json({ error: 'POST only' }, 405);

  if (!auth || !auth.user) {
    return json({
      configured: false,
      error: 'No Kaggle account is set on this deployment yet, so this site cannot start a GPU. '
        + 'Run tools\\set_kaggle.ps1 with your Kaggle API token, then deploy again.',
    }, 501);
  }

  const last = Number(await recall('gpu:started_at') || 0);
  const waited = Date.now() - last;
  if (last && waited < GPU_COOLDOWN_MS) {
    return json({
      configured: true,
      started: false,
      retryInSeconds: Math.ceil((GPU_COOLDOWN_MS - waited) / 1000),
      // This clock marks a *request* Kaggle accepted, not a machine that is running:
      // Kaggle queues a pushed version and starts it when it frees a T4, which can be
      // minutes later or not at all. Saying "a GPU was started" made a queued run look
      // like a running one.
      error: 'A GPU run was requested ' + Math.round(waited / 60000) + ' minutes ago; Kaggle starts '
        + 'a queued version when it frees a card. Kaggle gives a limited number of GPU hours '
        + 'a week, so this waits ' + Math.round(GPU_COOLDOWN_MS / 60000)
        + ' minutes between requests.',
    }, 429);
  }

  // A push does not add a run beside the old one: Kaggle attaches the new version to the
  // session the kernel already has, so pressing Start Kaggle during a boot is what
  // cancels that boot. The site cannot see a run before it joins, but Kaggle's kernel
  // carries its start time, so "started recently with an empty pool" is a boot in
  // progress - and the pool is asked first, because a machine that is serving is not.
  const BOOT_WINDOW_MS = 90 * 60 * 1000;
  const living = await readPool(env);
  // The guard can only speak if its join time can be stored. With KV refusing writes
  // (its daily allowance gone) every run looks like a boot that never joined, and a
  // press was refused for the whole ninety minutes on nothing: a run that died at
  // minute two was treated as a boot in progress. So the window is enforced only when
  // a write actually lands; otherwise the push is allowed and the answer says the
  // guard was off.
  let guard = 'enforced';
  if (living.length) {
    // A machine is serving: this run got as far as the pool, which is the difference
    // between a boot still running and a run that ended. Recorded so that a press after
    // it dies is allowed instead of waiting out the whole window.
    if (!(await remember('gpu:joined_at', String(Date.now())))) guard = 'off (KV refused a write)';
  } else {
    const ran = await kaggleStatus(env, auth);
    const began = Date.parse((ran && ran.lastRunTime) || '') || 0;
    const since = Math.max(last, Number.isFinite(began) ? began : 0);
    const joined = Number(await recall('gpu:joined_at') || 0);
    const storable = await remember('gpu:guard_at', String(Date.now()));
    if (!storable) guard = 'off (KV refused a write)';
    // Joined and gone: the run ended, so the start is allowed even inside the window.
    if (storable && since && Date.now() - since < BOOT_WINDOW_MS && !(joined >= since)) {
      const booted = Math.round((Date.now() - since) / 60000);
      return json({
        configured: true,
        started: false,
        booting: true,
        retryInSeconds: Math.ceil((BOOT_WINDOW_MS - (Date.now() - since)) / 1000),
        guard,
        error: 'The GPU run started ' + booted + ' minutes ago and has not joined the pool yet, so it '
          + 'is still booting. Pushing a new version would replace that session and cancel it, so '
          + 'this refuses until ' + Math.round(BOOT_WINDOW_MS / 60000) + ' minutes have passed since '
          + 'the run began - start one then if it never joined.',
      }, 429);
    }
  }

  const body = {
    slug: auth.handle + '/' + GPU_SLUG,
    newTitle: 'song gpu pool',
    text: gpuKernelScript(
      // The key the pool endpoints verify; a kernel given a different value boots fine
      // and then gets 403 Forbidden from every join.
      poolJoinKey(env),
      String(env.KAGGLE_CODE_DATASET || (auth.handle + '/song-gpu-code')).trim(),
      String(env.KAGGLE_WEIGHTS_DATASET || (auth.handle + '/song-gpu-weights')).trim(),
    ),
    language: 'python',
    kernelType: 'script',
    isPrivate: true,
    enableGpu: true,
    machineShape: 'NvidiaTeslaT4',
    enableInternet: true,
    // The datasets are what make an offline run possible at all.
    datasetDataSources: [
      String(env.KAGGLE_CODE_DATASET || (auth.handle + '/song-gpu-code')).trim(),
      String(env.KAGGLE_WEIGHTS_DATASET || (auth.handle + '/song-gpu-weights')).trim(),
    ],
    competitionDataSources: [],
    // Left empty on purpose. Pointing this at the kernel's own slug looked like the way
    // to mount the previous Output, but Kaggle answers invalidKernelSources for a
    // kernel that sources itself and silently drops it - kernels/pull then shows
    // kernelDataSources: [] - so the weights cannot come from here. They come from the
    // song-gpu-weights dataset that has to exist beside song-gpu-code; without it every
    // start downloads ~10 GB from Hugging Face first.
    kernelDataSources: [],
    categoryIds: [],
  };

  let pushed = null;
  let said = '';
  try {
    const answer = await fetch('https://www.kaggle.com/api/v1/kernels/push', {
      method: 'POST',
      headers: { 'content-type': 'application/json', authorization: auth.header },
      body: JSON.stringify(body),
    });
    said = (await answer.text()).slice(0, 400);
    pushed = answer.ok;
    if (!answer.ok) {
      return json({ configured: true, started: false, error: 'Kaggle refused the run: ' + said }, 502);
    }
  } catch (err) {
    return json({ configured: true, started: false, error: 'Could not reach Kaggle: ' + (err && err.message) }, 502);
  }

  await remember('gpu:started_at', String(Date.now()));
  return json({
    configured: true,
    started: Boolean(pushed),
    guard,
    note: 'The GPU boots, runs the song service, opens a tunnel and joins the pool. It shows up here '
      + 'when it reports the model loaded, which takes a few minutes on a fresh machine.',
    said,
  });
}

// --- the GPU pool ------------------------------------------------------------
// Every machine that installs the agent publishes its own tunnel address here.
// The KV entry is a list, not one string: with several GPUs online a model call
// goes to a machine chosen at random and the next one is tried when it does not
// answer, so the site keeps working whichever PC is awake.
const POOL_KEY = 'song_pool';
const POOL_LIMIT = 8;                                  // machines kept
const POOL_TTL_MS = 7 * 24 * 60 * 60 * 1000;           // forgotten after a week

// Which list the last /pool answer came from, reported by /api/song/pool: with two
// stores and one page, guessing which one was read wasted an afternoon.
let lastPoolSource = 'none';
// Why the pool list may be short: the register answered but could not be read, or
// the pool base did not answer at all. /api/song/pool reports it.
let lastPoolNote = '';

// The one way to reach the pool's Worker. Cloudflare refuses a Worker's fetch to
// another Worker on the same zone (error 1042), and both live on
// *.bpiersing.workers.dev, so the service binding is what works; the address stays as
// the fallback for a deployment that has no binding.
async function poolRequest(env, path, init) {
  const options = Object.assign({ headers: { accept: 'application/json' } }, init || {});
  if (env.POOL && typeof env.POOL.fetch === 'function') {
    return env.POOL.fetch('https://pool' + path, options);
  }
  const base = String(env.POOL_API || '').trim().replace(/\/+$/, '').replace(/\/pool$/, '');
  if (!base) throw new Error('no pool binding and no pool address');
  return fetch(base + path, options);
}

async function readPool(env) {
  // A separate pool base wins when it is configured. That Worker holds only the
  // list, so it survives a redeploy of the site and can be run by whoever owns the
  // cards. An unreachable pool base falls through to this Worker's own KV, so one
  // broken address cannot take the site's models offline.
  // A base that already names the route (…/pool) is used as written: appending
  // another /pool asked the pool Worker for /pool/pool and got a 404, which read as
  // "the pool is unreachable" and quietly hid every machine it held.
  const remote = String(env.POOL_API || '').trim().replace(/\/+$/, '').replace(/\/pool$/, '');
  let baseList = null;   // null: the pool base did not answer at all
  let remoteNote = '';   // why it did not answer, when it did not
  let baseNote = '';     // the pool base answered, but could not read its register
  lastPoolNote = '';
  if (remote || (env.POOL && typeof env.POOL.fetch === 'function')) {
    try {
      const answer = await poolRequest(env, '/pool');
      if (!answer.ok) remoteNote = 'status ' + answer.status + ' at ' + answer.url
        + ' body ' + (await answer.text()).slice(0, 160);
      if (answer.ok) {
        const info = await answer.json();
        // The pool Worker says so itself when its register could not be read.
        if (info.degraded) baseNote = String(info.error || 'the pool register could not be read');
        baseList = (info.machines || [])
          .map((machine) => ({
            url: String(machine.url || '').replace(/\/+$/, ''),
            seen: machine.seen || Date.now(),
            joined: machine.joined || machine.seen || Date.now(),
          }))
          .filter((machine) => machine.url);
      }
    } catch (err) {
      remoteNote = 'error: ' + ((err && err.message) || err);
    }
  }
  // An empty answer is the pool base saying "nobody is online", and that is the
  // truth: falling through to this Worker's own KV here resurrected a tunnel the
  // pool had just pruned, and the site kept routing to it.
  // An empty answer means nobody is online - unless the register could not be read,
  // in which case the KV copy below may still hold a card that is serving.
  if (baseList && !baseList.length && !baseNote) { lastPoolSource = 'pool-empty'; return []; }
  if (baseNote) lastPoolNote = baseNote;
  // A pool base that does not answer at all is the same problem from the page's
  // point of view: the list is short for a reason worth showing.
  else if (remoteNote) lastPoolNote = remoteNote;
  if (!env.STATS) {
    lastPoolSource = baseList ? 'pool' : 'none';
    return baseList ? livePool(baseList) : [];
  }
  let entries = [];
  try {
    const raw = await env.STATS.get(POOL_KEY);
    entries = raw ? JSON.parse(raw) : [];
  } catch (err) {
    entries = [];
  }
  const cutoff = Date.now() - POOL_TTL_MS;
  const pool = (Array.isArray(entries) ? entries : [])
    .filter((entry) => entry && typeof entry.url === 'string' && (entry.seen || 0) > cutoff);
  // The single address older pages still read.
  const legacy = await env.STATS.get('song_api');
  if (legacy && !pool.some((entry) => entry.url === legacy)) {
    pool.push({ url: legacy, seen: Date.now(), legacy: true });
  }
  // Two lists hold machines: the pool base, which every boot and notebook joins,
  // and this Worker's own KV, which older pages and the PC's service publish to.
  // Reading only one of them hid a card that had joined the other - a Colab T4 in
  // the pool base never reached the page while the Kaggle machine sat in KV. Both
  // are now offered when both answer; the pool base still wins when it says empty.
  const merged = baseList
    ? baseList.concat(pool.filter((entry) => !baseList.some((known) => known.url === entry.url)))
    : pool;
  // The card that joined last is the one this site connects to: starting a new GPU is a
  // request to use it, not to share the work with the one left running overnight. Only a
  // real join time counts - an address that merely re-published itself to this Worker's KV
  // (every couple of minutes, by design) used to outrank the pool and put a dead tunnel
  // first, which is what made both cards look offline.
  merged.sort((a, b) => Number(b.joined || 0) - Number(a.joined || 0));
  lastPoolSource = baseList ? 'pool+kv' : (remote ? 'kv-fallback ' + remote + ' ' + remoteNote : 'kv');
  return livePool(merged);
}

// --- which of those addresses answers ----------------------------------------
// A Quick Tunnel name dies with the session that made it, but its entry stays in
// the list for seven days. The header counted those dead machines as GPUs, and a
// render picked one of them at random, so every so often a song failed and had to
// be retried. Each list is probed before it is handed out, and the answer is kept
// for POOL_PROBE_MS so a page that asks twice does not probe twice.
const POOL_PROBE_MS = 15000;
let poolProbeCache = { at: 0, key: '', live: [] };

// One probe, and what it means: alive, gone (nothing answered), or broken (it answered
// and said its warm failed - a definite no rather than a blip).
async function poolVerdict(address, timeoutMs = 6000) {
  try {
    const reply = await fetch(String(address).replace(/\/+$/, '') + '/health', {
      headers: { accept: 'application/json' },
      signal: AbortSignal.timeout(timeoutMs),
    });
    if (!reply.ok) return 'gone';
    let body = null;
    try { body = await reply.json(); } catch (err) {}
    // Reachable but unable to render: a machine whose warm ended in warm_failed
    // answers /health forever and never loads, so it must not be handed a song.
    return (body && body.warm_failed) ? 'broken' : 'alive';
  } catch (err) {
    return 'gone';
  }
}

// A card that misses one /health probe is not gone: a tunnel hiccups for a second and
// the page would jump to another machine, or back to this PC, on that alone. So one
// quick retry, and then a second consecutive bad poll, before it leaves the list. A
// machine that says its warm failed leaves at once, because that is an answer.
const poolStrikes = new Map();
const POOL_STRIKE_MS = 90 * 1000;
// A card that has already missed twice is checked once, briefly, for the next minute.
// Three four-second waits on a dead tunnel are what made /api/song/pool take nine seconds
// and the page conclude that the whole pool was offline.
const poolCooled = new Map();
const POOL_COOL_MS = 60 * 1000;
async function poolAlive(address) {
  // The gap is not decoration: after a genuine timeout the tunnel is still closing that
  // stream, and a retry 700 ms later can fail on the same dead connection.
  const now0 = Date.now();
  const lastOk = Number(poolLastOk.get(address) || 0);
  const known = now0 - lastOk < POOL_ALIVE_MS;
  const cooling = Number(poolCooled.get(address) || 0) > now0;
  // A card nobody has watched answer in this isolate gets one short look, not three long
  // ones. Three four-second waits on a rebooted tunnel's old address is what pushed
  // /api/song/pool past the page's eight-second budget and printed "Pool - offline"
  // while a perfectly live T4 was sitting in the list.
  for (const wait of ((known && !cooling) ? [0, 1500, 3000] : [0])) {
    if (wait) await new Promise((resolve) => setTimeout(resolve, wait));
    const verdict = await poolVerdict(address, known ? 4000 : 2500);
    if (verdict === 'alive') { poolStrikes.delete(address); poolCooled.delete(address); return true; }
    if (verdict === 'broken') {
      poolStrikes.delete(address);
      poolCooled.set(address, Date.now() + POOL_COOL_MS);
      return false;
    }
  }
  const now = Date.now();
  const previous = poolStrikes.get(address);
  const strikes = previous && now - previous.at < POOL_STRIKE_MS ? previous.strikes + 1 : 1;
  poolStrikes.set(address, { strikes, at: now });
  if (strikes >= 2) poolCooled.set(address, now + POOL_COOL_MS);
  // One miss is forgiven only for a card this isolate has watched answer in the last
  // couple of minutes. Without that, every fresh isolate handed a dead tunnel a free
  // pass, so an address that closed hours ago kept coming back at the top of the list.
  return strikes < 2 && known;
}

async function poolAnswers(address) {
  return (await poolVerdict(address)) === 'alive';
}

// When each machine was last seen alive in this isolate, so one bad probe is forgiven
// only for a card that was working a minute ago.
const poolLastOk = new Map();
// Two minutes: long enough that a card which answered a moment ago is not dropped for one
// missed probe, short enough that an address whose tunnel is gone stops being counted.
// A ten-minute grace was tried and it made the header claim three GPUs when only one was
// answering - the count has to be what answers now, not what answered a while ago.
const POOL_ALIVE_MS = 2 * 60 * 1000;

async function livePool(list) {
  if (!Array.isArray(list) || !list.length) return [];
  const key = list.map((entry) => entry.url).join(' ');
  if (poolProbeCache.key === key && Date.now() - poolProbeCache.at < POOL_PROBE_MS) {
    return poolProbeCache.live;
  }
  const verdicts = await Promise.all(
    list.map(async (entry) => ({ entry, alive: await poolAlive(entry.url) })));
  // The grace belongs to a card that was serving a moment ago. A dead address nobody saw
  // answer - a leftover test entry, a tunnel that closed hours ago - used to be kept for
  // one poll in every fresh isolate, which put it back at the top of the list each time.
  const live = verdicts.filter((item) => {
    if (item.alive) { poolLastOk.set(item.entry.url, Date.now()); return true; }
    // Only a card this worker has watched answer gets the grace. Falling back to when it
    // joined kept an address that had already stopped answering in the count for two
    // minutes, which is how a shut-down PC was still counted as a GPU.
    const last = poolLastOk.get(item.entry.url) || 0;
    return Date.now() - last < POOL_ALIVE_MS;
  }).map((item) => item.entry);
  poolProbeCache = { at: Date.now(), key, live };
  return live;
}

async function writePool(env, pool) {
  // KV has a daily write allowance, and this namespace has hit it. A failed put used
  // to throw straight out of the POST that announces a machine, so a boot that had
  // already joined the pool Worker logged "could not join browser.bpiersing.workers.dev
  // HTTP Error 500" even though nothing was wrong with it. The pool base is the list
  // that matters; this says whether this Worker's own copy was saved, and never
  // fails the request over it.
  try {
    await env.STATS.put(POOL_KEY, JSON.stringify(pool.slice(0, POOL_LIMIT)));
    if (pool.length) await env.STATS.put('song_api', pool[0].url);
    return true;
  } catch (err) {
    return false;
  }
}

// --- is that address actually alive? ----------------------------------------
// "Connect my model" holds an address the browser typed, and the browser cannot
// say why it refused it: a CORS block, a dead Colab session and a typo are all
// "Failed to fetch" there. This Worker can see the status or the network error,
// so the page asks it instead of guessing. Only the hosts a song service can
// live on are probed, so this is not an open proxy.
// No loopback here, on purpose: this code runs at Cloudflare's edge, where
// 127.0.0.1 is Cloudflare's own machine and answers 403. A local song service can
// only be probed by the browser that shares its machine.
const PROBE_HOSTS = ['.trycloudflare.com', '.workers.dev'];

function probeAllowed(value) {
  try {
    const parsed = new URL(value);
    if (parsed.protocol !== 'https:' && parsed.protocol !== 'http:') return false;
    const host = parsed.hostname.toLowerCase();
    return PROBE_HOSTS.some((allowed) => host === allowed || host.endsWith(allowed));
  } catch (err) {
    return false;
  }
}

async function songProbe(url) {
  const target = String(url.searchParams.get('url') || '').trim().replace(/\/+$/, '');
  if (!probeAllowed(target)) {
    const loopback = /^https?:\/\/(127\.|localhost|\[::1\])/i.test(target);
    return json({
      ok: false,
      error: loopback
        ? 'A local address is checked by the browser itself, not from the edge.'
        : 'This site only probes tunnel and Worker addresses.',
    }, 400);
  }
  const started = Date.now();
  try {
    const answer = await fetch(target + '/health', {
      headers: { accept: 'application/json' },
      signal: AbortSignal.timeout(10000),
    });
    const text = await answer.text();
    let body = null;
    try { body = JSON.parse(text); } catch (err) {}
    return json({
      ok: answer.ok,
      status: answer.status,
      ms: Date.now() - started,
      device: body && body.device,
      gpu: body && body.gpu,
      loaded: body && body.loaded,
      // A tunnel can answer 200 with its own HTML error page; the first characters
      // say which, which a bare status hides.
      sample: body ? undefined : String(text || '').slice(0, 160),
    });
  } catch (err) {
    return json({ ok: false, error: String((err && err.message) || err), ms: Date.now() - started }, 502);
  }
}

// The GPU connect key: what the pool Worker checks as JOIN_KEY when a card says "route
// songs to me". It is its own secret on purpose. The song key (SONG_ENDPOINT_KEY) signs
// publishing, and the two were one value, so rotating the GPU key quietly rotated the
// song key with it - and a machine holding one while the Workers held the other could
// render songs all day while the pool never listed it, which is what "the pool is red
// and I am running it" was. POOL_JOIN_KEY wins when it is set; SONG_ENDPOINT_KEY stays
// the fallback so a deployment that never set it keeps working.
function poolJoinKey(env) {
  return String(env.POOL_JOIN_KEY || env.SONG_ENDPOINT_KEY || '').trim();
}

// --- tickets: publishing without a copy of the key on the machine -------------
// The key exists so a stranger cannot name an address here. A machine that has just been
// installed carries whatever key its build was made with, and setting the new one on the
// Workers needed wrangler and a Cloudflare login on a desktop that has neither - so the
// join and the publish were refused until somebody pushed it by hand. This Worker mints
// short-lived tickets from the key it already holds instead: anyone may ask for one, it
// works for ten minutes, and it authorises exactly the announcements the key does. The
// key still works and is still what the tenant uses for anything else.
const TICKET_TTL_MS = 10 * 60 * 1000;
const TICKET_DOMAIN = 'song-endpoint:';
const ticketEncoder = new TextEncoder();

function b64url(bytes) {
  const arr = new Uint8Array(bytes);
  let text = '';
  for (let i = 0; i < arr.length; i += 1) text += String.fromCharCode(arr[i]);
  return btoa(text).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function unb64url(text) {
  try {
    const padded = String(text).replace(/-/g, '+').replace(/_/g, '/');
    const raw = atob(padded + '='.repeat((4 - (padded.length % 4)) % 4));
    const out = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i += 1) out[i] = raw.charCodeAt(i);
    return out;
  } catch (err) {
    return null;
  }
}

async function ticketSigner(secret) {
  return crypto.subtle.importKey('raw', ticketEncoder.encode(secret),
    { name: 'HMAC', hash: 'SHA-256' }, false, ['sign', 'verify']);
}

async function mintTicket(secret, ttlMs = TICKET_TTL_MS) {
  if (!secret) return null;
  const expires = Date.now() + ttlMs;
  const signature = await crypto.subtle.sign('HMAC', await ticketSigner(secret),
    ticketEncoder.encode(TICKET_DOMAIN + expires));
  return { ticket: expires + '.' + b64url(signature), expires };
}

async function ticketValid(secret, ticket) {
  if (!secret || !ticket) return false;
  const parts = String(ticket).split('.');
  if (parts.length !== 2) return false;
  const expires = Number(parts[0]);
  if (!Number.isFinite(expires) || expires < Date.now()) return false;
  const signature = unb64url(parts[1]);
  if (!signature) return false;
  try {
    return await crypto.subtle.verify('HMAC', await ticketSigner(secret), signature,
      ticketEncoder.encode(TICKET_DOMAIN + expires));
  } catch (err) {
    return false;
  }
}

async function songEndpoint(request, env) {
  if (request.method === 'POST') {
    const expected = env.SONG_ENDPOINT_KEY || '';
    const given = request.headers.get('x-song-key') || '';
    if (!expected) {
      return json({ error: 'Publishing is disabled: this Worker has no SONG_ENDPOINT_KEY.' }, 503);
    }
    // A ticket from GET /api/song/ticket is accepted in place of the key itself, so a
    // machine that was installed with an older key can still name its address here.
    if (given !== expected && !(await ticketValid(expected, request.headers.get('x-song-ticket')))) {
      return json({ error: 'Wrong key.' }, 403);
    }
    if (!env.STATS) return json({ error: 'No KV namespace is attached.' }, 501);

    let body = {};
    try {
      body = await request.json();
    } catch (err) {
      return json({ error: 'Expected JSON: {"url": "https://..."}' }, 400);
    }
    const value = String((body && body.url) || '').trim().replace(/\/+$/, '');
    if (value && !/^https?:\/\/[^\s]+$/i.test(value)) {
      return json({ error: 'Not an http(s) address.' }, 400);
    }
    let pool = await readPool(env);
    // {remove: "https://..."} leaves the pool: a machine that was re-installed on
    // a new address, or one that should no longer receive songs.
    const remove = String((body && body.remove) || '').trim().replace(/\/+$/, '');
    if (remove) {
      pool = pool.filter((entry) => entry.url !== remove);
      const saved = await writePool(env, pool);
      return json({ ok: true, removed: remove, saved, pool: pool.map((entry) => entry.url) });
    }
    if (!value) {
      return json({ ok: true, url: '', pool: pool.map((entry) => entry.url) });
    }
    const now = Date.now();
    const known = pool.find((entry) => entry.url === value);
    if (known) known.seen = now;
    else pool.unshift({ url: value, seen: now });
    // saved: false means this Worker's KV copy was not written (usually its daily
    // allowance). The forward below still goes out, so the machine is in the pool the
    // site actually reads, and the answer says what happened instead of throwing.
    const saved = await writePool(env, pool);
    // When the pool lives on its own base, the machine belongs there too: this
    // Worker forwards the announcement so both lists agree, and neither the site
    // nor the pool has to be redeployed for the other to keep working.
    try {
      await poolRequest(env, '/join', {
        method: 'POST',
        headers: { 'content-type': 'application/json', 'x-join-key': poolJoinKey(env) },
        body: JSON.stringify({ url: value }),
      });
    } catch (err) {}
    return json({ ok: true, url: value, saved, pool: pool.map((entry) => entry.url) });
  }

  const pool = await readPool(env);
  // The address a machine published for the header comes before the pool. "AI ready ·
  // this PC" is what Connect my model means; taking pool[0] instead named whichever card
  // had joined most recently - a Kaggle one - so a person running the PC was shown someone
  // else's GPU. It is kept where writes still land (this Worker's own KV runs out of them
  // daily, and then this GET answered "" for a machine that was up), and it is probed:
  // an address that stopped answering must not become the header's answer.
  let published = '';
  try {
    const answer = await poolRequest(env, '/state/' + encodeURIComponent('site:endpoint'));
    if (answer.ok) {
      const body = await answer.json();
      if (body && body.value) published = String(body.value).trim().replace(/\/+$/, '');
    }
  } catch (err) {}
  let url = '';
  if (published && /^https?:\/\//i.test(published) && await poolAnswers(published)) {
    url = published;
  }
  if (!url) url = pool.length ? pool[0].url : (env.SONG_API || '');
  const urls = pool.map((entry) => entry.url);
  if (url && !urls.includes(url)) urls.unshift(url);
  return json({
    url,
    urls,
    machines: pool.map((entry) => ({ url: entry.url, seen: entry.seen })),
    source: lastPoolSource,
  });
}


async function song(request, env, url) {
  // SONG_API wins when it is set; otherwise the address the machine published to
  // KV is the one to use. Reading only SONG_API made /api/song/endpoint report a
  // live address that the proxy then ignored, so every model call through
  // Cloudflare answered 501 while the page quietly fell back to loopback.
  // SONG_API pins one machine; otherwise the machines are candidates in the order
  // readPool returns them, newest join first, and one that does not answer is skipped
  // for the next. It used to start at a random entry to spread the load, which made
  // "start a new GPU and use it" impossible to predict.
  let candidates = [];
  const pinned = String(env.SONG_API || '').trim().replace(/\/+$/, '');
  if (pinned) {
    candidates = [pinned];
  } else {
    const pool = await readPool(env);
    candidates = pool.map((entry) => entry.url);
  }
  if (!candidates.length) {
    return json({
      error: 'No GPU has joined yet. Run install.cmd on a PC with an NVIDIA card: it '
        + 'installs, starts and publishes itself, and the site picks it up on the next '
        + 'click.',
      model: null,
    }, 501);
  }

  const init = { method: request.method, headers: { accept: request.headers.get('accept') || '*/*' } };
  const contentType = request.headers.get('content-type');
  if (contentType) init.headers['content-type'] = contentType;
  if (request.method !== 'GET' && request.method !== 'HEAD') {
    init.body = await request.arrayBuffer();
  }

  const failures = [];
  for (let index = 0; index < candidates.length; index++) {
    const base = candidates[index];
    try {
      const upstream = await fetch(base + url.pathname + url.search, init);
      // A machine that is up but cannot serve the request is not the same as one
      // that is asleep: only 5xx and network errors move on to the next GPU, and
      // never past the last candidate (its answer is the one to report).
      if (upstream.status >= 500 && index < candidates.length - 1) {
        failures.push({ url: base, status: upstream.status });
        continue;
      }
      const headers = new Headers(upstream.headers);
      headers.delete('access-control-allow-origin');
      headers.delete('transfer-encoding');
      headers.delete('connection');
      return new Response(upstream.body, { status: upstream.status, headers });
    } catch (err) {
      failures.push({ url: base, error: String((err && err.message) || err) });
    }
  }
  return json({
    error: 'No machine in the pool answered. Leave one PC with the agent running, or run '
      + 'install.cmd on another.',
    tried: failures,
  }, 502);
}

function downloadBase(env) {
  return String(env.DOWNLOAD_API || '').trim().replace(/\/+$/, '');
}

async function downloaderHealth(env) {
  const base = downloadBase(env);
  if (!base) {
    return json({
      configured: false,
      error: 'Set DOWNLOAD_API to the URL of the downloader service.',
    }, 200);
  }
  try {
    const upstream = await fetch(base + '/health', { headers: { accept: 'application/json' } });
    const body = await upstream.json().catch(() => ({}));
    return json({
      configured: true,
      url: base,
      reachable: upstream.ok,
      status: upstream.status,
      downloader: body,
    }, 200);
  } catch (err) {
    return json({
      configured: true,
      url: base,
      reachable: false,
      error: 'The download service did not answer. Free instances sleep when idle: '
        + 'open this again in a minute, or open the service URL directly to wake it.',
    }, 200);
  }
}

async function download(request, env, url) {
  const base = downloadBase(env);
  if (!base) {
    return json({
      error: 'No downloader is set up for this deployment yet. ' +
        'Set DOWNLOAD_API to the URL of the downloader service.',
    }, 503);
  }

  const init = { method: request.method, headers: {}, redirect: 'follow' };
  const contentType = request.headers.get('content-type');
  if (contentType) init.headers['content-type'] = contentType;
  // Bodies here are small JSON documents; reading them avoids streaming a
  // request body through a second fetch.
  if (request.method !== 'GET' && request.method !== 'HEAD') {
    init.body = await request.text();
  }

  let upstream;
  try {
    upstream = await fetch(base + url.pathname + url.search, init);
  } catch (err) {
    // Say which of the two it is: a sleeping free instance comes back on its own,
    // a wrong URL never will.
    let detail = 'It may be asleep or down.';
    try {
      const ping = await fetch(base + '/health', { headers: { accept: 'application/json' } });
      if (ping.ok) detail = 'It answered a health check just now, so try the download again.';
    } catch (ignored) {
      detail = 'Its health check did not answer either, so it is asleep or the URL is wrong.';
    }
    return json({ error: 'The download service could not be reached. ' + detail }, 502);
  }

  // Pass the service's answer straight through, headers included, so the file's
  // Content-Disposition still makes the browser save it.
  const headers = new Headers(upstream.headers);
  headers.delete('access-control-allow-origin');
  headers.delete('access-control-allow-credentials');
  // Hop-by-hop headers belong to the upstream connection, not to this response.
  headers.delete('transfer-encoding');
  headers.delete('connection');
  return new Response(upstream.body, { status: upstream.status, headers });
}

// Workers AI takes the audio as base64 text, and the clip is small (a few
// seconds of webm), so the whole thing is converted in one pass.
function toBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = '';
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

async function transcribe(request, env) {
  if (request.method === 'OPTIONS') return new Response(null, { status: 204 });
  if (request.method !== 'POST') {
    return json({ error: 'Send the recording as a POST body.' }, 405);
  }

  const audio = await request.arrayBuffer();
  if (!audio || audio.byteLength < 800) {
    return json({ error: 'The recording was empty.' }, 400);
  }
  if (audio.byteLength > MAX_CLIP_BYTES) {
    return json({ error: 'That recording is too long to transcribe.' }, 413);
  }

  let result;
  try {
    result = await env.AI.run(WHISPER_MODEL, { audio: toBase64(audio) });
  } catch (err) {
    return json({ error: 'The speech service could not transcribe that clip.' }, 502);
  }

  const text = String((result && result.text) || '').trim();
  return json({ text, model: WHISPER_MODEL });
}

// The video index, queried in place. Every word of the query has to appear in the
// row, so "dubstep mix" narrows instead of widening to anything holding either
// word, and the order prefers what people actually clicked.
//
// The page asks for thirty at a time and paints more as it scrolls; before this
// it downloaded all 8,724 rows before it could show a single card.
//
// What counts as a video. The library was filled once by an import that stored
// every link it met, so along with 6,400 real videos it holds about 2,300 web
// pages: support articles, forums, dictionaries, weather pages, restaurant menus.
// Those are not videos, cannot play, and had no business in a search result. Both
// the search and the ingest use this one list, so what is stored and what is shown
// agree.
const VIDEO_URL_PATTERNS = [
  '//youtube.com/', '//www.youtube.com/', '//m.youtube.com/', '//music.youtube.com/',
  '//youtu.be/', '//www.dailymotion.com/', '//dailymotion.com/',
  '//www.tiktok.com/', '//tiktok.com/', '//vm.tiktok.com/',
  '//vimeo.com/', '//www.vimeo.com/', '//www.instagram.com/', '//instagram.com/',
  '//twitter.com/', '//x.com/', '//soundcloud.com/', '//www.soundcloud.com/',
  '//www.bilibili.com/', '//www.facebook.com/', '//www.twitch.tv/',
];

function isVideoUrl(link) {
  const lowered = String(link || '').toLowerCase();
  return VIDEO_URL_PATTERNS.some((pattern) => lowered.includes(pattern));
}



// The shipped index's keys. Read once per isolate from a small asset, never the 3 MB
// index itself, so an ingest can tell an old video from a new one without fetching the
// whole library on every search.
let shippedKeys = { set: null, at: 0 };

async function shippedUrlKeys(env) {
  if (shippedKeys.set && Date.now() - shippedKeys.at < 10 * 60 * 1000) {
    return shippedKeys.set;
  }
  if (!env.ASSETS) return shippedKeys.set || new Set();
  try {
    const response = await env.ASSETS.fetch(new Request('https://library.local/data/seen.json'));
    if (response.ok) {
      const entry = await response.json();
      const list = Array.isArray(entry && entry.keys) ? entry.keys : [];
      if (list.length) {
        shippedKeys = { set: new Set(list), at: Date.now() };
        return shippedKeys.set;
      }
    }
  } catch (err) { /* an ingest without the list still stores what it is given */ }
  return shippedKeys.set || new Set();
}

function videoHostClause() {
  return '(' + VIDEO_URL_PATTERNS.map((p) => `url LIKE '%${p}%'`).join(' OR ') + ')';
}

// The same words searched again within a minute - a retry, a voice repeat, a
// reload - are answered from the edge cache instead of scanning the database
// again. A search that logs itself, or one that discovers new videos, has a side
// effect and is never cached.
async function cachedSearch(env, request, url) {
  // The counters are not a search: they are what the page shows while someone is
  // looking at them, and a minute-old copy of a number that a click just moved
  // reads as "the click did not count".
  const askingForCounters = !(url.searchParams.get('q') || '').trim();
  const plain = request.method === 'GET'
    && url.searchParams.get('log') !== '1'
    && url.searchParams.get('discover') !== '1'
    && !askingForCounters;
  if (!plain) return searchIndex(env, url);

  const cache = caches.default;
  const key = new Request('https://search-cache.internal' + url.pathname + url.search, { method: 'GET' });
  // A cache that cannot be read is a cache miss, not a failed search: this sat
  // outside every guard, so an error here reached the route's catch as a 500.
  let hit = null;
  try {
    hit = await cache.match(key);
  } catch (err) {
    console.error('[search] cache.match failed:', (err && (err.stack || err.message)) || err);
  }
  if (hit) return hit;

  const fresh = await searchIndex(env, url);
  if (fresh.status === 200) {
    // A body can be read once. Passing `fresh.body` into another Response locks
    // it, so returning `fresh` afterwards threw "Body has already been used" and
    // Cloudflare answered 500 (error 1101) - measured on 10 of 120 requests, all
    // of them cache misses on this path, which is what made it look intermittent.
    // clone() gives the cache its own stream and leaves `fresh` readable.
    const forCache = fresh.clone();
    const copy = new Response(forCache.body, {
      status: forCache.status,
      statusText: forCache.statusText,
      headers: forCache.headers,
    });
    copy.headers.set('Cache-Control', 'public, max-age=60');
    await cache.put(key, copy).catch((err) => {
      console.error('[search] cache.put failed:', (err && (err.stack || err.message)) || err);
    });
  }
  return fresh;
}

async function searchIndex(env, url) {
  const query = (url.searchParams.get('q') || '').trim().slice(0, 120);
  // 200 a page, not 60: a query with 332 matches used to say "332 matches" and
  // show sixty of them, because that is all one request returned.
  const limit = Math.min(200, Math.max(1, Number(url.searchParams.get('limit')) || 30));
  const offset = Math.max(0, Number(url.searchParams.get('offset')) || 0);

  if (!env.DB) {
    return json({ error: 'No video index is attached to this Worker.' }, 501);
  }

  if (!query) {
    // The empty query is the page's counter poll: it says whether the database can
    // be read as well as what the totals are, so the page can explain itself
    // instead of silently serving the export's older figures.
    const library = await readStats(env);
    // One source of truth for "can it be read": the shared verdict a failed search
    // left behind. Comparing totals here was per-isolate, so the same outage answered
    // readable:true on one request and readable:false on the next.
    const unreadable = await libraryUnreadable(env);
    // Either witness is enough to say no, and neither can say yes on its own: the
    // page treats "readable" as advice and only a successful search as proof.
    const why = unreadable
      || (statsLookBehind ? 'the library total is behind the last one read' : '');
    return json({
      query: '', total: 0, library, discovered: 0, videos: [],
      readable: !why,
      error: why || undefined,
    });
  }

  // A search is also how the library grows. Before counting, this asks
  // Dailymotion's public API for the same words and inserts only the videos the
  // table has never seen - so searching something new leaves the library larger
  // than it was, which is what a discovery tool is for.
  let discovered = 0;
  if (url.searchParams.get('discover') === '1') {
    discovered = await discoverVideos(env, query);
  }
  const library = await readStats(env);

  const terms = (query.toLowerCase().match(/[a-z0-9']+/g) || []).slice(0, 8);
  if (!terms.length) {
    return json({ query, total: 0, library, discovered, videos: [] });
  }

  // A search that was run is a search that was run, whether or not the database
  // can answer it. Counting it first is the difference between a number that moves
  // while the database is over its limit and one that sits still, and it costs one
  // KV read and one KV write.
  const logging = url.searchParams.get('log') === '1';
  if (logging) {
    try {
      const after = await bumpStats(env, 'total_searches', 1);
      if (after.total_searches) library.total_searches = after.total_searches;
    } catch (err) {
      // Counting the search is a nicety; failing it must not fail the search.
      console.error('[stats] bumpStats failed:', (err && (err.stack || err.message)) || err);
    }
  }

  // LIKE wildcards inside a search term are escaped, so a term of "100%" is a
  // search for that text rather than for everything.
  const patterns = terms.map((term) => '%' + term.replace(/[\\%_]/g, (ch) => '\\' + ch) + '%');
  const clause = patterns
    .map(() => "(lower(title) LIKE ? ESCAPE '\\' OR lower(description) LIKE ? ESCAPE '\\' "
      + "OR lower(tags) LIKE ? ESCAPE '\\')")
    .join(' AND ');
  const params = [];
  patterns.forEach((pattern) => params.push(pattern, pattern, pattern));

  // Only rows that are actually videos. About a quarter of the library is web
  // pages - support articles, forums, weather pages, pizza menus - collected by
  // an older import that stored any link it found. They are filtered out here so
  // a search never offers one, and a cleanup of the table can follow when the
  // database is accepting writes again.
  const videosOnly = clause + ' AND ' + videoHostClause();

  // Counting the matches is a full scan of the library (LIKE on three columns),
  // so it is done once, on the first page. Pages after it only need rows: the
  // page keeps the total from page one, and asking for it again on every one of
  // four paged requests was four scans for one search.
  // Counting every match is a scan of the whole library, and it was the single
  // biggest reader in the app: one "dubstep" search read 8,756 rows just to print
  // "328 matches". Capping the inner scan at 1000 rows keeps the number exact for
  // every ordinary search and costs far less, which is what stops the free daily
  // limit from running out in a day of browsing.
  let counted = null;
  let found = null;
  try {
    counted = offset > 0
      ? null
      : await env.DB.prepare(
        `SELECT COUNT(*) AS n FROM (SELECT 1 FROM videos WHERE ${videosOnly} LIMIT 1000)`
      ).bind(...params).first();

    found = await env.DB.prepare(
      'SELECT url, title, description, thumbnail, duration, source, click_count, tags, genre_json '
      + `FROM videos WHERE ${videosOnly} `
      + 'ORDER BY click_count DESC, relevance_score DESC, id DESC LIMIT ? OFFSET ?'
    ).bind(...params, limit, offset).all();
  } catch (err) {
    // The library could not be read. That is not the same as "no results": say so,
    // and still hand back the counters so the page does not fall back to zeros.
    const reason = String((err && err.message) || err).slice(0, 200);
    // Shared, not per isolate: the next request may land on a Worker that never saw
    // this failure, and it has to answer the same way.
    await noteLibraryUnreadable(env, reason);
    return json({
      error: 'The library could not be read just now.',
      detail: reason,
      query,
      total: 0,
      // The page reads this: it may say the library is unreadable, but it must not
      // replace the totals on screen with the bundled export's older ones.
      readable: false,
      library,
      discovered,
      videos: [],
    }, 503);
  }

  // A read that worked clears the shared verdict, so the banner goes away as soon as
  // the database answers again - this request (or any later one) may be the first.
  await noteLibraryReadable(env);

  const videos = (found.results || []).map((row) => ({
    u: row.url,
    t: row.title,
    d: row.description,
    th: row.thumbnail,
    du: row.duration,
    s: row.source,
    c: row.click_count,
    tg: row.tags,
    // The inferred genre, as the JSON export carries it: {n: name, c: confidence}.
    // It is the one column the database did not already have.
    g: parseGenre(row.genre_json),
  }));

  // Only searches a person made are kept: log=1 is sent by the page, so a crawler
  // following the URL cannot fill the history with its own queries.
  if (logging) {
    try {
      await env.DB.prepare(
        'INSERT INTO search_history (query, result_count, searched_at) VALUES (?, ?, ?)'
      ).bind(query, Number(counted && counted.n) || videos.length, Date.now() / 1000).run();
    } catch (err) { /* the history is a nicety; the count already happened above */ }
  }
  return json({
    query,
    total: counted ? (Number(counted.n) || 0) : Number(videos.length) + offset,
    library,
    discovered,
    videos,
  });
}

// Dailymotion's public API answers without a key or an account, and the library
// is already mostly Dailymotion, so the thumbnails, durations and URLs arrive in
// the same shape the existing rows use.
const DISCOVER_API = 'https://api.dailymotion.com/videos';

async function discoverVideos(env, query, limit = 20) {
  let doc;
  try {
    const api = DISCOVER_API
      + '?search=' + encodeURIComponent(query)
      + '&fields=id,title,url,description,thumbnail_360_url,duration'
      + '&sort=relevance&limit=' + limit;
    const response = await fetch(api, { headers: { accept: 'application/json' } });
    if (!response.ok) return 0;
    doc = await response.json();
  } catch (err) {
    return 0;   // discovery failing must never fail the search
  }

  const found = Array.isArray(doc && doc.list) ? doc.list : [];
  if (!found.length) return 0;

  const statements = [];
  const seen = new Set();
  // A video the shipped index already lists is already in the library. Storing it again
  // would count it twice in the total, which is what made the figure 8,882 against 8,804.
  const shipped = await shippedUrlKeys(env);
  for (const row of found) {
    const link = String(row.url || '').trim();
    const title = String(row.title || '').trim();
    if (!link || !title || seen.has(link)) continue;
    if (shipped.has(videoKey(link))) continue;
    seen.add(link);
    statements.push(
      env.DB.prepare(
        'INSERT OR IGNORE INTO videos '
        + '(url, title, description, thumbnail, duration, source, platform, discovered_at, '
        + ' click_count, relevance_score, tags) '
        + "VALUES (?, ?, ?, ?, ?, 'Dailymotion', 'Dailymotion', ?, 0, 0, '')"
      ).bind(
        link,
        tidyText(title).slice(0, 300),
        tidyText(row.description).slice(0, 2000),
        String(row.thumbnail_360_url || ''),
        formatSeconds(row.duration),
        Date.now() / 1000
      )
    );
  }
  if (!statements.length) return 0;

  // meta.changes counts the rows INSERT OR IGNORE actually wrote: a video the
  // library already had is ignored and not counted as new.
  let added = 0;
  try {
    const results = await env.DB.batch(statements);
    for (const result of results) {
      added += Number((result.meta && result.meta.changes) || 0);
    }
  } catch (err) {
    return 0;
  }
  // A discovered video is a video in the library, so it is counted like any other. This
  // path stored rows without ever moving the total, which is why a search that found
  // twenty new videos could leave the library figure exactly where it was.
  if (added) await bumpVideoTotal(env, added);
  return added;
}

function formatSeconds(value) {
  const total = Math.round(Number(value) || 0);
  if (!total) return 'Unknown';
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return minutes + ':' + String(seconds).padStart(2, '0');
}

async function syncSession(env, request, url) {
  if (request.method === 'OPTIONS') return new Response(null, { status: 204 });
  if (!env.DB && !env.STATS) return json({ error: 'No video index is attached to this Worker.' }, 501);

  // Codes are shown to people, so they stay short and typable: letters, digits
  // and dashes only.
  const code = String(url.searchParams.get('code') || '')
    .toUpperCase()
    .replace(/[^A-Z0-9-]/g, '')
    .slice(0, 12);
  if (code.length < 4) return json({ error: 'A sync code is needed.' }, 400);

  if (request.method === 'GET') {
    // KV first: it answers while D1 is over its daily allowance, which is exactly
    // when a saved search is worth having.
    if (env.STATS) {
      try {
        const text = await env.STATS.get('sync:' + code);
        if (text) {
          const doc = JSON.parse(text);
          return json({ code, payload: doc.payload || null, updated_at: Number(doc.at) || 0 });
        }
      } catch (err) { /* fall back to the database copy */ }
    }
    try {
      // Newest save wins. Saves are appended rather than overwritten, because
      // updating a row in place makes D1 read it first - and a database that is
      // refusing reads would then refuse the save too.
      const row = await env.DB.prepare(
        'SELECT payload, updated_at FROM sync_saves WHERE code = ? ORDER BY id DESC LIMIT 1'
      ).bind(code).first();
      if (!row) {
        const older = await env.DB.prepare(
          'SELECT payload, updated_at FROM sync_sessions WHERE code = ?'
        ).bind(code).first();
        return json({
          code,
          payload: older ? older.payload : null,
          updated_at: older ? Number(older.updated_at) || 0 : 0,
        });
      }
      return json({
        code,
        payload: row ? row.payload : null,
        updated_at: row ? Number(row.updated_at) || 0 : 0,
      });
    } catch (err) {
      return json({ error: 'The saved search could not be read.' }, 503);
    }
  }

  if (request.method === 'POST') {
    const body = await request.json().catch(() => null);
    const payload = body && typeof body.payload === 'string' ? body.payload : '';
    if (!payload) return json({ error: 'There was nothing to save.' }, 400);

    // One write, no read, nothing for the database's daily limit to refuse.
    if (env.STATS) {
      try {
        await env.STATS.put('sync:' + code, JSON.stringify({
          payload: payload.slice(0, 200000),
          at: Date.now(),
        }));
        return json({ ok: true, code, stored: 'kv' });
      } catch (err) { /* try the database instead */ }
    }

    try {
      await env.DB.prepare(
        'INSERT INTO sync_saves (code, payload, updated_at) VALUES (?, ?, ?)'
      ).bind(code, payload.slice(0, 200000), Date.now()).run();
      return json({ ok: true, code, stored: 'db' });
    } catch (err) {
      // Nothing was stored anywhere: the page keeps the search and tries again.
      return json({
        error: 'The search could not be saved to the server.',
        detail: String((err && err.message) || err).slice(0, 200),
      }, 503);
    }
  }

  return json({ error: 'Only reading and saving are supported here.' }, 405);
}

async function ingestVideos(env, request) {
  if (request.method === 'OPTIONS') return new Response(null, { status: 204 });
  if (request.method !== 'POST') {
    return json({ error: 'Send the videos as a POST body.' }, 405);
  }
  if (!env.DB) {
    return json({ error: 'No video index is attached to this Worker.' }, 501);
  }

  const body = await request.json().catch(() => null);
  const list = Array.isArray(body && body.videos) ? body.videos.slice(0, 40) : [];
  if (!list.length) {
    return json({ error: 'No videos were given.' }, 400);
  }

  const statements = [];
  const seen = new Set();
  const shipped = await shippedUrlKeys(env);
  for (const video of list) {
    const link = String((video && video.url) || '').trim();
    const title = String((video && video.title) || '').trim();
    if (!/^https?:\/\//.test(link) || !title || seen.has(link)) continue;
    // A web page is not a video. The library once filled up with forum threads,
    // dictionaries and restaurant menus because anything with a URL was stored;
    // nothing that is not on a video host gets in again.
    if (!isVideoUrl(link)) continue;
    // Already shipped in the index: it is in the library, so storing it again would
    // count one video twice.
    if (shipped.has(videoKey(link))) continue;
    seen.add(link);
    const source = String((video.source) || 'YouTube').slice(0, 40);
    statements.push(
      env.DB.prepare(
        'INSERT OR IGNORE INTO videos '
        + '(url, title, description, thumbnail, duration, source, platform, discovered_at, '
        + ' click_count, relevance_score, tags) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, \'\')'
      ).bind(
        link,
        tidyText(title).slice(0, 300),
        tidyText(video.description).slice(0, 2000),
        String(video.thumbnail || ''),
        String(video.duration || 'Unknown'),
        source,
        source,
        Date.now() / 1000
      )
    );
  }
  if (!statements.length) {
    return json({ added: 0, library: await readStats(env) });
  }

  let added = 0;
  try {
    const results = await env.DB.batch(statements);
    for (const result of results) added += Number((result.meta && result.meta.changes) || 0);
    if (added) await bumpVideoTotal(env, added);
  } catch (err) {
    // The reason is useful: a full disk, a locked table and an exhausted daily
    // limit all look identical from the page otherwise.
    return json({
      error: 'Those videos could not be stored.',
      detail: String((err && err.message) || err).slice(0, 300),
    }, 500);
  }
  return json({ added, library: await readStats(env) });
}

async function recordClick(env, request) {
  if (request.method === 'OPTIONS') return new Response(null, { status: 204 });
  if (request.method !== 'POST') {
    return json({ error: 'Send the click as a POST body.' }, 405);
  }
  if (!env.DB) {
    return json({ error: 'No video index is attached to this Worker.' }, 501);
  }

  const body = await request.json().catch(() => null);
  const videoUrl = String((body && body.url) || '').trim().slice(0, 400);
  if (!videoUrl) {
    return json({ error: 'No video was given.' }, 400);
  }
  const title = String((body && body.title) || '').slice(0, 300);
  const query = String((body && body.query) || '').slice(0, 120);

  // Every click carries the id the page gave it. The click is stored under that
  // id first, and only a row that was actually new moves the counters. A click
  // the page sends again because it never saw the answer - a lost response, a
  // page closed mid-request, a queue flushed twice - lands on the same id and is
  // ignored, so the count can no longer run ahead of the clicks that happened.
  const eventId = String((body && body.id) || '').trim().slice(0, 80);
  if (!eventId) {
    return json({ error: 'A click needs an id.' }, 400);
  }

  // The click itself is counted in KV: one read, one write, no rows scanned and no
  // dependence on D1 being able to answer. The ids of the clicks already counted
  // travel in the same document, so sending the same click again finds its id and
  // writes nothing.
  const doc = await readStatsDoc(env);
  if (!doc.v && !doc.s && !doc.c) {
    const seeded = await indexTotals(env);
    doc.v = seeded.total_videos;
    doc.s = seeded.total_searches;
    doc.c = seeded.total_clicks;
  }

  if (doc.ids.includes(eventId)) {
    return json({ ok: true, duplicate: true, library: statsFromDoc(doc) });
  }

  doc.c += 1;
  doc.ids.push(eventId);
  const stored = await writeStatsDoc(env, doc);
  if (!stored) {
    // Nothing was counted: the page keeps the click and sends it again.
    return json({ ok: false, queued: true, library: null });
  }

  // What the click teaches the library is secondary, and it is the part that needs
  // D1. A failure here no longer loses the click or holds the counter back.
  try {
    await env.DB.batch([
      env.DB.prepare('UPDATE videos SET click_count = click_count + 1 WHERE url = ?').bind(videoUrl),
      env.DB.prepare(
        'INSERT INTO learned_preferences (query, clicked_url, clicked_title, learned_at) VALUES (?, ?, ?, ?)'
      ).bind(query, videoUrl, title, Date.now() / 1000),
      env.DB.prepare(
        'INSERT INTO click_events (event_id, url, query, title, clicked_at, counted) VALUES (?, ?, ?, ?, ?, 1) '
        + 'ON CONFLICT(event_id) DO NOTHING'
      ).bind(eventId, videoUrl, query, title, Date.now()),
    ]);
  } catch (err) { /* the click is counted; the library catches up when D1 works */ }

  return json({ ok: true, duplicate: false, library: statsFromDoc(doc) });
}

function parseGenre(text) {
  if (!text) return null;
  try {
    const parsed = JSON.parse(text);
    return parsed && parsed.n ? parsed : null;
  } catch (err) {
    return null;
  }
}

// The counters and the clicks live in KV, not in D1. D1 bills in rows read, and
// the counters are read on page load, on every search and after every click, so
// they alone could run an account out of its free daily allowance - and once D1
// stops answering, storing a click stops too. KV bills in operations and allows
// far more of them, so one small document holds the totals and the ids of the
// clicks already counted, and D1 keeps only the videos.
const STATS_KEY = 'library';
const SEEN_CLICK_LIMIT = 200;

function statsFromDoc(doc) {
  return {
    total_videos: Number(doc.v) || 0,
    total_searches: Number(doc.s) || 0,
    total_clicks: Number(doc.c) || 0,
  };
}

async function readStatsDoc(env) {
  if (!env.STATS) return { v: 0, s: 0, c: 0, ids: [] };
  try {
    const text = await env.STATS.get(STATS_KEY);
    if (!text) return { v: 0, s: 0, c: 0, ids: [] };
    const doc = JSON.parse(text);
    return {
      v: Number(doc.v) || 0,
      s: Number(doc.s) || 0,
      c: Number(doc.c) || 0,
      ids: Array.isArray(doc.ids) ? doc.ids.slice(-SEEN_CLICK_LIMIT) : [],
    };
  } catch (err) {
    return { v: 0, s: 0, c: 0, ids: [] };
  }
}

async function writeStatsDoc(env, doc) {
  if (!env.STATS) return false;
  try {
    await env.STATS.put(STATS_KEY, JSON.stringify({
      v: Number(doc.v) || 0,
      s: Number(doc.s) || 0,
      c: Number(doc.c) || 0,
      ids: (doc.ids || []).slice(-SEEN_CLICK_LIMIT),
      at: Date.now(),
    }));
    return true;
  } catch (err) {
    return false;
  }
}

// How many rows the shipped index actually holds. A counter only starts from whatever the
// library held the day it was seeded and then counts new ingests, so every re-export left
// it further behind (8,067 against 8,724). The number is read from the export's own
// library.json - a few hundred bytes, not the 3 MB index - and kept for ten minutes per
// isolate, because it changes only when the site is deployed again.
let shippedLibrary = { count: 0, at: 0 };

async function shippedCount(env) {
  if (shippedLibrary.count && Date.now() - shippedLibrary.at < 10 * 60 * 1000) {
    return shippedLibrary.count;
  }
  if (!env.ASSETS) return shippedLibrary.count;
  try {
    // The key list is the count that matters: the index can hold one video twice under two
    // links, and the library should count the video once.
    const response = await env.ASSETS.fetch(new Request('https://library.local/data/seen.json'));
    if (response.ok) {
      const entry = await response.json();
      const count = Number(entry && entry.count) || 0;
      if (count > 0) shippedLibrary = { count, at: Date.now() };
      return shippedLibrary.count;
    }
  } catch (err) { /* fall back to the row count */ }
  try {
    const response = await env.ASSETS.fetch(new Request('https://library.local/data/library.json'));
    if (!response.ok) return shippedLibrary.count;
    const entry = await response.json();
    const count = Number(entry && entry.count) || 0;
    if (count > 0) shippedLibrary = { count, at: Date.now() };
  } catch (err) { /* the counter answers without it */ }
  return shippedLibrary.count;
}

// The video total lives in D1, where one statement can add to it. It used to live in the
// same KV document as the search and click counts, and a KV write replaces the whole
// document: whichever counter wrote last won, so twenty discovered videos disappeared
// under the next search's write of an older figure. A search's count of searches cannot
// also decide how many videos there are.
async function d1Videos(env) {
  if (!env.DB) return 0;
  try {
    const row = await env.DB.prepare(
      'SELECT total_videos FROM library_stats WHERE id = 1'
    ).first();
    return Number(row && row.total_videos) || 0;
  } catch (err) {
    return 0;
  }
}

// Every path that stores videos counts them the same way, in one statement.
async function bumpVideoTotal(env, added) {
  if (!env.DB || !added) return;
  try {
    await env.DB.batch([
      env.DB.prepare(
        'INSERT OR IGNORE INTO library_stats (id, total_videos, total_searches, total_clicks) '
        + 'VALUES (1, 0, 0, 0)'
      ),
      env.DB.prepare('UPDATE library_stats SET total_videos = total_videos + ? WHERE id = 1')
        .bind(added),
    ]);
  } catch (err) { /* the library still grew; the page adds what it found */ }
}

// Raise the stored total to the shipped index, never lower it: a counter below what is
// served has drifted, a counter above it has counted new rows. The comparison is inside
// the statement, so two isolates rebasing at once cannot undo each other.
async function rebaseVideoTotal(env, count) {
  if (!env.DB || !count) return;
  try {
    await env.DB.batch([
      env.DB.prepare(
        'INSERT OR IGNORE INTO library_stats (id, total_videos, total_searches, total_clicks) '
        + 'VALUES (1, 0, 0, 0)'
      ),
      env.DB.prepare('UPDATE library_stats SET total_videos = ? WHERE id = 1 AND total_videos < ?')
        .bind(count, count),
    ]);
  } catch (err) { /* the page falls back to the shipped count */ }
}

// The library is what the index ships plus what the database holds that the index does
// not. Counting the overlap twice was invisible while the two grew apart; now that a
// discovery is counted as it happens, the overlap has to be measured. It is measured once
// per export - the rows the database holds are compared against the shipped keys - and
// after that the ingest path keeps new rows out of the overlap by refusing them.
// The marker remembers which export was reconciled, so a new export reconciles again.
const RECONCILE_VERSION = 4;

// The database kept `\u0026` where the index kept `&`, because a title arrived as escaped
// JSON text and was stored as given. Decoded on the way in, so the two sides say the same
// thing about a video.
function tidyText(text) {
  return String(text == null ? '' : text).replace(/\\u([0-9a-fA-F]{4})/g,
    (match, hex) => String.fromCharCode(parseInt(hex, 16)));
}

async function exactVideoTotal(env, shipped) {
  if (!env.DB || !shipped) return 0;
  const marker = String(shipped) + ':' + RECONCILE_VERSION;
  let reconciled = '';
  try { reconciled = env.STATS ? (await env.STATS.get('reconciled')) || '' : ''; } catch (err) {}
  if (reconciled === marker) return 0;
  const keys = await shippedUrlKeys(env);
  if (!keys.size) return 0;
  try {
    // Read the database in pages: a single answer is not guaranteed to carry every row,
    // and a total measured from half the table would be worse than the estimate it
    // replaces.
    let extra = 0;
    for (let offset = 0; offset < 200000; offset += 500) {
      const page = await env.DB.prepare('SELECT url FROM videos ORDER BY id LIMIT 500 OFFSET ?')
        .bind(offset).all();
      const rows = (page && page.results) || [];
      for (const row of rows) {
        if (!keys.has(videoKey(row.url))) extra += 1;
      }
      if (rows.length < 500) break;
    }
    const total = shipped + extra;
    await env.DB.batch([
      env.DB.prepare(
        'INSERT OR IGNORE INTO library_stats (id, total_videos, total_searches, total_clicks) '
        + 'VALUES (1, 0, 0, 0)'
      ),
      env.DB.prepare('UPDATE library_stats SET total_videos = ? WHERE id = 1').bind(total),
    ]);
    if (env.STATS) await env.STATS.put('reconciled', marker);
    return total;
  } catch (err) {
    return 0;
  }
}

// First use after this change: the database still holds the real totals, so they
// are copied across once and KV answers from then on. If D1 cannot be read either,
// the answer is zeroes and the page keeps showing what it already knows.
async function readStatsUnsafe(env) {
  const doc = await readStatsDoc(env);
  if (!doc.v && !doc.s && !doc.c) {
    const seeded = await indexTotals(env);
    if (seeded.total_videos || seeded.total_searches || seeded.total_clicks) {
      await writeStatsDoc(env, {
        v: seeded.total_videos, s: seeded.total_searches, c: seeded.total_clicks, ids: [],
      });
    }
  }
  const shipped = await shippedCount(env);
  let stored = await d1Videos(env);
  const exact = await exactVideoTotal(env, shipped);
  if (exact) stored = exact;
  if (shipped > stored) {
    await rebaseVideoTotal(env, shipped);
  }
  return {
    total_videos: Math.max(doc.v, shipped, stored),
    total_searches: Number(doc.s) || 0,
    total_clicks: Number(doc.c) || 0,
  };
}

// The counters are decoration, and this runs before the search's own try/catch: a
// KV or D1 hiccup in here turned a query that could still be answered into a 500.
// One such 500 was seen on the live site (the next two identical calls answered
// 200) and could not be reproduced, so the failure is made harmless and loud
// rather than hunted in the dark: the search answers, the log says what broke.
// The last totals a search actually saw, kept in KV so a database that cannot be
// read does not make the page fall back to the bundled export's old numbers. One
// write every five minutes, not one per search: KV allows 1,000 a day.
const LIBRARY_KEY = 'library_snapshot';

async function readLibrarySnapshot(env) {
  if (!env.STATS) return null;
  try {
    const raw = await env.STATS.get(LIBRARY_KEY);
    const doc = raw ? JSON.parse(raw) : null;
    return doc && Number(doc.v) ? doc : null;
  } catch (err) {
    return null;
  }
}

async function saveLibrarySnapshot(env, library) {
  if (!env.STATS || !library || !Number(library.total_videos)) return;
  try {
    const existing = await readLibrarySnapshot(env);
    // Never write a smaller total over a larger one: a read that fell back to the
    // export's 8,724 used to clobber a snapshot holding 11,854, and the page then
    // showed the smaller figure as if it were the library.
    if (existing && Number(existing.v) > Number(library.total_videos)) return;
    if (existing && Date.now() - Number(existing.at || 0) < 5 * 60 * 1000) return;
    await env.STATS.put(LIBRARY_KEY, JSON.stringify({
      v: library.total_videos, s: library.total_searches, c: library.total_clicks, at: Date.now(),
    }));
  } catch (err) {
    // Counters are decoration: a refused KV write must not fail a search.
  }
}

function snapshotAsLibrary(snapshot) {
  if (!snapshot) return null;
  return {
    total_videos: Number(snapshot.v) || 0,
    total_searches: Number(snapshot.s) || 0,
    total_clicks: Number(snapshot.c) || 0,
  };
}

// One row, not zero: `SELECT 1` costs nothing to read, so a spent daily row
// allowance answers it happily and the probe lies. Asking for a real row fails
// exactly when a search would.
async function d1Problem(env) {
  if (!env.DB) return 'no database is bound';
  try {
    await env.DB.prepare('SELECT 1 AS x FROM videos LIMIT 1').first();
    return '';
  } catch (err) {
    return String((err && err.message) || err).slice(0, 200);
  }
}

// True when the live count is behind the last one this Worker managed to read: the
// database is answering from something stale (a spent allowance makes its reads
// fail, and the helpers then fall back to the export's 8,724). Kept for the log;
// the answer the page gets comes from the shared verdict below, because this flag
// differs per isolate and flipped readable:true/false on consecutive requests.
let statsLookBehind = false;

// Why the library could not be read, shared through KV so every isolate agrees. A
// real row read is the only honest probe: a one-row SELECT still answers when the
// daily allowance is spent, so the failure itself is what gets recorded.
const D1_DOWN_KEY = 'library_unreadable';
const D1_DOWN_MS = 15 * 60 * 1000;   // a verdict older than this stops counting
let d1DownMarkedAt = 0;              // per isolate: keeps the writes cheap

async function noteLibraryUnreadable(env, reason) {
  if (!env.STATS) return;
  const now = Date.now();
  if (now - d1DownMarkedAt < 60 * 1000) return;   // one write a minute at most
  d1DownMarkedAt = now;
  try {
    await env.STATS.put(D1_DOWN_KEY, JSON.stringify({ at: now, reason: String(reason || '').slice(0, 200) }));
  } catch (err) {
    // KV refused the write: the page still gets readable:false on this response.
  }
}

async function noteLibraryReadable(env) {
  if (!env.STATS) return;
  try {
    if (await env.STATS.get(D1_DOWN_KEY)) await env.STATS.delete(D1_DOWN_KEY);
    d1DownMarkedAt = 0;
  } catch (err) {
    // Nothing to do: a stale verdict expires on its own after D1_DOWN_MS.
  }
}

async function libraryUnreadable(env) {
  if (!env.STATS) return null;
  try {
    const raw = await env.STATS.get(D1_DOWN_KEY);
    if (!raw) return null;
    const doc = JSON.parse(raw);
    if (!doc || Date.now() - Number(doc.at || 0) > D1_DOWN_MS) return null;
    return String(doc.reason || 'the library could not be read');
  } catch (err) {
    return null;
  }
}

async function readStats(env) {
  try {
    const live = await readStatsUnsafe(env);
    const fromSnapshot = snapshotAsLibrary(await readLibrarySnapshot(env));
    if (!fromSnapshot) {
      statsLookBehind = false;
      if (Number(live.total_videos)) await saveLibrarySnapshot(env, live);
      return live;
    }
    const behind = (Number(live.total_videos) || 0) < fromSnapshot.total_videos;
    statsLookBehind = behind;
    // Never lower a number that is already on screen: the snapshot holds the last
    // totals a search actually read, and the counters only ever grow.
    const merged = {
      total_videos: Math.max(fromSnapshot.total_videos, Number(live.total_videos) || 0),
      total_searches: Math.max(fromSnapshot.total_searches, Number(live.total_searches) || 0),
      total_clicks: Math.max(fromSnapshot.total_clicks, Number(live.total_clicks) || 0),
    };
    if (behind) {
      console.error('[stats] live total is behind the last snapshot:', live.total_videos,
        'vs', fromSnapshot.total_videos, '- answering with the snapshot');
    } else {
      await saveLibrarySnapshot(env, merged);
    }
    return merged;
  } catch (err) {
    console.error('[stats] readStats failed:', (err && (err.stack || err.message)) || err);
    statsLookBehind = true;
    const fromSnapshot = snapshotAsLibrary(await readLibrarySnapshot(env));
    return fromSnapshot || { total_videos: 0, total_searches: 0, total_clicks: 0 };
  }
}

// The D1 totals, kept only to seed KV once and to answer if KV is missing.
async function indexTotals(env) {
  if (!env.DB) return { total_videos: 0, total_searches: 0, total_clicks: 0 };
  try {
    const row = await env.DB.prepare(
      'SELECT total_videos, total_searches, total_clicks FROM library_stats WHERE id = 1'
    ).first();
    return {
      total_videos: Number(row && row.total_videos) || 0,
      total_searches: Number(row && row.total_searches) || 0,
      total_clicks: Number(row && row.total_clicks) || 0,
    };
  } catch (err) {
    return { total_videos: 0, total_searches: 0, total_clicks: 0 };
  }
}

// One read and one write in KV, whatever is being counted.
async function bumpStats(env, field, amount) {
  const doc = await readStatsDoc(env);
  if (!doc.v && !doc.s && !doc.c) {
    const seeded = await indexTotals(env);
    doc.v = seeded.total_videos;
    doc.s = seeded.total_searches;
    doc.c = seeded.total_clicks;
  }
  if (field === 'total_videos') doc.v += amount;
  else if (field === 'total_searches') doc.s += amount;
  else if (field === 'total_clicks') doc.c += amount;
  await writeStatsDoc(env, doc);
  return statsFromDoc(doc);
}

function json(body, status) {
  return new Response(JSON.stringify(body), {
    status: status || 200,
    headers: {
      'Content-Type': 'application/json; charset=utf-8',
      'Cache-Control': 'no-store',
    },
  });
}
