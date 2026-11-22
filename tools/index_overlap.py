"""Compare the shipped index with the database, video by video.

Both sides are keyed the way the Worker keys them, so a YouTube video stored as watch?v= in
one and youtu.be/ in the other counts as one video. Prints how far the two overlap, whether
they disagree about a video's details, and the library figure that follows from the
comparison.

    .venv\\Scripts\\python tools\\index_overlap.py
"""

import importlib.util
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _exporter():
    spec = importlib.util.spec_from_file_location(
        "export_index", os.path.join(ROOT, "tools", "export_index.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_index():
    with open(os.path.join(ROOT, "public", "data", "videos.json"), encoding="utf-8") as handle:
        return json.load(handle)["videos"]


def load_database():
    command = [
        "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
        os.path.join(ROOT, "build", "wr.ps1"),
        "d1", "execute", "video-index", "--remote", "--json",
        "--command", "SELECT url, title, duration, description FROM videos",
    ]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
    text = result.stdout or ""
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end < 0:
        raise SystemExit("No rows came back from the database:\n" + text[-800:] + result.stderr[-400:])
    payload = json.loads(text[start:end + 1])
    rows = []
    for entry in payload:
        rows.extend(entry.get("results") or [])
    return rows


def clean(value):
    return " ".join(str(value or "").split()).strip().lower()


def main():
    exporter = _exporter()
    index = load_index()
    database = load_database()

    index_by_key = {}
    index_dupes = 0
    for row in index:
        key = exporter.video_key(row.get("u", ""))
        if key in index_by_key:
            index_dupes += 1
        else:
            index_by_key[key] = row

    database_by_key = {}
    database_dupes = 0
    for row in database:
        key = exporter.video_key(row.get("url", ""))
        if key in database_by_key:
            database_dupes += 1
        else:
            database_by_key[key] = row

    overlap = set(index_by_key) & set(database_by_key)
    extra = set(database_by_key) - set(index_by_key)

    disagreements = {"title": [], "duration": [], "description": []}
    for key in sorted(overlap):
        shipped = index_by_key[key]
        stored = database_by_key[key]
        pairs = (
            ("title", clean(shipped.get("t")), clean(stored.get("title"))),
            ("duration", clean(shipped.get("du")), clean(stored.get("duration"))),
            ("description", clean(shipped.get("d")), clean(stored.get("description"))),
        )
        for field, left, right in pairs:
            if left and right and left != right:
                disagreements[field].append((key, left, right))

    print("shipped index : %d rows, %d videos (same video under two links: %d)"
          % (len(index), len(index_by_key), index_dupes))
    print("database      : %d rows, %d videos (same video under two links: %d)"
          % (len(database), len(database_by_key), database_dupes))
    print("in both       : %d videos" % len(overlap))
    print("only database : %d videos" % len(extra))
    print("library       : %d videos (%d + %d)" % (len(index_by_key) + len(extra),
                                                   len(index_by_key), len(extra)))
    print()
    for field, items in disagreements.items():
        print("%-11s differ on %d of the %d shared videos" % (field, len(items), len(overlap)))
        for key, left, right in items[:3]:
            print("   %-28s index: %-52s database: %s" % (key, left[:52], right[:52]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
