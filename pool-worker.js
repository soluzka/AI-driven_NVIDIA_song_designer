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
  // The register lives in D1 when it is bound: one row per machine, so a re-join is an
  // upsert and a read is a SELECT. KV's 1,000 writes a day could not carry a card that
  // re-joins every two minutes, and when they ran out the pool answered 0 GPUs for a
  // machine that was running and serving. KV is still read when D1 is not bound.
  if (env.REGISTER) {
    try {
      const cutoff = Date.now() - POOL_TTL_MS;
      const { results } = await env.REGISTER
        .prepare('SELECT url, seen, joined, note FROM machines WHERE seen > ? ORDER BY seen DESC')
        .bind(cutoff).all();
      return (results || []).map((row) => ({
        url: row.url, seen: Number(row.seen) || 0,
        joined: Number(row.joined) || 0, note: row.note || '',
      }));
    } catch (err) {
      lastWriteError = String((err && err.message) || err);
      return [];
    }
  }
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
  if (env.REGISTER) {
    // No cap on the D1 register: one row per machine, so the sixteenth card is no more
    // expensive than the first. The old 16 was a KV value size, and a pool that silently
    // stopped accepting the seventeenth machine is exactly the failure this is meant to
    // end. Dead rows still leave by the TTL below.
    const trimmed = pool;
    try {
      // The list passed in is the whole register, so the table is replaced with it: a row
      // that is no longer in the list is gone. Upserting alone used to leave a removed
      // machine listed for a week, because the only delete was the TTL.
      const statements = [env.REGISTER.prepare('DELETE FROM machines')];
      for (const entry of trimmed) {
        statements.push(env.REGISTER
          .prepare('INSERT INTO machines (url, seen, joined, note) VALUES (?, ?, ?, ?) '
            + 'ON CONFLICT(url) DO UPDATE SET seen = excluded.seen, note = excluded.note')
          .bind(entry.url, Number(entry.seen) || Date.now(),
            Number(entry.joined || entry.seen) || Date.now(), String(entry.note || '')));
      }
      await env.REGISTER.batch(statements);
      return true;
    } catch (err) {
      lastWriteError = String((err && err.message) || err);
      return false;
    }
  }
  // KV has a daily write allowance. Hitting it must not take the pool down: the list this
  // isolate already holds is still good for routing, and the next write that fits saves it.
  try {
    await env.POOL.put(POOL_KEY, JSON.stringify(pool.slice(0, POOL_LIMIT)));
    return true;
  } catch (err) {
    lastWriteError = String((err && err.message) || err);
    return false;
  }
}

// Kept so a refusal can be read back instead of guessed at: an exhausted daily write
// allowance and a broken binding look identical from "the join said ok and the pool is
// still empty".
let lastWriteError = '';

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
// Two misses in a row, not one: a tunnel can time out for a second, and taking the card
// out of routing on that alone made the page jump to the other GPU - or back to the PC.
// The clock starts again after this long, so two failures an hour apart are not a pair.
const POOL_STRIKE_MS = 90 * 1000;

// A card that keeps missing is probed with a short timeout for a minute, so one dead
// address cannot cost every reader three twelve-second waits - that latency is what the
// page reports as "the pool is offline". Memory only: nothing here is ever written.
const poolCooled = new Map();
const POOL_COOLDOWN_MS = 60 * 1000;

// A card that has stopped answering is filtered out of every answer, but its row used to
// stay in the register for the whole seven-day TTL: still probed on every read, still
// counted by anything that reads the register, and still a URL a song could be sent to.
// Writing on every poll is what exhausted the write allowance once, so dead rows are
// deleted rarely - at most once every ten minutes, and only when the probe really found
// a card gone. That keeps dead addresses out of the pool at almost no write cost.
const PRUNE_EVERY_MS = 10 * 60 * 1000;
let prunedAt = 0;

async function livingPool(env) {
  const pool = await readPool(env);
  if (!pool.length) return pool;
  const opened = Date.now();
  const answered = await Promise.allSettled(
    pool.map((entry) => answers(entry.url, poolCooled.get(entry.url) > opened ? 2500 : 8000)));
  const alive = pool.map((entry, index) => answered[index].status === 'fulfilled' && answered[index].value === true);
  // A tunnel that has just timed out is still closing that stream, so a retry against it
  // straight away failed on the same dead connection. Give the name a moment, then try
  // twice more before a card is even counted as having missed.
  for (const wait of [1500, 3000]) {
    const pending = pool.filter((entry, index) => !alive[index]);
    if (!pending.length) break;
    await new Promise((resolve) => setTimeout(resolve, wait));
    const again = await Promise.allSettled(pending.map((entry) => answers(
      entry.url, poolCooled.get(entry.url) > Date.now() ? 2500 : 12000)));
    let at = 0;
    pool.forEach((entry, index) => {
      if (alive[index]) return;
      alive[index] = again[at] && again[at].status === 'fulfilled' && again[at].value === true;
      at += 1;
    });
  }

  // Nothing about a probe result is written any more. A dead card made every single poll
  // dirty, so a page polling /pool burned the whole 1,000-write daily allowance in
  // minutes; once writing failed the dead entries could never leave the list, and every
  // read paid three dead probes - which is what "the pool went offline" looked like.
  // The stored list changes only when someone joins or leaves; a card that stops
  // answering is filtered out of the answer here, in memory.
  const now = Date.now();
  const living = [];
  pool.forEach((entry, index) => {
    if (alive[index]) {
      entry.ok_at = now;
      entry.fail = 0;
      entry.fail_at = 0;
      poolCooled.delete(entry.url);
      living.push(entry);
      return;
    }
    const sinceFail = now - Number(entry.fail_at || 0);
    const strikes = sinceFail < POOL_STRIKE_MS ? Number(entry.fail || 0) + 1 : 1;
    entry.fail = strikes;
    entry.fail_at = now;
    if (strikes >= 2) poolCooled.set(entry.url, now + POOL_COOLDOWN_MS);
    // The grace belongs to a card that answered recently, not to one that has been
    // silent for days: that one is out of the answer at once, so nobody waits on it.
    const last = Math.max(Number(entry.ok_at || 0), Number(entry.seen || 0));
    if (strikes < 2 && now - last < POOL_GRACE_MS) living.push(entry);
  });
  if (living.length < pool.length && Date.now() - prunedAt > PRUNE_EVERY_MS) {
    prunedAt = Date.now();
    await writePool(env, living);
  }
  return living;
}

// --- the two timestamps the site's own KV could not keep ---------------------
// gpu:started_at and gpu:joined_at are what the site's GPU endpoints measure against:
// the thirty-minute cooldown between starts, and the ninety-minute window that says a
// run is still booting. They lived in the site Worker's STATS namespace, whose daily
// write allowance runs out - and once it did, every announce answered 500, Start Kaggle
// answered error 1101, and the boot guard stuck for the full ninety minutes. This
// Worker's KV is a different namespace and kept answering, so they live here now.
//
// Reads are open like /pool - a timestamp is not a secret. Writes need the join key,
// because the site's Worker is the only thing that should move this clock.
// gpu:guard_at is the site's writability probe: the boot-window guard only enforces
// when a write actually lands, and before this key existed its probe was refused here
// and fell through to the site KV that is out of writes, so the guard switched itself
// off on every press.
const STATE_KEYS = new Set(['gpu:started_at', 'gpu:joined_at', 'gpu:guard_at', 'site:endpoint']);
const STATE_PREFIX = '/state/';

async function readState(env, key) {
  if (env.REGISTER) {
    try {
      const row = await env.REGISTER.prepare('SELECT value FROM state WHERE key = ?').bind(key).first();
      if (row) return String(row.value);
    } catch (err) {}
  }
  try {
    return await env.POOL.get(key);
  } catch (err) {
    return null;
  }
}

async function writeState(env, key, value) {
  if (env.REGISTER) {
    try {
      await env.REGISTER.prepare('INSERT INTO state (key, value) VALUES (?, ?) '
        + 'ON CONFLICT(key) DO UPDATE SET value = excluded.value')
        .bind(key, String(value)).run();
      return true;
    } catch (err) {
      lastWriteError = String((err && err.message) || err);
    }
  }
  try {
    await env.POOL.put(key, value);
    return true;
  } catch (err) {
    return false;
  }
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
      const entries = await readPool(env);
      // The raw register, not the living one: /pool answers what routing may use, and
      // without this there is no way to tell "never joined" from "joined and filtered"
      // - which is the whole difference between a card that is down and a pool that is
      // refusing to see it. URLs are already public in /pool, so nothing new leaks.
      return json({
        ok: true,
        pool: entries.length,
        raw: entries.map((entry) => ({ url: entry.url, seen: entry.seen, joined: entry.joined || 0 })),
      });
    }

    // Why a card that answers in a browser is filtered out of /pool. Read-only, and it
    // only asks an address the caller already names - no key, no secrets. This is the
    // endpoint that turns "the pool is red" into the actual status or error.
    if (url.pathname === '/probe') {
      const target = String(url.searchParams.get('url') || '').trim().replace(/\/+$/, '');
      if (!target) return json({ error: 'add ?url=https://host' }, 400);
      const started = Date.now();
      try {
        const answer = await fetch(target + '/health', {
          headers: { accept: 'application/json' },
          signal: AbortSignal.timeout(15000),
        });
        const text = await answer.text();
        return json({
          url: target, ok: answer.ok, status: answer.status, ms: Date.now() - started,
          body: text.slice(0, 300),
        });
      } catch (err) {
        return json({
          url: target, ok: false, ms: Date.now() - started,
          error: String((err && err.message) || err),
        });
      }
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

      const note = String(body.note || '').slice(0, 80);
      const pool = await readPool(env);
      const now = Date.now();
      // A restart gives the machine a new tunnel address, and the row it left behind is
      // a dead address listed next to the live one until a probe finds it - at most once
      // every ten minutes. The note names the machine, so rows carrying the same note and
      // a different address are dropped in this same write rather than left to the probe.
      const kept = note ? pool.filter((entry) => entry.url === value || entry.note !== note) : pool;
      const known = kept.find((entry) => entry.url === value);
      if (known) {
        known.seen = now;
        // The first join is kept: a machine that re-joins every two minutes is not
        // newer than the notebook that started after it.
        if (!known.joined) known.joined = known.seen || now;
        if (note) known.note = note;
      } else {
        kept.unshift({ url: value, seen: now, joined: now, note });
      }
      const saved = await writePool(env, kept);
      return json({
        ok: true, url: value, saved,
        error: saved ? undefined : (lastWriteError || 'the register refused the write'),
        count: kept.length, pool: kept.map((entry) => entry.url),
      });
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

    if (url.pathname.startsWith(STATE_PREFIX)) {
      const key = decodeURIComponent(url.pathname.slice(STATE_PREFIX.length));
      if (!STATE_KEYS.has(key)) return json({ error: 'No such state key: ' + key }, 404);
      if (request.method === 'GET') return json({ key, value: await readState(env, key) });
      if (request.method !== 'POST') return json({ error: 'GET or POST only' }, 405);
      const allowed = authorised(request, env);
      if (!allowed.ok) return json({ error: allowed.error }, allowed.status);
      let body = {};
      try {
        body = await request.json();
      } catch (err) {
        return json({ error: 'Expected JSON: {"value": "..."}' }, 400);
      }
      // 200, not 40: these keys hold timestamps, and one of them holds the address the
      // header names - a tunnel host is longer than 40 characters and was being cut
      // mid-hostname, which stores something that looks fine and resolves to nothing.
      const value = String((body && body.value) === undefined ? '' : (body && body.value)).slice(0, 200);
      if (!value) return json({ error: 'Expected JSON: {"value": "..."}' }, 400);
      if (!(await writeState(env, key, value))) {
        return json({ error: 'This store refused the write.' }, 503);
      }
      return json({ ok: true, key, value });
    }

    return json({ error: 'GET /pool, GET /pick, POST /join, POST /remove, GET|POST /state/<key>, GET /health' }, 404);
  },
};
