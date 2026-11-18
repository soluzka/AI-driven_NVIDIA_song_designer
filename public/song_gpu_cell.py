# One cell for Colab, Kaggle, or any Linux GPU box. Paste this whole file into a
# new notebook cell and run it (Kaggle: a new notebook, then paste and run).
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

# Colab keeps its session disk at /content, Kaggle uses /kaggle/working, and a
# plain Linux GPU box uses whatever directory this was started in. Everything is
# written under WORK, so the same file works on all three without editing paths.
WORK = '/content' if os.path.isdir('/content') and os.access('/content', os.W_OK) else os.getcwd()
REPO = os.path.join(WORK, 'internet')
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

try:
    import torch  # noqa: E402 - after the first print, so a slow import cannot hide it
except ModuleNotFoundError:
    # Colab and Kaggle ship a CUDA build, a bare Linux box may not have one at all.
    # The default Linux wheels bundle the CUDA libraries, so installing them here
    # is what gets the model onto the card instead of onto the processor.
    say('torch is not installed on this runtime; installing the default CUDA build ...')
    run([sys.executable, '-m', 'pip', 'install', '-q', 'torch', 'torchaudio', 'torchvision'])
    import torch  # noqa: E402
# A torch that is already here is left exactly as it is: swapping it for another
# wheel is how a working CUDA build turns into a CPU one.
say('torch', torch.__version__, 'cuda', torch.cuda.is_available())
if not torch.cuda.is_available():
    say('warning: no CUDA. The model still runs, on the processor: hours per song.')

# --- the code and its packages ---------------------------------------------
say()
say('1/5 system packages ...')
run(['apt-get', '-qq', 'install', '-y', 'ffmpeg', 'libsndfile1', 'git'], quiet=True)

say('2/5 the code ...')
GIT_URL = 'https://github.com/soluzka/AI-driven_NVIDIA_song_designer.git'
if os.path.isdir(os.path.join(REPO, '.git')):
    # A folder left from an earlier run in this session is code from that run.
    # Reusing it silently means the pins here and the packages there disagree, so
    # it is moved to main every time and the commit is printed.
    run(['git', '-C', REPO, 'fetch', '--depth', '1', 'origin', 'main'], quiet=True)
    run(['git', '-C', REPO, 'reset', '--hard', 'origin/main'], quiet=True)
    say('updated', REPO)
else:
    run(['git', 'clone', '--depth', '1', GIT_URL, REPO])
try:
    commit = subprocess.run(['git', '-C', REPO, 'rev-parse', '--short', 'HEAD'],
                            capture_output=True, text=True).stdout.strip()
    say('code at commit', commit or 'unknown')
except Exception:
    pass

say('3/5 the packages (3-6 minutes; pip prints nothing until it finishes) ...')
pip = [sys.executable, '-m', 'pip', 'install', '-q']
# The list is the repository's own song_service/requirements.txt, the one the Windows
# installer uses, so it carries every package ACE-Step imports (loguru, cutlet,
# fugashi, num2words, pypinyin, hangul-romanize, peft, accelerate, diffusers, ...)
# and the pins that matter: transformers 4.57.6 with huggingface-hub 0.36.2,
# diffusers/gradio capped to what that hub allows, py3langid 0.2.2 for the
# segmenter. Only the torch lines are skipped: they name the +cu126 wheels from a
# Windows-only index, and Colab/Kaggle already have a CUDA build.
SKIP = ('torch', 'torchaudio', 'torchvision')
specs = []
for line in open(os.path.join(SERVICE, 'requirements.txt')):
    line = line.split('#')[0].strip()
    name = re.split(r'[=<>!\[ ]', line, 1)[0].strip().lower() if line else ''
    if name and name not in SKIP:
        specs.append(line)
# ACE-Step's metadata pins transformers==4.50.0 against the 4.57.6 the lyric model
# needs, so its code is installed without its dependency list.
run(pip + specs + ['--no-deps', 'git+https://github.com/ace-step/ACE-Step.git'])

# The voice (chatterbox) is part of the work this card does: the song service calls
# it over loopback on 8095, exactly as the Windows install runs it, so the public
# tunnel keeps pointing at 8090 alone and there is no second public door. --no-deps
# for the reason its own file documents: its metadata pins torch 2.6 and numpy 1.x,
# which would replace this runtime's CUDA torch. Everything it imports is listed
# there, so --no-deps loses nothing.
say('  the voice (chatterbox) ...')
voice_specs = [line.split('#')[0].strip()
               for line in open(os.path.join(REPO, 'voice_service', 'requirements-shared.txt'))
               if line.split('#')[0].strip()]
run(pip + ['--no-deps'] + voice_specs)

say('4/5 checking the imports ...')
try:
    from acestep.pipeline_ace_step import ACEStepPipeline  # noqa: E402,F401
except ModuleNotFoundError as missing:
    say('missing package:', missing.name)
    say('install it with:', sys.executable, '-m pip install', missing.name)
    raise
say('packages in place')

# --- the weights ------------------------------------------------------------
say()
say('5/5 the weights: about 10 GB, once per session (bars below are the progress)')
os.environ['HF_HOME'] = os.path.join(WORK, 'hf')
os.environ['HF_HUB_ENABLE_HF_TRANSFER'] = '1'
run([sys.executable, 'song_service/install.py', '--weights'], cwd=REPO)

# --- the service ------------------------------------------------------------
say()
say('starting the service ...')
# A cell that is re-run after a package fix must replace the process that is
# already holding the old modules. The command line here is "python app.py", so the
# match is on that, not on a path that never appears in it.
killed = subprocess.run(['pkill', '-f', 'app.py'], capture_output=True, text=True)
if killed.returncode == 0:
    say('stopped the services from the previous run so the installed packages load')
    time.sleep(2)
try:
    version = subprocess.run([sys.executable, '-c',
                              'import importlib.metadata as m; print(m.version("py3langid"))'],
                             capture_output=True, text=True).stdout.strip()
    say('py3langid', version or 'unknown', '(ACE-Step needs 0.2.2)')
except Exception:
    pass
os.environ.update(SONG_PORT=str(PORT), SONG_HOST='0.0.0.0', SONG_WARM='1')
SONG_LOG = os.path.join(WORK, 'song.log')
log = open(SONG_LOG, 'w')
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
    say('the service did not answer within 3 minutes. End of ' + SONG_LOG + ':')
    say(open(SONG_LOG).read()[-2000:])
    raise SystemExit('stopped before the tunnel: the service is not running')

# --- the voice service ------------------------------------------------------
say()
say('starting the voice service on 8095 ...')
VOICE_PORT_N = 8095
os.environ['VOICE_PORT'] = str(VOICE_PORT_N)
VOICE_LOG = os.path.join(WORK, 'voice.log')
voice_log = open(VOICE_LOG, 'w')
voice_proc = subprocess.Popen([sys.executable, 'app.py'],
                              cwd=os.path.join(REPO, 'voice_service'),
                              stdout=voice_log, stderr=subprocess.STDOUT)
# /health answers before the model loads - torch is imported on the first sing - so
# this only proves the port is up; the song service takes it from there, because
# song_service reads VOICE_SERVICE (default 127.0.0.1:8095) over loopback.
voice_health = ''
for attempt in range(24):
    try:
        with urllib.request.urlopen('http://127.0.0.1:%d/health' % VOICE_PORT_N,
                                    timeout=5) as answer:
            voice_health = answer.read().decode()
        break
    except Exception:
        if attempt and attempt % 4 == 0:
            say('  waiting for the voice ...', str(attempt * 5) + 's')
        time.sleep(5)
if voice_health:
    say('voice:', voice_health[:200])
else:
    say('the voice service did not answer within 2 minutes. Last lines of ' + VOICE_LOG + ':')
    try:
        say(open(VOICE_LOG).read()[-1500:])
    except Exception:
        pass

# --- the tunnel -------------------------------------------------------------
say()


def tunnel_answers(address, timeout=8):
    """True when the model service behind that address replies to /health."""
    try:
        with urllib.request.urlopen(address.rstrip('/') + '/health', timeout=timeout) as reply:
            return reply.status == 200
    except Exception:
        return False


def logged_url(log_path):
    """The last address written to the tunnel log, answering or not."""
    try:
        found = re.findall(r'https://[a-z0-9-]+[.]trycloudflare[.]com', open(log_path).read())
    except FileNotFoundError:
        return ''
    return found[-1] if found else ''


binary = os.path.join(WORK, 'cloudflared')
TUNNEL_LOG = os.path.join(WORK, 'tunnel.log')
# WORK survives between runs inside one session, so the log can still hold an
# address whose tunnel died with the run before it. A saved name is reused only
# while it answers, and a new tunnel starts from an empty log: printing a dead
# address is what makes the site show a red dot.
url = ''
saved = logged_url(TUNNEL_LOG)
if saved and tunnel_answers(saved):
    url = saved
    say('the tunnel already running in this session answers, so there is nothing to wait for')
else:
    if saved:
        say(saved, 'is the last address in the log, and it does not answer any more.')
        say('starting a new tunnel: a trycloudflare name stops when its process stops')
    say('downloading cloudflared (40 MB, silent while it downloads) ...')
    if not os.path.isfile(binary):
        urllib.request.urlretrieve(
            'https://github.com/cloudflare/cloudflared/releases/download/2026.10.7/cloudflared-linux-amd64',
            binary)
        os.chmod(binary, 0o755)
    say('starting the tunnel ...')
    try:
        open(TUNNEL_LOG, 'w').close()
    except OSError:
        pass
    subprocess.Popen([binary, 'tunnel', '--no-autoupdate', '--url',
                      'http://127.0.0.1:%d' % PORT, '--logfile', TUNNEL_LOG],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for attempt in range(30):
        url = logged_url(TUNNEL_LOG)
        if url:
            break
        if attempt and attempt % 5 == 0:
            say('  still waiting for a name ...', str(attempt * 2) + 's')
        time.sleep(2)
    if url:
        # A brand new name takes a few seconds to route, so it is probed before
        # being handed over.
        for attempt in range(12):
            if tunnel_answers(url):
                break
            time.sleep(5)

say()
say('=' * 70)
if url and not tunnel_answers(url):
    say('the tunnel got', url, 'but /health does not answer through it,')
    say('so it is not offered: the site would only show a red dot.')
    raise SystemExit('tunnel address does not answer')
if not url:
    say('cloudflared did not produce a name. End of ' + TUNNEL_LOG + ':')
    try:
        say(open(TUNNEL_LOG).read()[-1500:])
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
