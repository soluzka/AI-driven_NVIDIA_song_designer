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

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request

WORK = '/kaggle/working' if os.path.isdir('/kaggle/working') else '.'
PORT = int(os.environ.get('SONG_PORT') or 8090)
VOICE_PORT = 8095


def say(*parts):
    print(' '.join(str(p) for p in parts), flush=True)


# A cancelled session keeps nothing but its Output, so the boot's own lines go there
# too: "your session was cancelled" with no reason is what a run leaves behind
# otherwise, and boot.log is then the only place the last thing it printed survives.
def tee_boot_log():
    try:
        handle = open(os.path.join(WORK, 'boot.log'), 'w', encoding='utf-8',
                      errors='replace', buffering=1)
    except OSError:
        return

    class Tee:
        def __init__(self, *streams):
            self.streams = streams

        def write(self, text):
            for stream in self.streams:
                try:
                    stream.write(text)
                except Exception:
                    pass
            return len(text)

        def flush(self):
            for stream in self.streams:
                try:
                    stream.flush()
                except Exception:
                    pass

    sys.stdout = Tee(sys.__stdout__, handle)
    sys.stderr = Tee(sys.__stderr__, handle)


tee_boot_log()


# The two endpoints that keep a list of cards: the site's own, and the pool Worker it
# reads. Both are told, because a machine missing from either is a machine the site
# will not use.
POOL_TARGETS = (
    ('https://browser.bpiersing.workers.dev/api/song/endpoint', 'x-song-key'),
    ('https://song-pool.bpiersing.workers.dev/join', 'x-join-key'),
)


def tell_pool(url, join_key):
    """Tell the pool and the site that this card is behind `url`.

    Returns True when the pool Worker took the address. Cloudflare answers a bare
    urllib request with 403 "error code: 1010" before the key is looked at, and urllib
    prints only the status, so that block reads exactly like a wrong key - hence the
    browser-shaped user-agent.
    """
    payload = ('{"url":"' + url + '"}').encode()
    told = False
    for target, header in POOL_TARGETS:
        request = urllib.request.Request(target, data=payload, headers={
            'content-type': 'application/json',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                          'AppleWebKit/537.36 (KHTML, like Gecko) '
                          'Chrome/131.0.0.0 Safari/537.36',
            header: join_key})
        try:
            with urllib.request.urlopen(request, timeout=20) as answer:
                say('joined', target.split('/')[2], answer.status)
                if target.endswith('/join'):
                    told = True
        except Exception as exc:
            say('could not join', target.split('/')[2], exc)
    return told


# A boot joins once, so a machine the pool drops - a tunnel that hiccuped, a Worker
# that was redeployed while the session slept - stayed off the list until the kernel
# was restarted, with the card still warm and serving. Re-posting the same address
# costs one request every couple of minutes and puts it back by itself.
def keep_joined(url, join_key, every=120):
    def loop():
        while True:
            time.sleep(every)
            if not tell_pool(url, join_key):
                say('the pool did not take the address this time; trying again in',
                    every, 'seconds')

    threading.Thread(target=loop, name='keep-joined', daemon=True).start()
    say('the pool is told this address every', every, 'seconds, so a drop is repaired')


def health_summary(state):
    """One line saying whether the model is loaded, and on which card.

    /health is a whole document, and the log used to cut it at 200 characters - which
    stopped before "loaded", so a run could not be judged from its own log.
    """
    if not state:
        return 'no readable /health JSON'
    loaded = 'loaded=' + str(bool(state.get('loaded')))
    if state.get('warming'):
        loaded += ' (warming, ' + str(state.get('warming_seconds') or 0) + 's)'
    if state.get('warm_failed'):
        loaded += ' (the warm failed)'
    card = 'device=' + str(state.get('device')) + ' gpu=' + str(state.get('gpu'))
    if state.get('vram_gb'):
        card += ' ' + str(state['vram_gb']) + 'GB'
    return (loaded + ' ' + card
            + ' dtype=' + str(state.get('dtype'))
            + ' checkpoint_present=' + str(state.get('checkpoint_present')))


def mount(name, root='/kaggle/input'):
    """The first mounted path that ends with this name, or ''. The root is a parameter
    so the search can be exercised against a folder that is not a Kaggle mount."""
    try:
        for entry in sorted(os.listdir(root)):
            for inner in (entry, os.path.join(entry, entry.replace('-', '_'))):
                candidate = os.path.join(root, inner)
                if os.path.isdir(candidate):
                    for item in sorted(os.listdir(candidate)):
                        if item.lower() == name.lower():
                            return os.path.join(candidate, item)
                    # A kernel source mounts a whole Output one folder deeper than a
                    # dataset does: the weights are saved as models/ACE-Step-v1-3.5B,
                    # so they arrive at /kaggle/input/<kernel-slug>/models/<name>.
                    try:
                        subs = sorted(os.listdir(candidate))
                    except OSError:
                        subs = []
                    for sub in subs:
                        deeper = os.path.join(candidate, sub)
                        if not os.path.isdir(deeper):
                            continue
                        try:
                            inner_files = sorted(os.listdir(deeper))
                        except OSError:
                            continue
                        for item in inner_files:
                            if item.lower() == name.lower():
                                return os.path.join(deeper, item)
    except OSError:
        pass
    direct = os.path.join(root, name)
    return direct if os.path.exists(direct) else ''


def free_gb(path):
    """Space left under a path; /kaggle/working is a fixed-size disk."""
    try:
        return shutil.disk_usage(path).free / (1024 ** 3)
    except OSError:
        return 0.0


def scratch_dir(want_gb):
    """A place for a download that is not the output.

    /kaggle/working is the Output and it is capped at 20 GB. The Hugging Face cache
    and the model it was fetched from are two copies of the same 10 GB, so keeping
    both there crossed the cap while the download was still running, and Kaggle
    cancelled the session before the model could ever load. The cache only has to
    outlive the download, and this disk is not saved, so it goes here instead.
    """
    for candidate in ('/kaggle/temp', '/tmp'):
        if os.path.isdir(candidate) and free_gb(candidate) >= want_gb:
            return candidate
    return WORK


def unzip_all(folder, dest, names=None):
    """Extract every .zip in a mounted dataset into dest, and return dest.

    Uploading with `--dir-mode zip` leaves the dataset as repo.zip / pylibs.zip
    rather than as directories - Kaggle does not unpack them - so the mount has to be
    expanded before anything can be imported from it. /kaggle/input is read-only, so
    the expansion goes to the working directory.
    """
    import zipfile
    os.makedirs(dest, exist_ok=True)
    if not os.path.isdir(folder):
        return dest
    for name in sorted(os.listdir(folder)):
        if not name.lower().endswith('.zip'):
            continue
        if names and not any(part in name.lower() for part in names):
            continue
        target = os.path.join(dest, name[:-4])
        if os.path.isdir(target):
            continue
        say('extracting', name, '...')
        with zipfile.ZipFile(os.path.join(folder, name)) as archive:
            archive.extractall(target)
        # A zip of a folder usually carries that folder as its root; lift the
        # contents up so the paths below are the same either way it was made.
        entries = os.listdir(target)
        if len(entries) == 1 and os.path.isdir(os.path.join(target, entries[0])):
            inner = os.path.join(target, entries[0])
            for item in os.listdir(inner):
                os.rename(os.path.join(inner, item), os.path.join(target, item))
            os.rmdir(inner)
    return dest


CODE = os.environ.get('SONG_CODE') or mount('repo')
if not CODE or not os.path.isdir(CODE):
    # The dataset may be mounted as zips instead of directories, which is what the
    # first real run hit: the mount existed, repo/ inside it did not.
    code_mount = os.path.join('/kaggle/input', 'song-gpu-code')
    if os.path.isdir(code_mount):
        packed = unzip_all(code_mount, os.path.join(WORK, 'pack'))
        candidate = os.path.join(packed, 'repo')
        if os.path.isdir(candidate):
            CODE = candidate
        else:
            say('extracted', packed, 'but there is no repo/ in it:',
                sorted(os.listdir(packed))[:10])
if not CODE or not os.path.isdir(CODE):
    raise SystemExit('no mounted repository: expected /kaggle/input/<code dataset>/repo or '
                     'repo.zip. Run tools\\kaggle_pack.ps1 and attach song-gpu-code to the kernel.')
# /kaggle/input is read-only, and the service writes inside its own tree (the wav it
# renders, and the weights folder on a machine that has to fetch them), so the
# repository is copied into the working directory and everything runs from the copy.
MOUNTED = CODE
PACKED = os.path.dirname(MOUNTED)
REPO = os.path.join(WORK, 'repo')
if os.path.abspath(MOUNTED) != os.path.abspath(REPO):
    say('copying the repository into', REPO, '...')
    shutil.copytree(MOUNTED, REPO, dirs_exist_ok=True)
else:
    REPO = MOUNTED
SERVICE = os.path.join(REPO, 'song_service')
say('repo:', REPO, '(mounted at', MOUNTED + ')')

# --- no network, by design --------------------------------------------------
# Said out loud because the previous failure was a DNS error 30 seconds in, which
# says nothing about what is wrong.
NETWORK = False
try:
    socket.create_connection(('huggingface.co', 443), timeout=5).close()
    NETWORK = True
    say('this kernel does have internet; what is mounted is used first either way')
except Exception:
    say('no internet in this kernel, which is expected here: everything comes from /kaggle/input')

# --- the packages -----------------------------------------------------------
# Not a pip install: the dependencies are already unpacked into a directory beside
# the repo by tools\kaggle_pack.ps1, built for Linux and for this Python, so the
# kernel only has to put them on sys.path. A pip run would need a local wheel index
# and would still have to compile anything without a binary; this needs neither.
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

# The service runs as a child process, and a child does not inherit sys.path - only
# the environment. Both directories have to be exported or the service cannot see
# them, while this boot process can: that is what `No module named 'flask_cors'`
# was, with the package sitting in pylibs the whole time.
packaged = [p for p in (PYLIBS, ace) if os.path.isdir(p)]
inherited = os.environ.get('PYTHONPATH', '')
os.environ['PYTHONPATH'] = os.pathsep.join(packaged + ([inherited] if inherited else []))
# app.py puts its own vendored checkout on sys.path first, and a copy of that checkout
# inside the repository is not always complete: the packaged one had no acestep/models,
# so the service warmed with "No module named 'acestep.models'" while the mounted tree
# beside it was whole. Naming the mounted tree leaves nothing to chance.
os.environ['ACE_REPO'] = ace
say('ACE_REPO for the services:', ace)
say('PYTHONPATH for the services:', os.environ['PYTHONPATH'])

# --- the weights ------------------------------------------------------------
weights_mount = os.path.join('/kaggle/input', 'song-gpu-weights')
weights = mount('ACE-Step-v1-3.5B')
if not weights and os.path.isdir(weights_mount):
    # Zipped weights are extracted here too; only the ACE-Step tree is unpacked,
    # because the lyricist is optional and 7.7 GB already fills the working disk.
    packed_weights = unzip_all(weights_mount, os.path.join(WORK, 'weights'), names=('ace-step',))
    for name in sorted(os.listdir(packed_weights)):
        if name.lower().startswith('ace-step'):
            weights = os.path.join(packed_weights, name)
            break
if not weights and NETWORK:
    # Nothing mounted to read, but this kernel can still fetch: the dataset is the
    # offline path, not the only one. install.py is what a PC runs, so the versions it
    # resolves are the same ones.
    # The music model is the one the service cannot start without, and it is the
    # larger of the two; the lyricist is optional, so it is fetched only when the disk
    # has room for both. Filling the working disk half way through would leave the
    # kernel with nothing to serve, which is worse than starting without lyrics.
    room = free_gb(WORK)
    say('no mounted weights, and the kernel has internet: fetching them from Hugging Face',
        '(%.1f GB free in %s)' % (room, WORK))
    if room < 10:
        raise SystemExit('only %.1f GB free in %s, and the ACE-Step weights are ~8 GB: '
                         'attach the weights dataset instead of downloading them'
                         % (room, WORK))
    os.environ['HF_HOME'] = os.path.join(scratch_dir(12), 'hf')
    say('the Hugging Face cache goes to', os.environ['HF_HOME'],
        'so the output holds one copy of the weights, not two')
    installer = os.path.join(SERVICE, 'install.py')
    only = [] if room >= 14 else ['--only', 'ACE-Step']
    say('fetching:', 'everything the manifest lists' if not only else 'the music model only')
    fetched = subprocess.run([sys.executable, installer, '--weights'] + only, cwd=REPO)
    candidate = os.path.join(SERVICE, 'models', 'ACE-Step-v1-3.5B')
    if fetched.returncode == 0 and os.path.isdir(candidate):
        weights = candidate
    else:
        say('the download did not finish:', fetched.returncode)
if not weights:
    raise SystemExit('no ACE-Step weights: attach the weights dataset to this kernel, '
                     'or run it in one that has the internet')
# --- keep the weights where Kaggle can save them ----------------------------
# /kaggle/working is the only directory a run writes back as its Output, and an
# Output dataset is the one way the weights survive a session without uploading them
# from a PC. Six folders down (repo/song_service/models) they are hard to pick in the
# Output tab, so the directory moves to the root of the output under the name a
# dataset needs, ACE-Step-v1-3.5B at its top. A rename on the same disk: no bytes are
# copied and nothing is fetched again.
os.environ['ACE_CHECKPOINT'] = weights
if weights.startswith(os.path.join(SERVICE, 'models')):
    beside_output = os.path.join(WORK, 'models')
    try:
        if not os.path.isdir(beside_output):
            os.rename(os.path.join(SERVICE, 'models'), beside_output)
        weights = weights.replace(os.path.join(SERVICE, 'models'), beside_output, 1)
        os.environ['ACE_CHECKPOINT'] = weights
        say('weights are in the output at', weights)
        say('stop this run, then New Dataset from the Output tab and pick models/')
    except OSError as exc:
        say('left the weights where they were:', exc)
# The Hub cache keeps its own copy of every file it fetched, and the output is
# capped: two copies of 11 GB crossed that cap while the download was still running.
# The cache now lives on the scratch disk, so this only has to clear what an older
# boot left in the output, and what is left on the scratch disk either way.
cache = os.environ.get('HF_HOME') or ''
if cache and os.path.isdir(cache):
    shutil.rmtree(cache, ignore_errors=True)
    say('dropped the Hugging Face cache now the model is in place')
lyrics = mount('Qwen2.5-1.5B-Instruct')
if lyrics:
    # transformers reads this directory instead of reaching for the hub.
    os.environ['SONG_LYRICS_MODEL'] = lyrics
    os.environ['HF_HOME'] = os.path.join(scratch_dir(2), 'hf')
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    os.environ['HF_HUB_OFFLINE'] = '1'
say('weights:', weights)

# --- the service ------------------------------------------------------------
os.environ.update(SONG_PORT=str(PORT), SONG_HOST='0.0.0.0', SONG_WARM='1')
log = open(os.path.join(WORK, 'song.log'), 'w')
proc = subprocess.Popen([sys.executable, 'app.py', '--role', 'song'], cwd=SERVICE,
                        stdout=log, stderr=subprocess.STDOUT)
health = ''
state = {}
for attempt in range(36):
    answered = False
    try:
        with urllib.request.urlopen('http://127.0.0.1:%d/health' % PORT, timeout=5) as answer:
            health = answer.read().decode()
        answered = True
    except Exception:
        pass
    try:
        state = json.loads(health) if health.strip().startswith('{') else {}
    except ValueError:
        state = {}
    # /health answers while the model is still warming, so the first reply proves the
    # service is up and nothing more: stay in the loop until "loaded" is true, or the
    # service stops claiming it is warming, or the three minutes are gone.
    if answered and (state.get('loaded') or not state.get('warming')):
        break
    if attempt and attempt % 6 == 0:
        say('  waiting for', 'the model to load' if answered else '/health', '...',
            str(attempt * 5) + 's')
        if answered:
            say('   ', health_summary(state))
    time.sleep(5)
if not health:
    say('the service did not answer within 3 minutes. End of song.log:')
    try:
        say(open(os.path.join(WORK, 'song.log')).read()[-2500:])
    except OSError:
        pass
    raise SystemExit(2)
say('service:', health_summary(state))
if not state.get('loaded'):
    if state.get('warm_failed'):
        say('the model failed to load, so a song would not render. End of song.log:')
        try:
            say(open(os.path.join(WORK, 'song.log')).read()[-2000:])
        except OSError:
            pass
        # A machine that cannot load is not a pool machine. Staying alive holds the
        # card for twelve hours and spends the week's quota on nothing, so the run
        # ends here - which is also what saves this version's output.
        proc.terminate()
        say('stopping this run: the GPU would be held by a machine that cannot render')
        raise SystemExit(4)
    else:
        say('the model is not loaded yet; the first song request will load it (song.log has the warm)')
else:
    say('model loaded on', state.get('gpu') or state.get('device'), '- songs can render now')

# --- the voice service ------------------------------------------------------
voice_log = open(os.path.join(WORK, 'voice.log'), 'w')
subprocess.Popen([sys.executable, 'app.py'], cwd=os.path.join(REPO, 'voice_service'),
                 stdout=voice_log, stderr=subprocess.STDOUT,
                 env=dict(os.environ, VOICE_PORT=str(VOICE_PORT)))

# --- the video downloader ---------------------------------------------------
# Started only when this boot can actually complete a download, and said out loud
# either way instead of leaving /pc to accept a job that can never finish. Two
# things decide it: yt-dlp has to be importable (the packed wheels beside the repo
# are built from song_service and voice_service, so it usually is not), and the
# kernel has to have the network - the normal case here is none at all, which is
# why the song stack boots from mounted files in the first place. The online boot,
# public/song_gpu_cell.py, is where a downloader always starts.
DOWNLOAD_PORT = 8080
say()
try:
    import flask_cors  # noqa: F401
    import imageio_ffmpeg  # noqa: F401
    import yt_dlp  # noqa: F401
    packed_downloader = True
except ModuleNotFoundError as missing:
    packed_downloader = False
    say('video downloads: not started -', missing.name, 'is not in the packed wheels beside the repo')
if not packed_downloader:
    say('                 songs and the voice are unaffected; repack with')
    say('                 downloader_service/requirements.txt added to tools\\kaggle_pack.ps1')
    say('                 to have it here on a kernel that has internet')
elif not NETWORK:
    say('video downloads: not started - this kernel has no internet, so yt-dlp could not')
    say('                 fetch a video even though its package is here')
else:
    say('starting the video downloader on 8080 ...')
    download_log = open(os.path.join(WORK, 'downloads.log'), 'w')
    download_proc = subprocess.Popen([sys.executable, 'app.py'],
                                     cwd=os.path.join(REPO, 'downloader_service'),
                                     stdout=download_log, stderr=subprocess.STDOUT,
                                     env=dict(os.environ, PORT=str(DOWNLOAD_PORT),
                                              MAX_HEIGHT='720'))
    download_health = ''
    for attempt in range(15):
        try:
            with urllib.request.urlopen('http://127.0.0.1:%d/health' % DOWNLOAD_PORT,
                                        timeout=5) as answer:
                download_health = answer.read().decode()
            break
        except Exception:
            if attempt and attempt % 5 == 0:
                say('  waiting for the downloader ...', str(attempt * 2) + 's')
            time.sleep(2)
    if download_health:
        say('downloads:', download_health[:220])
    else:
        say('the downloader did not answer on 8080 within 30s. End of downloads.log:')
        try:
            say(open(os.path.join(WORK, 'downloads.log')).read()[-1200:])
        except OSError as exc:
            say('no log at all:', exc)

# --- the tunnel -------------------------------------------------------------
binary = os.path.join(PACKED, 'cloudflared-linux-amd64')
if not os.path.isfile(binary):
    raise SystemExit('no cloudflared binary next to the repo: ' + binary)
# /kaggle/input is read-only: chmod on the mounted copy fails with EROFS ([Errno 30]),
# so copy the binary into the working directory before making it executable. The
# mount often arrives executable already, in which case it is used where it is.
try:
    os.chmod(binary, 0o755)
except OSError:
    local_binary = os.path.join(WORK, 'cloudflared-linux-amd64')
    if not os.path.isfile(local_binary):
        say('copying cloudflared out of the read-only mount ...')
        shutil.copyfile(binary, local_binary)
    os.chmod(local_binary, 0o755)
    binary = local_binary
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
    reached = False
    for attempt in range(10):
        try:
            with urllib.request.urlopen(url + '/health', timeout=10):
                pass
        except Exception as exc:
            say('  the tunnel is not answering yet:', exc)
            time.sleep(10)
            continue
        reached = True
        tell_pool(url, join_key)
        # Started whether or not the first post landed: it is the same address, and a
        # refused post is exactly the case the repeat exists to repair.
        keep_joined(url, join_key)
        break
    if not reached:
        say('the card never answered through the tunnel, so the pool was not told about it')

say('ready: the service is on', PORT, 'behind', url)
# The kernel has to stay alive for the tunnel and the service to keep serving.
while True:
    time.sleep(60)
    if proc.poll() is not None:
        say('the song service exited; nothing left to serve')
        break
