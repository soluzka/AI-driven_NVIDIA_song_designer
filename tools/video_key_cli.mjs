// Runs the one video key over links given as JSON on stdin, so the Python exporter uses the
// same definition as the page and the Worker instead of a copy that can drift.
//
//   echo '["https://youtu.be/abc"]' | node tools/video_key_cli.mjs
//   ["y:abc"]
import { videoKey } from '../public/lib/video-key.js';

let text = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => { text += chunk; });
process.stdin.on('end', () => {
  let links = [];
  try {
    links = JSON.parse(text || '[]');
  } catch (err) {
    process.stderr.write('Expected a JSON array of links on stdin.\n');
    process.exit(1);
  }
  if (!Array.isArray(links)) links = [];
  process.stdout.write(JSON.stringify(links.map(videoKey)));
});
