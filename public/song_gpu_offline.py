"""Boot the song stack on a Kaggle GPU with no internet at all.

Kaggle disables the network on every kernel pushed through its API - the
`enableInternet` field is ignored, which is a year-old open feature request - so the
usual boot (git clone, pip install, weight download) can never work there. This
script is the other way round: everything it needs is mounted from dataset sources
before the kernel starts, and nothing touches the network.

What has to be mounted (both created by tools\\kaggle_pack.ps1):

    /kaggle/input/<code dataset>/repo        this repository, no .git, no models
    /kaggle/input/<code dataset>/wheels      every dependency as a Linux wheel
    /kaggle/input/<code dataset>/ace-step    ACE-Step source, for --no-deps install
    /kaggle/input/<code dataset>/cloudflared-linux-amd64
    /kaggle/input/<weights dataset>/ACE-Step-v1-3.5B
    /kaggle/input/<weights dataset>/Qwen2.5-1.5B-Instruct

The kernel text the site pushes is a dozen lines that find this file and run it, so
the pushed code never changes when this one does.
"""

import os
import re
import subprocess
import sys
import time
import urllib.request

WORK = '/kaggle/working' if os.path.isdir('/kaggle/working') else '.'
PORT = int(os.environ.get('SONG_PORT') or 8090)
VOICE_PORT = 8095


def say(*parts):
    print(' '.join(str(p) for p in parts), flush=True)


def mount(name):
    """The first mounted path that ends with this name, or ''."""
    root = '/kaggle/input'
    try:
        for entry in sorted(os.listdir(root)):
            for inner in (entry, os.path.join(entry, entry.replace('-', '_'))):
                candidate = os.path.join(root, inner)
                if os.path.isdir(candidate):
                    for item in sorted(os.listdir(candidate)):
                        if item.lower() == name.lower():
                            return os.path.join(candidate, item)
    except OSError:
        pass
    direct = os.path.join(root, name)
    return direct if os.path.exists(direct) else ''


CODE = os.environ.get('SONG_CODE') or mount('repo')
if not CODE or not os.path.isdir(CODE):
    raise SystemExit('no mounted repository: expected /kaggle/input/<code dataset>/repo. '
                     'Run tools\\kaggle_pack.ps1 and attach song-gpu-code to the kernel.')
REPO = os.path.dirname(CODE) if os.path.basename(CODE) == 'repo' else CODE
SERVICE = os.path.join(REPO, 'song_service')
say('repo:', CODE)

# --- no network, by design --------------------------------------------------
# Said out loud because the previous failure was a DNS error 30 seconds in, which
# says nothing about what is wrong.
try:
    import socket
    socket.create_connection(('pypi.org', 443), timeout=5).close()
    say('note: this kernel does have internet; it is still using only what is mounted')
except Exception:
    say('no internet in this kernel, which is expected here: everything comes from /kaggle/input')

# --- the packages -----------------------------------------------------------
# Not a pip install: the dependencies are already unpacked into a directory beside
# the repo by tools\kaggle_pack.ps1, built for Linux and for this Python, so the
# kernel only has to put them on sys.path. A pip run would need a local wheel index
# and would still have to compile anything without a binary; this needs neither.
PACKED = os.path.dirname(CODE)
PYLIBS = os.path.join(PACKED, 'pylibs')
if os.path.isdir(PYLIBS):
    sys.path.insert(0, PYLIBS)
    say('packages:', PYLIBS, '(' + str(len(os.listdir(PYLIBS))) + ' entries)')
else:
    say('no pylibs beside the repo:', PYLIBS, '- it will use only what Kaggle ships')

ace = os.path.join(PACKED, 'ace-step')
if os.path.isdir(ace):
    sys.path.insert(0, ace)
    say('ACE-Step source:', ace)
else:
    raise SystemExit('no ACE-Step source beside the repo: ' + ace)

# --- the weights ------------------------------------------------------------
weights = mount('ACE-Step-v1-3.5B')
if not weights:
    raise SystemExit('no mounted ACE-Step weights: expected '
                     '/kaggle/input/<weights dataset>/ACE-Step-v1-3.5B')
os.environ['ACE_CHECKPOINT'] = weights
lyrics = mount('Qwen2.5-1.5B-Instruct')
if lyrics:
    # transformers reads this directory instead of reaching for the hub.
    os.environ['SONG_LYRICS_MODEL'] = lyrics
    os.environ['HF_HOME'] = os.path.join(WORK, 'hf')
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    os.environ['HF_HUB_OFFLINE'] = '1'
say('weights:', weights)

# --- the service ------------------------------------------------------------
os.environ.update(SONG_PORT=str(PORT), SONG_HOST='0.0.0.0', SONG_WARM='1')
log = open(os.path.join(WORK, 'song.log'), 'w')
proc = subprocess.Popen([sys.executable, 'app.py', '--role', 'song'], cwd=SERVICE,
                        stdout=log, stderr=subprocess.STDOUT)
health = ''
for attempt in range(36):
    try:
        with urllib.request.urlopen('http://127.0.0.1:%d/health' % PORT, timeout=5) as answer:
            health = answer.read().decode()
        break
    except Exception:
        if attempt and attempt % 6 == 0:
            say('  waiting for /health ...', str(attempt * 5) + 's')
        time.sleep(5)
if not health:
    say('the service did not answer within 3 minutes. End of song.log:')
    try:
        say(open(os.path.join(WORK, 'song.log')).read()[-2500:])
    except OSError:
        pass
    raise SystemExit(2)
say('service:', health[:200])

# --- the voice service ------------------------------------------------------
voice_log = open(os.path.join(WORK, 'voice.log'), 'w')
subprocess.Popen([sys.executable, 'app.py'], cwd=os.path.join(REPO, 'voice_service'),
                 stdout=voice_log, stderr=subprocess.STDOUT,
                 env=dict(os.environ, VOICE_PORT=str(VOICE_PORT)))

# --- the tunnel -------------------------------------------------------------
binary = os.path.join(PACKED, 'cloudflared-linux-amd64')
if not os.path.isfile(binary):
    raise SystemExit('no cloudflared binary next to the repo: ' + binary)
os.chmod(binary, 0o755)
tunnel_log = os.path.join(WORK, 'tunnel.log')
open(tunnel_log, 'w').close()
with open(tunnel_log, 'w') as handle:
    subprocess.Popen([binary, 'tunnel', '--no-autoupdate', '--url',
                      'http://127.0.0.1:%d' % PORT],
                     stdout=handle, stderr=subprocess.STDOUT)

url = ''
for _ in range(40):
    time.sleep(3)
    try:
        found = re.findall(r'https://[a-z0-9-]+[.]trycloudflare[.]com', open(tunnel_log).read())
    except OSError:
        found = []
    if found:
        url = found[-1]
        try:
            with urllib.request.urlopen(url + '/health', timeout=8) as answer:
                if answer.status == 200:
                    break
        except Exception:
            url = ''
if not url:
    say('the tunnel did not come up; the service is still running on', PORT)
    raise SystemExit(3)
say('tunnel:', url)

# --- join the pool ----------------------------------------------------------
join_key = (os.environ.get('SONG_JOIN_KEY') or '').strip()
if not join_key:
    say('no join key in this kernel, so the card is not in the pool: use', url)
else:
    joined = False
    for attempt in range(10):
        try:
            with urllib.request.urlopen(url + '/health', timeout=10):
                pass
            payload = ('{"url":"' + url + '"}').encode()
            for target, header in (
                ('https://browser.bpiersing.workers.dev/api/song/endpoint', 'x-song-key'),
                ('https://song-pool.bpiersing.workers.dev/join', 'x-join-key'),
            ):
                request = urllib.request.Request(target, data=payload, headers={
                    'content-type': 'application/json', header: join_key})
                try:
                    with urllib.request.urlopen(request, timeout=20) as answer:
                        say('joined', target.split('/')[2], answer.status)
                except Exception as exc:
                    say('could not join', target.split('/')[2], exc)
            joined = True
            break
        except Exception as exc:
            say('  the tunnel is not answering yet:', exc)
            time.sleep(10)
    if not joined:
        say('the card never answered through the tunnel, so the pool was not told about it')

say('ready: the service is on', PORT, 'behind', url)
# The kernel has to stay alive for the tunnel and the service to keep serving.
while True:
    time.sleep(60)
    if proc.poll() is not None:
        say('the song service exited; nothing left to serve')
        break
