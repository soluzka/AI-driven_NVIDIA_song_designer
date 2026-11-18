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
        // it. The reason is worth more than the tidiness.
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

    if (ENDPOINT_ROUTE.test(url.pathname)) {
      return songEndpoint(request, env);
    }

    if (POOL_ROUTE.test(url.pathname)) {
      const pool = await readPool(env);
      return json({
        count: pool.length,
        machines: pool.map((entry) => ({ url: entry.url, seen: entry.seen })),
      });
    }

    if (url.pathname === '/api/song/probe') {
      return songProbe(url);
    }

    if (SONG_ROUTES.some((route) => route.test(url.pathname))) {
      return song(request, env, url);
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

// --- the GPU pool ------------------------------------------------------------
// Every machine that installs the agent publishes its own tunnel address here.
// The KV entry is a list, not one string: with several GPUs online a model call
// goes to a machine chosen at random and the next one is tried when it does not
// answer, so the site keeps working whichever PC is awake.
const POOL_KEY = 'song_pool';
const POOL_LIMIT = 8;                                  // machines kept
const POOL_TTL_MS = 7 * 24 * 60 * 60 * 1000;           // forgotten after a week

async function readPool(env) {
  // A separate pool base wins when it is configured. That Worker holds only the
  // list, so it survives a redeploy of the site and can be run by whoever owns the
  // cards. An unreachable pool base falls through to this Worker's own KV, so one
  // broken address cannot take the site's models offline.
  const remote = String(env.POOL_API || '').trim().replace(/\/+$/, '');
  if (remote) {
    try {
      const answer = await fetch(remote + '/pool', { headers: { accept: 'application/json' } });
      if (answer.ok) {
        const info = await answer.json();
        const list = (info.machines || [])
          .map((machine) => ({ url: String(machine.url || '').replace(/\/+$/, ''), seen: machine.seen || Date.now() }))
          .filter((machine) => machine.url);
        if (list.length) return livePool(list);
      }
    } catch (err) {}
  }
  if (!env.STATS) return [];
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
  return livePool(pool);
}

// --- which of those addresses answers ----------------------------------------
// A Quick Tunnel name dies with the session that made it, but its entry stays in
// the list for seven days. The header counted those dead machines as GPUs, and a
// render picked one of them at random, so every so often a song failed and had to
// be retried. Each list is probed before it is handed out, and the answer is kept
// for POOL_PROBE_MS so a page that asks twice does not probe twice.
const POOL_PROBE_MS = 15000;
let poolProbeCache = { at: 0, key: '', live: [] };

async function poolAnswers(address) {
  try {
    const reply = await fetch(String(address).replace(/\/+$/, '') + '/health', {
      headers: { accept: 'application/json' },
      signal: AbortSignal.timeout(4000),
    });
    return reply.ok;
  } catch (err) {
    return false;
  }
}

async function livePool(list) {
  if (!Array.isArray(list) || !list.length) return [];
  const key = list.map((entry) => entry.url).join(' ');
  if (poolProbeCache.key === key && Date.now() - poolProbeCache.at < POOL_PROBE_MS) {
    return poolProbeCache.live;
  }
  const verdicts = await Promise.all(
    list.map(async (entry) => ({ entry, alive: await poolAnswers(entry.url) })));
  const live = verdicts.filter((item) => item.alive).map((item) => item.entry);
  poolProbeCache = { at: Date.now(), key, live };
  return live;
}

async function writePool(env, pool) {
  await env.STATS.put(POOL_KEY, JSON.stringify(pool.slice(0, POOL_LIMIT)));
  if (pool.length) await env.STATS.put('song_api', pool[0].url);
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

async function songEndpoint(request, env) {
  if (request.method === 'POST') {
    const expected = env.SONG_ENDPOINT_KEY || '';
    const given = request.headers.get('x-song-key') || '';
    if (!expected) {
      return json({ error: 'Publishing is disabled: this Worker has no SONG_ENDPOINT_KEY.' }, 503);
    }
    if (given !== expected) return json({ error: 'Wrong key.' }, 403);
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
      await writePool(env, pool);
      return json({ ok: true, removed: remove, pool: pool.map((entry) => entry.url) });
    }
    if (!value) {
      return json({ ok: true, url: '', pool: pool.map((entry) => entry.url) });
    }
    const now = Date.now();
    const known = pool.find((entry) => entry.url === value);
    if (known) known.seen = now;
    else pool.unshift({ url: value, seen: now });
    await writePool(env, pool);
    // When the pool lives on its own base, the machine belongs there too: this
    // Worker forwards the announcement so both lists agree, and neither the site
    // nor the pool has to be redeployed for the other to keep working.
    const poolBase = String(env.POOL_API || '').trim().replace(/\/+$/, '');
    if (poolBase) {
      try {
        await fetch(poolBase + '/join', {
          method: 'POST',
          headers: { 'content-type': 'application/json', 'x-join-key': env.SONG_ENDPOINT_KEY || '' },
          body: JSON.stringify({ url: value }),
        });
      } catch (err) {}
    }
    return json({ ok: true, url: value, pool: pool.map((entry) => entry.url) });
  }

  const pool = await readPool(env);
  return json({
    url: pool.length ? pool[0].url : (env.SONG_API || ''),
    urls: pool.map((entry) => entry.url),
    machines: pool.map((entry) => ({ url: entry.url, seen: entry.seen })),
  });
}


async function song(request, env, url) {
  // SONG_API wins when it is set; otherwise the address the machine published to
  // KV is the one to use. Reading only SONG_API made /api/song/endpoint report a
  // live address that the proxy then ignored, so every model call through
  // Cloudflare answered 501 while the page quietly fell back to loopback.
  // SONG_API pins one machine; otherwise every machine that published is a
  // candidate. The order starts at a random entry so the load spreads over the
  // pool, and a machine that does not answer is skipped for the next one.
  let candidates = [];
  const pinned = String(env.SONG_API || '').trim().replace(/\/+$/, '');
  if (pinned) {
    candidates = [pinned];
  } else {
    const pool = await readPool(env);
    candidates = pool.map((entry) => entry.url);
    if (candidates.length > 1) {
      const start = Math.floor(Math.random() * candidates.length);
      candidates = candidates.slice(start).concat(candidates.slice(0, start));
    }
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
  const hit = await cache.match(key);
  if (hit) return hit;

  const fresh = await searchIndex(env, url);
  if (fresh.status === 200) {
    const copy = new Response(fresh.body, {
      status: fresh.status,
      statusText: fresh.statusText,
      headers: fresh.headers,
    });
    copy.headers.set('Cache-Control', 'public, max-age=60');
    await cache.put(key, copy).catch(() => {});
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
    return json({ query: '', total: 0, library: await readStats(env), discovered: 0, videos: [] });
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
    const after = await bumpStats(env, 'total_searches', 1);
    if (after.total_searches) library.total_searches = after.total_searches;
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
    return json({
      error: 'The library could not be read just now.',
      detail: String((err && err.message) || err).slice(0, 200),
      query,
      total: 0,
      library,
      discovered,
      videos: [],
    }, 503);
  }

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
  for (const row of found) {
    const link = String(row.url || '').trim();
    const title = String(row.title || '').trim();
    if (!link || !title || seen.has(link)) continue;
    seen.add(link);
    statements.push(
      env.DB.prepare(
        'INSERT OR IGNORE INTO videos '
        + '(url, title, description, thumbnail, duration, source, platform, discovered_at, '
        + ' click_count, relevance_score, tags) '
        + "VALUES (?, ?, ?, ?, ?, 'Dailymotion', 'Dailymotion', ?, 0, 0, '')"
      ).bind(
        link,
        title.slice(0, 300),
        String(row.description || '').slice(0, 2000),
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
  for (const video of list) {
    const link = String((video && video.url) || '').trim();
    const title = String((video && video.title) || '').trim();
    if (!/^https?:\/\//.test(link) || !title || seen.has(link)) continue;
    // A web page is not a video. The library once filled up with forum threads,
    // dictionaries and restaurant menus because anything with a URL was stored;
    // nothing that is not on a video host gets in again.
    if (!isVideoUrl(link)) continue;
    seen.add(link);
    const source = String((video.source) || 'YouTube').slice(0, 40);
    statements.push(
      env.DB.prepare(
        'INSERT OR IGNORE INTO videos '
        + '(url, title, description, thumbnail, duration, source, platform, discovered_at, '
        + ' click_count, relevance_score, tags) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, \'\')'
      ).bind(
        link,
        title.slice(0, 300),
        String(video.description || '').slice(0, 2000),
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
    if (added) await bumpStats(env, 'total_videos', added);
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

// First use after this change: the database still holds the real totals, so they
// are copied across once and KV answers from then on. If D1 cannot be read either,
// the answer is zeroes and the page keeps showing what it already knows.
async function readStats(env) {
  const doc = await readStatsDoc(env);
  if (doc.v || doc.s || doc.c) return statsFromDoc(doc);
  const seeded = await indexTotals(env);
  if (seeded.total_videos || seeded.total_searches || seeded.total_clicks) {
    await writeStatsDoc(env, {
      v: seeded.total_videos, s: seeded.total_searches, c: seeded.total_clicks, ids: [],
    });
  }
  return seeded;
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
