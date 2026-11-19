/**
 * The GPU pool, as its own Worker and its own base.
 *
 * Nothing here runs a model. This Worker is the register and the dispatcher: the
 * machines that hold the cards announce themselves, and the site asks it which
 * machine to talk to. Keeping it separate from the site's Worker means the pool
 * survives a redeploy of the site, can be hosted on another account, and can be
 * read by anything that wants to know who is online.
 *
 *   GET  /pool     every machine that has joined, newest first
 *   GET  /pick     one machine, chosen at random (the site's routing)
 *   POST /join     {"url": "https://..."} with x-join-key: a machine announces itself
 *   POST /remove   {"url": "https://..."} with x-join-key: a machine leaves
 *   GET  /health   a plain 200, for uptime checks
 *
 * It is also a model base in its own right: the song paths below are forwarded to
 * a machine from the pool, so this second Worker can serve the AI on its own
 * without the site Worker in front of it. The weights still live on the GPU
 * machines — no Worker can hold 10 GB of model — but the requests, the routing and
 * the failover do run here.
 *
 * What this cannot do, and no Worker can: expose somebody's GPU to a browser
 * without a process on that machine. A page cannot reach a graphics card that is
 * not its own, and WebGPU runs inside the visiting tab, on the model code the page
 * already has — a 3.5B diffusion model with 10 GB of weights does not fit that.
 * So a machine joins by running the agent once (it then restarts itself at logon);
 * this Worker is what collects the machines and spreads the work over them.
 */

// The same model paths the site Worker proxies, so this base is usable directly.
const MODEL_ROUTES = [
  /^\/api\/lyrics$/i,
  /^\/api\/song$/i,
  /^\/api\/song\/[a-z0-9]+$/i,
  /^\/song_file\/[a-z0-9]+$/i,
  /^\/last_song$/i,
];

// The checkpoint can live in R2 and be streamed from here: 10 GB is storage, and
// R2 has no egress fee, so a machine can pull the weights from Cloudflare instead
// of from Hugging Face. It still cannot be *run* here — that needs CUDA — but the
// download, the versioning and the caching do fit.
const WEIGHTS_PREFIX = '/weights/';

const POOL_KEY = 'pool';
const POOL_LIMIT = 16;
const POOL_TTL_MS = 7 * 24 * 60 * 60 * 1000;   // a machine silent this long is dropped

const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Headers': 'content-type, x-join-key',
  'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
};

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store', ...CORS },
  });
}

async function readPool(env) {
  if (!env.POOL) return [];
  let entries = [];
  try {
    const raw = await env.POOL.get(POOL_KEY);
    entries = raw ? JSON.parse(raw) : [];
  } catch (err) {
    entries = [];
  }
  const cutoff = Date.now() - POOL_TTL_MS;
  return (Array.isArray(entries) ? entries : [])
    .filter((entry) => entry && typeof entry.url === 'string' && (entry.seen || 0) > cutoff)
    .sort((a, b) => (b.seen || 0) - (a.seen || 0));
}

async function writePool(env, pool) {
  await env.POOL.put(POOL_KEY, JSON.stringify(pool.slice(0, POOL_LIMIT)));
}

// A tunnel dies with the session that opened it, but the register only knew time:
// a machine was dropped after a week of silence, so yesterday's dead tunnel kept
// being handed out. The site then routed to nothing and its badge sat at "warming"
// over a machine that had not existed since the day before. So the register asks.
async function answers(base, timeout = 8000) {
  try {
    const answer = await fetch(base + '/health', {
      headers: { accept: 'application/json' },
      signal: AbortSignal.timeout(timeout),
    });
    if (!answer.ok) return false;
    const text = await answer.text();
    let body = null;
    try { body = JSON.parse(text); } catch (err) {}
    // A machine that answered but whose warm ended in warm_failed will never load:
    // it is reachable and cannot render, so it is not something to route a song to.
    if (body && body.warm_failed) return false;
    return true;
  } catch (err) {
    return false;
  }
}

// The pool that routing may use: every entry that still answers. Probes run in
// parallel, so the cost is one 2.5 s timeout rather than one per machine, and the
// register is rewritten only when something actually died.
// A failed probe is not proof a card is gone. A tunnel's first request pays for its TLS
// handshake, a Worker fetching a fresh trycloudflare hostname can be refused while a
// browser reaches it fine, and the boot only joins once - so deleting on the first miss
// erased machines that were alive and serving. Submissions are still dropped on a miss,
// because routing must never hand out a card that cannot render; the entry is kept for
// this long before it is forgotten, so a momentary failure costs nothing.
const POOL_GRACE_MS = 10 * 60 * 1000;

async function livingPool(env) {
  const pool = await readPool(env);
  if (!pool.length) return pool;
  const answered = await Promise.allSettled(pool.map((entry) => answers(entry.url)));
  const alive = pool.map((entry, index) => answered[index].status === 'fulfilled' && answered[index].value === true);
  const retry = pool.filter((entry, index) => !alive[index]);
  if (retry.length) {
    const again = await Promise.allSettled(retry.map((entry) => answers(entry.url, 12000)));
    let at = 0;
    pool.forEach((entry, index) => {
      if (alive[index]) return;
      alive[index] = again[at] && again[at].status === 'fulfilled' && again[at].value === true;
      at += 1;
    });
  }

  const now = Date.now();
  const living = [];
  const kept = [];
  let dirty = false;
  pool.forEach((entry, index) => {
    if (alive[index]) {
      // Only written once a minute: /pool is read by every page, and this entry is only
      // the clock that decides how long a silent machine is remembered.
      if (!entry.ok_at || now - entry.ok_at > 60000) {
        entry.ok_at = now;
        dirty = true;
      }
      entry.fail = 0;
      living.push(entry);
      kept.push(entry);
      return;
    }
    entry.fail = Number(entry.fail || 0) + 1;
    dirty = true;
    const last = Math.max(Number(entry.ok_at || 0), Number(entry.seen || 0));
    if (now - last < POOL_GRACE_MS) kept.push(entry);
  });
  if (dirty || kept.length !== pool.length) await writePool(env, kept);
  return living;
}

function authorised(request, env) {
  const expected = env.JOIN_KEY || '';
  if (!expected) return { ok: false, error: 'This pool has no JOIN_KEY set, so nobody can join.', status: 503 };
  if ((request.headers.get('x-join-key') || '') !== expected) return { ok: false, error: 'Wrong join key.', status: 403 };
  return { ok: true };
}

// One request, one machine: chosen at random, then the next when one does not
// answer. The body is read once and reused, so a large WAV upload survives a retry.
async function proxy(request, env, url) {
  const pool = await livingPool(env);
  if (!pool.length) {
    return json({ error: 'No GPU has joined the pool yet. Run install.cmd on a PC with an NVIDIA card.' }, 501);
  }
  const start = Math.floor(Math.random() * pool.length);
  const ordered = pool.slice(start).concat(pool.slice(0, start));

  const init = { method: request.method, headers: { accept: request.headers.get('accept') || '*/*' } };
  const contentType = request.headers.get('content-type');
  if (contentType) init.headers['content-type'] = contentType;
  let body;
  if (request.method !== 'GET' && request.method !== 'HEAD') body = await request.arrayBuffer();

  const failures = [];
  for (let index = 0; index < ordered.length; index++) {
    const base = ordered[index].url;
    try {
      const upstream = await fetch(base + url.pathname + url.search, body === undefined ? init : { ...init, body });
      if (upstream.status >= 500 && index < ordered.length - 1) {
        failures.push({ url: base, status: upstream.status });
        continue;
      }
      const headers = new Headers(upstream.headers);
      headers.delete('transfer-encoding');
      headers.delete('connection');
      headers.set('Access-Control-Allow-Origin', '*');
      return new Response(upstream.body, { status: upstream.status, headers });
    } catch (err) {
      failures.push({ url: base, error: String((err && err.message) || err) });
    }
  }
  return json({ error: 'No machine in the pool answered.', tried: failures }, 502);
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === 'OPTIONS') return new Response('', { status: 204, headers: CORS });

    if (url.pathname === '/health') {
      return json({ ok: true, pool: (await readPool(env)).length });
    }

    if (url.pathname === '/pool') {
      const pool = await livingPool(env);
      return json({
        count: pool.length,
        machines: pool.map((entry) => ({
          url: entry.url,
          seen: entry.seen,
          // When this card first joined, which is what "the new GPU" means. seen
          // moves on every re-join, so it cannot answer that on its own.
          joined: entry.joined || entry.seen,
          note: entry.note || '',
        })),
      });
    }

    if (url.pathname === '/pick') {
      const pool = await livingPool(env);
      if (!pool.length) return json({ error: 'No machine has joined the pool yet.' }, 404);
      // Random rather than first: the cards share the work, and a machine that has
      // just gone to sleep is not always the one a request is sent to first.
      const chosen = pool[Math.floor(Math.random() * pool.length)];
      return json({ url: chosen.url, count: pool.length });
    }

    if (MODEL_ROUTES.some((route) => route.test(url.pathname))) {
      return proxy(request, env, url);
    }

    if (url.pathname.startsWith(WEIGHTS_PREFIX)) {
      if (!env.WEIGHTS) {
        return json({
          error: 'No R2 bucket is bound to this Worker. Create one and add it to '
            + 'wrangler.pool.jsonc as a binding named WEIGHTS; the checkpoint is stored, '
            + 'not run, here: a Worker has no GPU.',
        }, 501);
      }
      const key = decodeURIComponent(url.pathname.slice(WEIGHTS_PREFIX.length));
      const object = await env.WEIGHTS.get(key);
      if (!object) return json({ error: 'No such object: ' + key }, 404);
      const headers = new Headers();
      headers.set('content-type', (object.httpMetadata && object.httpMetadata.contentType) || 'application/octet-stream');
      headers.set('content-length', String(object.size));
      headers.set('cache-control', 'public, max-age=31536000, immutable');
      headers.set('etag', object.httpEtag || '');
      headers.set('Access-Control-Allow-Origin', '*');
      return new Response(object.body, { status: 200, headers });
    }

    if (url.pathname === '/join' && request.method === 'POST') {
      const allowed = authorised(request, env);
      if (!allowed.ok) return json({ error: allowed.error }, allowed.status);
      let body = {};
      try {
        body = await request.json();
      } catch (err) {
        return json({ error: 'Expected JSON: {"url": "https://..."}' }, 400);
      }
      const value = String((body && body.url) || '').trim().replace(/\/+$/, '');
      if (!/^https?:\/\/[^\s]+$/i.test(value)) return json({ error: 'Not an http(s) address.' }, 400);

      const pool = await readPool(env);
      const now = Date.now();
      const known = pool.find((entry) => entry.url === value);
      if (known) {
        known.seen = now;
        // The first join is kept: a machine that re-joins every two minutes is not
        // newer than the notebook that started after it.
        if (!known.joined) known.joined = known.seen || now;
        if (body.note) known.note = String(body.note).slice(0, 80);
      } else {
        pool.unshift({ url: value, seen: now, joined: now, note: String(body.note || '').slice(0, 80) });
      }
      await writePool(env, pool);
      return json({ ok: true, url: value, count: pool.length, pool: pool.map((entry) => entry.url) });
    }

    if (url.pathname === '/remove' && request.method === 'POST') {
      const allowed = authorised(request, env);
      if (!allowed.ok) return json({ error: allowed.error }, allowed.status);
      let body = {};
      try {
        body = await request.json();
      } catch (err) {
        return json({ error: 'Expected JSON: {"url": "https://..."}' }, 400);
      }
      const value = String((body && body.url) || '').trim().replace(/\/+$/, '');
      const pool = (await readPool(env)).filter((entry) => entry.url !== value);
      await writePool(env, pool);
      return json({ ok: true, removed: value, count: pool.length, pool: pool.map((entry) => entry.url) });
    }

    return json({ error: 'GET /pool, GET /pick, POST /join, POST /remove, GET /health' }, 404);
  },
};
