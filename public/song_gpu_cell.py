# One cell for Colab. Paste this whole file into a new notebook cell and run it.
#
# It does what the .ipynb does, without a notebook file to load, so nothing can be
# lost between GitHub, the notebook importer and the runtime: every line is a
# print, and the first one appears immediately. That first line is the point - if
# even it does not appear, the runtime is not attached and no code is running.
#
# Put the site owner's join key between the quotes below to add this card to the
# pool. Leave it empty and the address it prints is used with Connect my model.
JOIN_KEY = ''

import json
import os
import re
import subprocess
import sys
import time
import urllib.request

REPO = '/content/internet'
SERVICE = os.path.join(REPO, 'song_service')
PORT = 8090


def say(*parts):
    print(*parts, flush=True)


def run(command, cwd=None, quiet=False):
    say('+', ' '.join(command))
    return subprocess.run(
        command, cwd=cwd,
        stdout=subprocess.DEVNULL if quiet else None,
        stderr=subprocess.DEVNULL if quiet else None,
    )


say('cell is running. python', sys.version.split()[0])
say('this line appearing means the runtime is attached and code is executing')

# --- the card ---------------------------------------------------------------
try:
    probe = subprocess.run(
        ['nvidia-smi', '--query-gpu=name,memory.total', '--format=csv,noheader'],
        capture_output=True, text=True)
    say('gpu:', (probe.stdout or probe.stderr).strip()[:300] or 'none reported')
except FileNotFoundError:
    say('gpu: no nvidia-smi on this runtime (that means a CPU runtime)')

import torch  # noqa: E402 - after the first print, so a slow import cannot hide it
say('torch', torch.__version__, 'cuda', torch.cuda.is_available())
if not torch.cuda.is_available():
    say('warning: no CUDA. The model still runs, on the processor: hours per song.')

# --- the code and its packages ---------------------------------------------
say()
say('1/5 system packages ...')
run(['apt-get', '-qq', 'install', '-y', 'ffmpeg', 'libsndfile1', 'git'], quiet=True)

say('2/5 the code ...')
if os.path.isdir(SERVICE):
    say('already at', REPO)
else:
    run(['git', 'clone', '--depth', '1',
         'https://github.com/soluzka/AI-driven_NVIDIA_song_designer.git', REPO])

say('3/5 the packages (3-6 minutes; pip prints nothing until it finishes) ...')
pip = [sys.executable, '-m', 'pip', 'install', '-q']
run(pip + ['flask', 'flask-cors', 'soundfile', 'librosa', 'scipy', 'demucs',
           'hf_transfer', 'spacy'])
run(pip + ['transformers==4.57.6', 'huggingface-hub==0.36.2', 'numpy==2.4.6'])
# ACE-Step's metadata pins transformers==4.50.0 against the 4.57.6 the lyric model
# needs, so its code is installed without its dependency list.
run(pip + ['--no-deps', 'git+https://github.com/ace-step/ACE-Step.git'])

say('4/5 checking the imports ...')
from acestep.pipeline_ace_step import ACEStepPipeline  # noqa: E402,F401
say('packages in place')

# --- the weights ------------------------------------------------------------
say()
say('5/5 the weights: about 10 GB, once per session (bars below are the progress)')
os.environ['HF_HOME'] = '/content/hf'
os.environ['HF_HUB_ENABLE_HF_TRANSFER'] = '1'
run([sys.executable, 'song_service/install.py', '--weights'], cwd=REPO)

# --- the service ------------------------------------------------------------
say()
say('starting the service ...')
os.environ.update(SONG_PORT=str(PORT), SONG_HOST='0.0.0.0', SONG_WARM='1')
log = open('/content/song.log', 'w')
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
if health:
    say('service:', health[:300])
else:
    say('the service did not answer within 3 minutes. End of /content/song.log:')
    say(open('/content/song.log').read()[-2000:])
    raise SystemExit('stopped before the tunnel: the service is not running')

# --- the tunnel -------------------------------------------------------------
say()
say('downloading cloudflared (40 MB, silent while it downloads) ...')
binary = '/usr/local/bin/cloudflared'
if not os.path.isfile(binary):
    urllib.request.urlretrieve(
        'https://github.com/cloudflare/cloudflared/releases/download/2026.10.7/cloudflared-linux-amd64',
        binary)
    os.chmod(binary, 0o755)
say('starting the tunnel ...')
subprocess.Popen([binary, 'tunnel', '--no-autoupdate', '--url',
                  'http://127.0.0.1:%d' % PORT, '--logfile', '/content/tunnel.log'],
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
url = ''
for attempt in range(30):
    try:
        found = re.findall(r'https://[a-z0-9-]+[.]trycloudflare[.]com',
                           open('/content/tunnel.log').read())
        if found:
            url = found[-1]
            break
    except FileNotFoundError:
        pass
    if attempt and attempt % 5 == 0:
        say('  still waiting for a name ...', str(attempt * 2) + 's')
    time.sleep(2)

say()
say('=' * 70)
if not url:
    say('cloudflared did not produce a name. End of /content/tunnel.log:')
    try:
        say(open('/content/tunnel.log').read()[-1500:])
    except Exception as exc:
        say('no log at all:', exc)
    raise SystemExit('no tunnel address')

say('ADDRESS:', url)
say('(that line is the whole address; it stops working when this session ends)')
say('=' * 70)
say()
say('On the site: press Connect my model in the header and paste that address.')
say('This card is reachable while this cell and session stay running.')

if JOIN_KEY.strip():
    body = json.dumps({'url': url, 'note': 'colab t4'}).encode()
    request = urllib.request.Request(
        'https://song-pool.bpiersing.workers.dev/join', data=body,
        headers={'content-type': 'application/json', 'x-join-key': JOIN_KEY.strip()})
    try:
        with urllib.request.urlopen(request, timeout=20) as answer:
            say('pool:', answer.read().decode())
        say('This card is in the pool until the notebook stops. Keep the tab open.')
    except Exception as exc:
        say('join refused:', exc)
        say('Use the address above with Connect my model instead.')
else:
    say('No JOIN_KEY set, so this card is not in the pool: use the address above.')
