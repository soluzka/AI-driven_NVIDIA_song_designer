"""Fail if the voice fit starts making low end again, in any profile.

    .venv\\Scripts\\python tools\\check_fit_low_end.py
    .venv\\Scripts\\python tools\\check_fit_low_end.py --url "https://.../?fittest=1&split=0"
    .venv\\Scripts\\python tools\\check_fit_low_end.py --profiles "lo-fi,dub"

The rumble under the fitted phrase came from the waveshaper: a 220 Hz + 1 kHz test tone went
into the chain at -101 dB in the 80-120 Hz band and came out at -35.9 dB, because the drive
turns partials into difference tones (1 kHz - 4 x 220 Hz = 120 Hz). With the low end routed
around the shaper the band sits near -57 dB. This runs that same measurement against the real
fitRender in a headless copy of the page and fails if the band comes back up.

Every genre profile is measured at 0 and -5 semitones, not just neutral: each has its own
highpass, ceiling, drive and room, so each can make low end of its own. A page load renders
one batch of them (`?fittest=1&profiles=...`), because rendering is real time and a whole sweep
outlasts a headless dump.

There is also a "grind" case, and it is the one that matters most: four partials above the
400 Hz split, 80, 100 and 120 Hz apart. Two sines an octave apart never made low end through
this chain, but those partials become their differences inside the shaper and put the 80-120 Hz
band at -30 dB. Anything that stops catching that has stopped testing the thing that rumbled.

The measurement lives in the page (window.fitSelfTest), not in this file, so there is nothing
here to drift from the chain it is checking. `?fittest=1&split=0` drives the whole signal again
and is the built-in way to watch this check fail.
"""

import argparse
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_URL = "https://browser.bpiersing.workers.dev/?fittest=1"

# Must match FIT_PROFILES in public/index.html.
PROFILES = [
    "dubstep", "riddim", "techno", "house", "drum and bass", "jungle", "reggae",
    "dub", "hip hop", "trap", "ambient", "trance", "garage", "lo-fi",
]

# Measured with the low end routed around the drive: -57.3 dB at 0 semitones, -56.0 at -5,
# -57.8 for the dub profile. -52 leaves five decibels of headroom before this fails; the
# broken chain measured -30.3 to -41.6, so the gap is wide.
BAND = 2                 # the 80-120 Hz column of fitSelfTest's table
LIMIT_DB = -52.0
LOW_VOICE = "low voice 110 Hz"
FUNDAMENTAL_FLOOR_DB = -6.0   # a low voice must keep its own band
BATCH = 3                # profiles per page load: 6 tone renders plus neutral and the voice

BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def find_browser():
    for path in BROWSERS:
        if os.path.exists(path):
            return path
    for name in ("msedge", "chrome", "chromium", "google-chrome"):
        found = shutil.which(name)
        if found:
            return found
    raise SystemExit("No Edge or Chrome found, so the page cannot be measured. "
                     "Install one, or deploy with --no-check and measure later.")


def batch_url(url, names):
    separator = "&" if "?" in url else "?"
    return url + separator + "profiles=" + urllib.parse.quote(",".join(names), safe=",")


def parse_answer(text):
    if text.startswith("FITTEST-ERROR"):
        raise SystemExit("The page could not run the test: " + text[:400])
    marker = text.find("FITTEST ")
    if marker < 0:
        raise SystemExit("Unreadable answer from the page: " + text[:400])
    return json.loads(text[marker + len("FITTEST "):])


class _Collector(http.server.BaseHTTPRequestHandler):
    """Takes the one answer the page navigates to."""

    def do_GET(self):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "result" in query:
            self.server.answer = query["result"][0]
            self.server.done.set()
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args):
        pass


def run_page(browser, url):
    # The page used to be read with --dump-dom under --virtual-time-budget, and the dump
    # could arrive before the offline renders finished: two batches in five came back
    # without an answer from pages that answered correctly in a real browser, and since
    # the page's own source contains the string "FITTEST-ERROR" the failure then quoted
    # that template instead of the reason. Virtual time does not wait for the audio
    # thread, and no budget fixes that. So the page navigates to ?report= with its answer
    # instead, into this process, and the browser is closed once it does.
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Collector)
    server.answer = None
    server.done = threading.Event()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    report = "http://127.0.0.1:%d/" % server.server_address[1]
    separator = "&" if "?" in url else "?"
    target = url + separator + "report=" + urllib.parse.quote(report, safe=":/")

    profile = tempfile.mkdtemp(prefix="fit-low-end-")
    command = [
        browser,
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--autoplay-policy=no-user-gesture-required",
        "--user-data-dir=" + profile,
        target,
    ]
    proc = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not server.done.wait(240):
            raise SystemExit("The page did not report a result within 240 s: %s" % url)
        return parse_answer(server.answer)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        server.shutdown()
        server.server_close()


def measure(browser, url, names, batch):
    report = None
    rows = []
    for start in range(0, len(names), batch):
        chunk = names[start:start + batch]
        print("  measuring %s" % ", ".join(chunk), flush=True)
        page = run_page(browser, batch_url(url, chunk))
        report = report or page
        for row in page["rows"]:
            # neutral and the low voice come back with every batch; they are the same
            # measurement each time, so keep one copy.
            if row["label"] not in {existing["label"] for existing in rows}:
                rows.append(row)
    return report, rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--limit", type=float, default=LIMIT_DB)
    parser.add_argument("--profiles", default="")
    parser.add_argument("--batch", type=int, default=BATCH)
    parser.add_argument("--show-bands", action="store_true",
                        help="print every band, not only 80-120 Hz")
    args = parser.parse_args()

    names = [name.strip() for name in args.profiles.split(",") if name.strip()] or PROFILES
    unknown = [name for name in names if name not in PROFILES]
    if unknown:
        raise SystemExit("Unknown profile(s): %s\nKnown: %s"
                         % (", ".join(unknown), ", ".join(PROFILES)))

    browser = find_browser()
    print("browser:", os.path.basename(browser))
    print("page:   ", args.url)
    report, rows = measure(browser, args.url, names, args.batch)
    print("bands:  ", ", ".join(report.get("bands") or []))
    print()

    problems = []
    for row in rows:
        level = row["bands"][BAND]
        if row["label"] == LOW_VOICE:
            if level < FUNDAMENTAL_FLOOR_DB:
                problems.append("%s lost its low end: %.1f dB (floor %.1f)"
                                % (row["label"], level, FUNDAMENTAL_FLOOR_DB))
                verdict = "FAIL"
            else:
                verdict = "ok"
        elif level > args.limit:
            problems.append("%s has %.1f dB in 80-120 Hz (limit %.1f)"
                            % (row["label"], level, args.limit))
            verdict = "FAIL"
        else:
            verdict = "ok"
        if args.show_bands:
            print("%-4s %-20s 80-120 Hz: %6.1f dB   all: %s" % (
                verdict, row["label"], level,
                " ".join("%6.1f" % value for value in row["bands"])))
        else:
            print("%-4s %-20s 80-120 Hz: %6.1f dB" % (verdict, row["label"], level))

    print()
    worst = max((row for row in rows if row["label"] != LOW_VOICE),
                key=lambda row: row["bands"][BAND], default=None)
    if problems:
        for problem in problems:
            print("FAIL  " + problem)
        return 1
    print("the low end stayed where it was measured: %d profiles, nothing above %.1f dB in "
          "80-120 Hz (worst: %s at %.1f dB)"
          % (len(names), args.limit, worst["label"], worst["bands"][BAND]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
