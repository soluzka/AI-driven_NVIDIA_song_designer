"""Fail if the voice fit starts making low end again.

    .venv\\Scripts\\python tools\\check_fit_low_end.py
    .venv\\Scripts\\python tools\\check_fit_low_end.py --url "https://.../?fittest=1&split=0"

The rumble under the fitted phrase came from the waveshaper: a 220 Hz + 1 kHz test tone went
into the chain at -101 dB in the 80-120 Hz band and came out at -35.9 dB, because the drive
turns partials into difference tones (1 kHz - 4 x 220 Hz = 120 Hz). With the low end routed
around the shaper the band sits near -57 dB. This runs that same measurement against the real
fitRender in a headless copy of the page and fails if the band comes back up.

The measurement lives in the page (window.fitSelfTest), not in this file, so there is nothing
here to drift from the chain it is checking. Pass a deliberately broken query -
`?fittest=1&split=0` drives the whole signal again - to see the check fail.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_URL = "https://browser.bpiersing.workers.dev/?fittest=1"

# Measured with the low end routed around the drive: -57.3 dB at 0 semitones, -56.0 at -5,
# -57.8 for the dub profile. -52 leaves five decibels of headroom before this fails; the
# broken chain measured -30.3 and -26.3, so the gap is wide.
BAND = 2                 # the 80-120 Hz column of fitSelfTest's table
LIMIT_DB = -52.0
LOW_VOICE = "low voice 110 Hz"
FUNDAMENTAL_FLOOR_DB = -6.0   # a low voice must keep its own band

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
    raise SystemExit("No Edge or Chrome found to run the page in.")


def run_page(browser, url):
    profile = tempfile.mkdtemp(prefix="fit-low-end-")
    command = [
        browser,
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--autoplay-policy=no-user-gesture-required",
        "--user-data-dir=" + profile,
        "--virtual-time-budget=30000",
        "--dump-dom",
        url,
    ]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                            errors="replace")
    dom = (result.stdout or "") + (result.stderr or "")
    marker = dom.find("FITTEST ")
    if marker < 0:
        if "FITTEST-ERROR" in dom:
            start = dom.find("FITTEST-ERROR")
            raise SystemExit("The page could not run the test: " + dom[start:start + 300])
        raise SystemExit("The page did not answer with FITTEST (did the deploy land?)\n"
                         + dom[-600:])
    start = marker + len("FITTEST ")
    depth = 0
    end = start
    for index in range(start, len(dom)):
        if dom[index] == "{":
            depth += 1
        elif dom[index] == "}":
            depth -= 1
            if depth == 0:
                end = index + 1
                break
    return json.loads(dom[start:end])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--limit", type=float, default=LIMIT_DB)
    args = parser.parse_args()

    browser = find_browser()
    print("browser:", os.path.basename(browser))
    print("page:   ", args.url)
    report = run_page(browser, args.url)
    bands = report.get("bands") or []
    print("bands:  ", ", ".join(bands))
    print()

    problems = []
    for row in report.get("rows", []):
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
        print("%-4s %-20s 80-120 Hz: %6.1f dB   (all bands: %s)"
              % (verdict, row["label"], level, ", ".join("%.1f" % value for value in row["bands"])))

    print()
    if problems:
        for problem in problems:
            print("FAIL  " + problem)
        return 1
    print("the low end stayed where it was measured: nothing above %.1f dB in 80-120 Hz"
          % args.limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
