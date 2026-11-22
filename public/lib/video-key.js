// The one definition of "the same video".
//
// Three sides need this answer: the Worker (is this video already in the library?), the
// page (are these two results one card?) and the exporter (which videos did I just ship?).
// It used to be written three times and the copies drifted: the first version dropped the
// query string, and every app on Google Play and every YouTube Music playlist shares a path
// and differs only there, so dozens of unrelated videos became one key - which merged
// results, skipped real discoveries and made the library count too low.
//
// So: this file, no copies. worker.js imports it (bundled by wrangler), the page imports it
// as a module, and tools/video_key_cli.mjs runs it for the Python exporter.
//
// A key is:
//   y:<id>  a YouTube video, however it is linked (watch?v=, youtu.be/, /shorts/, /embed/)
//   p:<id>  a playlist, by its list id
//   d:<id>  a Dailymotion video (/video/ or /embed/video/)
//   v:<id>  a Vimeo video
//   u:<host><path>[?<query>]  anything else, with tracking parameters removed
export const TRACKING_PARAMS = new Set([
  'si', 'feature', 'pp', 't', 'fbclid', 'gclid', 'igshid', 'ref', 'ref_src',
]);

function queryKey(url) {
  const kept = [];
  for (const [name, value] of url.searchParams.entries()) {
    const lowered = String(name).toLowerCase();
    if (!lowered || TRACKING_PARAMS.has(lowered) || lowered.startsWith('utm_')) continue;
    kept.push(`${name}=${value}`);
  }
  return kept.length ? '?' + kept.sort().join('&') : '';
}

export function videoKey(link) {
  const text = String(link == null ? '' : link).trim();
  if (!text) return '';
  let url;
  try {
    url = new URL(text);
  } catch (err) {
    return text.toLowerCase();
  }
  const host = url.hostname.replace(/^www\./, '').toLowerCase();
  const path = url.pathname || '';

  if (host === 'youtu.be' || /(^|\.)youtube\.com$/.test(host)) {
    let id = '';
    if (host === 'youtu.be') {
      id = path.replace(/^\/+/, '').split('/')[0] || '';
    } else if (path === '/watch') {
      id = url.searchParams.get('v') || '';
    } else if (path === '/playlist') {
      // A playlist is not the page: every playlist in YouTube Music shares the path.
      const list = url.searchParams.get('list') || '';
      if (list) return 'p:' + list;
    } else {
      const match = path.match(/^\/(?:shorts|embed|live)\/([A-Za-z0-9_-]{11})/);
      id = match ? match[1] : '';
    }
    if (id) return 'y:' + id;
  }
  if (/(^|\.)dailymotion\.com$/.test(host)) {
    const match = path.match(/\/(?:embed\/)?video\/([A-Za-z0-9]+)/);
    if (match) return 'd:' + match[1];
  }
  if (/(^|\.)vimeo\.com$/.test(host)) {
    const match = path.match(/(\d{6,})/);
    if (match) return 'v:' + match[1];
  }
  // The path alone is not an identity: every app on Google Play and every YouTube playlist
  // shares its path and differs in the query.
  return 'u:' + (host + path).replace(/\/+$/, '').toLowerCase() + queryKey(url);
}
