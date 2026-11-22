"""The video key is defined once and used by three sides; check they still mean it.

    .venv\\Scripts\\python tools\\check_video_keys.py

No copy of the key lives in worker.js, public/index.html or this repository's Python any
more: all three run public/lib/video-key.js. This checks that no copy has crept back, that
the copy the site serves is the same file, and that a handful of real links key the way they
should - one video in every form it is linked, and two different videos never the same key.
"""

import json
import os
import subprocess
import sys
import tempfile
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE = os.path.join(ROOT, "tools", "node", "node.exe")
SITE = "https://browser.bpiersing.workers.dev"
MODULE_URL = SITE + "/lib/video-key.js"

# Real links from the index, plus the forms the same video is stored under elsewhere.
SAMPLES = {
    "a YouTube video, linked four ways": [
        "https://www.youtube.com/watch?v=MJX5q2BE2rk",
        "https://youtu.be/MJX5q2BE2rk",
        "https://www.youtube.com/shorts/MJX5q2BE2rk",
        "https://www.youtube.com/embed/MJX5q2BE2rk?rel=0",
        "https://m.youtube.com/watch?v=MJX5q2BE2rk&si=trackingonly",
    ],
    "a Dailymotion video, linked two ways": [
        "https://www.dailymotion.com/video/x8jfpqn",
        "https://www.dailymotion.com/embed/video/x8jfpqn",
    ],
}

# Different videos that share a path: these must never collapse into one key.
DISTINCT = {
    "two YouTube Music playlists": [
        "https://music.youtube.com/playlist?list=PLuvJc7QPNrF",
        "https://music.youtube.com/playlist?list=PLJtpjlkBVF4",
    ],
    "two Play Store apps": [
        "https://play.google.com/store/apps/details?id=com.google.android.youtube",
        "https://play.google.com/store/apps/details?id=com.netflix.mediaclient",
    ],
    "the same help page in two languages": [
        "https://support.google.com/translate/?hl=en",
        "https://support.google.com/translate/?hl=es",
    ],
}

# The same page with and without tracking parameters is one video.
TRACKED = [
    ("https://www.youtube.com/watch?v=MJX5q2BE2rk&feature=share&utm_source=twitter",
     "https://www.youtube.com/watch?v=MJX5q2BE2rk"),
]


def exporter():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "export_index", os.path.join(ROOT, "tools", "export_index.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def keys_from_module_file(path, links):
    """Keys as the page's copy of the module would answer, run in Node."""
    folder = tempfile.mkdtemp(prefix="video-key-")
    with open(path, "rb") as handle:
        text = handle.read().decode("utf-8")
    with open(os.path.join(folder, "video-key.js"), "w", encoding="utf-8") as handle:
        handle.write(text)
    with open(os.path.join(folder, "package.json"), "w", encoding="utf-8") as handle:
        json.dump({"type": "module"}, handle)
    script = (
        "import { videoKey } from %s;\n"
        "let text = '';\n"
        "process.stdin.setEncoding('utf8');\n"
        "process.stdin.on('data', c => { text += c; });\n"
        "process.stdin.on('end', () => process.stdout.write(JSON.stringify("
        "JSON.parse(text).map(videoKey))));\n"
        % json.dumps("file:///" + os.path.join(folder, "video-key.js").replace("\\", "/"))
    )
    result = subprocess.run([NODE, "--input-type=module", "-e", script],
                            input=json.dumps(links).encode("utf-8"), capture_output=True)
    if result.returncode != 0:
        raise SystemExit("Node could not load the module copy: "
                         + result.stderr.decode("utf-8", "replace")[-400:])
    return json.loads(result.stdout.decode("utf-8"))


def main():
    problems = []
    module = exporter()
    all_links = []
    for group in list(SAMPLES.values()) + list(DISTINCT.values()):
        all_links.extend(group)
    all_links.extend(pair[0] for pair in TRACKED)
    keys = dict(zip(all_links, module.video_keys(all_links)))

    for label, group in SAMPLES.items():
        seen = {keys[link] for link in group}
        if len(seen) == 1:
            print("ok    %s -> %s" % (label, seen.pop()))
        else:
            problems.append("%s keyed as %s" % (label, sorted(seen)))

    for label, group in DISTINCT.items():
        got = [keys[link] for link in group]
        if len(set(got)) == len(got):
            print("ok    %s stay apart -> %s" % (label, got))
        else:
            problems.append("%s collapsed into %s" % (label, got))

    for tracked, plain in TRACKED:
        if keys[tracked] == keys[plain]:
            print("ok    tracking parameters ignored -> %s" % keys[plain])
        else:
            problems.append("tracking parameters changed the key: %s vs %s"
                            % (keys[tracked], keys[plain]))

    # No copy has crept back into the two sides that used to keep one.
    for path, marker in (("worker.js", "import { videoKey } from './public/lib/video-key.js'"),
                         (os.path.join("public", "index.html"),
                          "import { videoKey } from '/lib/video-key.js'")):
        with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
            text = handle.read()
        imports = marker in text
        own_copy = "function videoKey(" in text
        if imports and not own_copy:
            print("ok    %s uses the shared module and keeps no copy" % path)
        else:
            problems.append("%s: imports shared=%s, keeps its own copy=%s"
                            % (path, imports, own_copy))

    # The copy the site serves is the file in this repository, and it answers the same way.
    try:
        request = urllib.request.Request(MODULE_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=20) as response:
            served = response.read()
        with open(os.path.join(ROOT, "public", "lib", "video-key.js"), "rb") as handle:
            local = handle.read()
        if served == local:
            print("ok    %s is the same file as public/lib/video-key.js" % MODULE_URL)
        else:
            problems.append("the deployed %s differs from public/lib/video-key.js"
                            % MODULE_URL)
        folder = tempfile.mkdtemp(prefix="video-key-deployed-")
        served_path = os.path.join(folder, "video-key.js")
        with open(served_path, "wb") as handle:
            handle.write(served)
        served_keys = keys_from_module_file(served_path, all_links)
        if served_keys == [keys[link] for link in all_links]:
            print("ok    the deployed copy answers the same keys as the exporter")
        else:
            problems.append("the deployed copy answers different keys")
    except Exception as err:  # offline, or the site is down: say so, do not pretend
        print("skip  could not compare the deployed copy: %s" % err)

    if problems:
        print()
        for problem in problems:
            print("FAIL  " + problem)
        return 1
    print("\nall sides agree")
    return 0


if __name__ == "__main__":
    sys.exit(main())
