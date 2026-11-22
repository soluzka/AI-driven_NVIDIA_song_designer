"""Refresh the committed video index from the live database.

The app reads ../video_index.db at runtime, but the repo also carries a
snapshot (data/video_index.db) and a JSON copy used by the static site
(public/data/videos.json). Run this after scraping so all three agree:

    .venv\\Scripts\\python tools\\export_index.py

The static site has no Python behind it, so the genre model cannot run there.
Genres are therefore inferred here, once, and shipped inside the JSON next to
 each video.
"""

import json
import math
import os
import re
import sqlite3
import sys
from urllib.parse import parse_qs, urlsplit
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from genre_model import MusicGenreModel  # noqa: E402
LIVE_DB = os.path.join(ROOT, "..", "video_index.db")
SNAPSHOT_DB = os.path.join(ROOT, "data", "video_index.db")
STATIC_JSON = os.path.join(ROOT, "public", "data", "videos.json")


# A reset live database does not announce itself: it is a valid database with fewer
# rows, and the backup below copies it straight over the committed snapshot. One run
# against 36 KB of empty tables took the 8,724-row snapshot and the static index with
# it, and a database with five rows would do exactly the same. Anything under this
# share of the snapshot is treated as a reset rather than as news.
MIN_KEEP = 0.9

_ESCAPED = re.compile(r'\\u([0-9a-fA-F]{4})')


def video_key(link: str) -> str:
    """The identity the Worker uses, so a link this index ships is recognised there.

    One video is watch?v=, youtu.be/, /shorts/ and /embed/; a Dailymotion one is /video/
    or /embed/video/. Both sides have to agree, or "the library already has this" is a
    guess and the count of what the database adds is wrong.
    """
    text = (link or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return text.lower()
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path or ""
    if host == "youtu.be":
        vid = path.strip("/").split("/")[0]
        if vid:
            return "y:" + vid
    elif host.endswith("youtube.com"):
        if path == "/watch":
            vid = (parse_qs(parts.query).get("v") or [""])[0]
        elif path == "/playlist":
            # A playlist is not the page: every playlist in YouTube Music shares the path.
            playlist = (parse_qs(parts.query).get("list") or [""])[0]
            if playlist:
                return "p:" + playlist
            vid = ""
        else:
            match = re.match(r"^/(?:shorts|embed|live)/([A-Za-z0-9_-]{11})", path)
            vid = match.group(1) if match else ""
        if vid:
            return "y:" + vid
    elif host.endswith("dailymotion.com"):
        match = re.search(r"/(?:embed/)?video/([A-Za-z0-9]+)", path)
        if match:
            return "d:" + match.group(1)
    elif host.endswith("vimeo.com"):
        match = re.search(r"(\d{6,})", path)
        if match:
            return "v:" + match.group(1)
    # Anything else: the path alone is not an identity. Every app on Google Play and every
    # YouTube Music playlist shares its path and differs in the query, so dropping the query
    # merged dozens of unrelated videos into one key - which also made the total too low.
    return "u:" + (host + path).rstrip("/").lower() + _query_key(parts)


TRACKING_PARAMS = {"si", "feature", "pp", "t", "fbclid", "gclid", "igshid", "ref", "ref_src"}


def _query_key(parts) -> str:
    try:
        pairs = parse_qs(parts.query, keep_blank_values=False)
    except ValueError:
        return ""
    kept = []
    for name, values in pairs.items():
        lowered = name.lower()
        if lowered in TRACKING_PARAMS or lowered.startswith("utm_"):
            continue
        for value in values:
            kept.append(f"{name}={value}")
    return "?" + "&".join(sorted(kept)) if kept else ""


def plain_text(value) -> str:
    """A title as it should read.

    YouTube's API answers with JSON-escaped text, and the scraper stored it verbatim, so
    "Wooli \\u0026 Trivecta" reached the page as those six characters and "u0026" was
    picked up as a word by the genre model and by the remix keywords. Anything that looks
    like a unicode escape is decoded on the way out.
    """
    if not value:
        return ""
    text = str(value)
    if "\\u" not in text:
        return text
    return _ESCAPED.sub(lambda match: chr(int(match.group(1), 16)), text)


def count_videos(path):
    """Videos in a database, or 0 when it is missing or has no such table."""
    if not os.path.exists(path):
        return 0
    connection = sqlite3.connect(path)
    try:
        return connection.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
    except sqlite3.Error:
        return 0
    finally:
        connection.close()


def shrink_refusal(live_count, snapshot_count):
    """Why this live database must not be copied over the snapshot, or None.

    Kept as a function of the two counts so the rule can be checked without
    touching either database.
    """
    if snapshot_count <= 0:
        return None  # nothing to protect yet
    if live_count >= snapshot_count * MIN_KEEP:
        return None
    return ("live database holds %d videos against %d in the committed snapshot, "
            "under the %d%% a refresh must keep"
            % (live_count, snapshot_count, round(MIN_KEEP * 100)))


# --- readings from the library around a listing ------------------------------
# Most rows say nothing a genre vocabulary can catch: "Untitled Video", a part number, a
# workshop clip. Their own words cannot name a genre, but the library can speak for them -
# if a title shares its rare words with rows that are labelled, those labels are evidence
# about this one. It is written as a reading and the page prints it as one, and only when
# the neighbours are clearly closer than the alternatives: at least two labelled neighbours,
# a real share of the weight, and a margin over the next genre.
NEIGHBOUR_STOP = {
    'the', 'and', 'for', 'with', 'from', 'that', 'this', 'you', 'your', 'has', 'have',
    'was', 'were', 'are', 'not', 'but', 'all', 'out', 'over', 'into', 'about', 'after',
    'official', 'video', 'audio', 'lyrics', 'lyric', 'music', 'song', 'songs', 'full',
    'new', 'hd', 'hq', 'feat', 'ft', 'prod', 'remix', 'edit', 'clip', 'mix', 'album',
    'track', 'untitled', 'youtube', 'www', 'com', 'http', 'https',
}

# A neighbour has to be close on its own terms, not merely first among weak matches: a
# title of one generic word ("Intro", "Keyboard shortcuts") matches something everywhere,
# and taking that as evidence is how a reading turns into a guess. So the comparison is a
# cosine over idf-weighted words, the query needs at least three words that are not the
# same generic words, two of those words have to be shared with the winner, and the winner
# has to hold a clear share with a margin over the next genre.
NEIGHBOUR_NEIGHBOURS = 5
NEIGHBOUR_MIN_COSINE = 0.55    # how close the best labelled row has to be
NEIGHBOUR_MIN_SUPPORT_COSINE = 0.45
NEIGHBOUR_MIN_SHARED_WORDS = 2
NEIGHBOUR_MIN_QUERY_WORDS = 3
NEIGHBOUR_MIN_COVERAGE = 0.55  # of this listing's own weight, matched by the neighbour
NEIGHBOUR_MIN_SHARE = 0.68     # of the neighbour weight, against the next genre
NEIGHBOUR_MIN_SUPPORT = 2      # distinct labelled rows agreeing
NEIGHBOUR_MIN_MARGIN = 1.5     # times the runner-up's weight
NEIGHBOUR_DISTINCTIVE_DF = 5   # a shared word may not be common in the labelled rows


def _tokens(text: str):
    return [word for word in re.findall(r"[a-z0-9']{3,}", str(text or '').lower())
            if word not in NEIGHBOUR_STOP and not word.isdigit()]


def _has_music_cue(text: str) -> bool:
    """Whether a listing says it is music at all.

    The library is mostly not music: pilates routines, race cards, cake clips. Those clusters
    sit next to each other in the words and a nearest-neighbour pass will happily hand a
    pilates guide the genre of another pilates guide that the vocabulary had mislabelled.
    A listing that never says music, song, remix, album, beat or audio is not offered a
    borrowed reading; the honest answer for it is still "unclassified".
    """
    return any(re.search(r'(?<![a-z0-9])' + re.escape(cue) + r's?(?![a-z0-9])', text)
               for cue in MUSIC_LISTING_CUES)


MUSIC_LISTING_CUES = ('music', 'song', 'remix', 'mix', 'album', 'single', 'ep', 'beat',
                     'beats', 'instrumental', 'lyrics', 'concert', 'band', 'singer',
                     'vocal', 'vocals', 'audio', 'ost', 'soundtrack', 'playlist', 'dj',
                     'mixtape', 'bootleg', 'rework', 'live set')


def neighbour_readings(videos, model):
    """Give rows that name no genre a reading borrowed from labelled neighbours.

    Returns (rows given a reading, rows with words to compare).
    """
    # Only labels worth borrowing: a confident genre, on a listing that reads as music.
    # These 1,541 labels are what the vocabulary produced, and it is not always right about
    # what a page is - a neighbour pass over its mistakes spreads them, so the pool is
    # the rows that are both confident and plainly musical.
    labelled = [row for row in videos
                if (row.get('g') or {}).get('n')
                and float((row.get('g') or {}).get('c') or 0) >= 0.6
                and model.is_music_listing(row.get('t', ''), row.get('d', ''), row.get('tg', ''))
                and _has_music_cue(('%s %s %s' % (row.get('t', ''), row.get('d', ''), row.get('tg', ''))).lower())]
    if len(labelled) < 10:
        return 0, 0
    doc_tokens = [_tokens('%s %s' % (row.get('t', ''), row.get('tg', ''))) for row in labelled]

    document_frequency = {}
    for tokens in doc_tokens:
        for token in set(tokens):
            document_frequency[token] = document_frequency.get(token, 0) + 1
    total = len(labelled)
    idf = {token: math.log((total + 1) / (count + 1)) + 1.0
           for token, count in document_frequency.items()}

    vectors = []
    for tokens in doc_tokens:
        weights = {token: idf[token] for token in set(tokens)}
        norm = math.sqrt(sum(value * value for value in weights.values())) or 1.0
        vectors.append({token: value / norm for token, value in weights.items()})

    index = {}
    for position, tokens in enumerate(doc_tokens):
        for token in set(tokens):
            index.setdefault(token, []).append(position)

    given = 0
    considered = 0
    for row in videos:
        if (row.get('g') or {}).get('n') or row.get('w'):
            continue
        # The model's own refusal applies before neighbours are asked: a listing about
        # horses, parts or prices is not a quiet music listing, and a shared word like
        # "grand" or "national" must not be allowed to make it one.
        if not model.is_music_listing(row.get('t', ''), row.get('d', ''), row.get('tg', '')):
            continue
        if not _has_music_cue(('%s %s %s' % (row.get('t', ''), row.get('d', ''), row.get('tg', ''))).lower()):
            continue
        words = set(_tokens('%s %s' % (row.get('t', ''), row.get('tg', ''))))
        if not words:
            continue
        considered += 1
        if len(words) < NEIGHBOUR_MIN_QUERY_WORDS:
            continue

        query = {token: idf.get(token, 1.0) for token in words}
        norm = math.sqrt(sum(value * value for value in query.values())) or 1.0
        similarity = {}
        for token, weight in query.items():
            for position in index.get(token, ()):
                similarity[position] = similarity.get(position, 0.0) + (weight / norm) * vectors[position].get(token, 0.0)
        if not similarity:
            continue
        best = sorted(similarity.items(), key=lambda item: item[1], reverse=True)[:NEIGHBOUR_NEIGHBOURS]
        if best[0][1] < NEIGHBOUR_MIN_COSINE:
            continue
        # Two of this listing's own words have to be in the winning neighbour: one shared
        # word is a coincidence, two is a subject. And one of them has to be distinctive -
        # rare in the labelled rows and long enough to be a name - because words like
        # "grand" or "national" are shared by a race card and a symphony and mean neither.
        shared_with_best = words & set(doc_tokens[best[0][0]])
        if len(shared_with_best) < NEIGHBOUR_MIN_SHARED_WORDS:
            continue
        if not any(document_frequency.get(token, 0) <= NEIGHBOUR_DISTINCTIVE_DF and len(token) >= 5
                   for token in shared_with_best):
            continue
        # Almost all of what this listing says has to be in that neighbour. "ROBLOX animal
        # hospital" sharing "hospital" with one labelled row is not a subject, it is the
        # word hospital.
        coverage = sum(query.get(token, 0.0) for token in shared_with_best)
        total_query = sum(query.values()) or 1.0
        if coverage / total_query < NEIGHBOUR_MIN_COVERAGE:
            continue

        weight = {}
        support = {}
        for position, closeness in best:
            if closeness < NEIGHBOUR_MIN_SUPPORT_COSINE:
                continue
            genre = labelled[position]['g']['n']
            weight[genre] = weight.get(genre, 0.0) + closeness
            support.setdefault(genre, set()).add(position)
        if not weight:
            continue
        ordered = sorted(weight.items(), key=lambda item: item[1], reverse=True)
        top_genre, top_weight = ordered[0]
        runner = ordered[1][1] if len(ordered) > 1 else 0.0
        if len(support[top_genre]) < NEIGHBOUR_MIN_SUPPORT:
            continue
        share = top_weight / (top_weight + runner) if (top_weight + runner) else 0.0
        # Clearly closer than the alternatives, not merely first.
        if share < NEIGHBOUR_MIN_SHARE or (runner and top_weight < NEIGHBOUR_MIN_MARGIN * runner):
            continue

        terms = []
        for position in support[top_genre]:
            for token in words & set(doc_tokens[position]):
                if token not in terms:
                    terms.append(token)
        terms.sort(key=lambda token: idf.get(token, 1.0), reverse=True)
        reading = [[top_genre, round(min(0.9, share), 2), terms[:3]]]
        # A second reading when another genre has its own support: the page shows two, and
        # two readings that disagree are more honest than one that hides the disagreement.
        if len(ordered) > 1:
            other_genre, other_weight = ordered[1]
            if len(support[other_genre]) >= NEIGHBOUR_MIN_SUPPORT:
                other_share = other_weight / (top_weight + other_weight)
                reading.append([other_genre, round(min(0.9, other_share), 2), terms[:3]])
        row['w'] = reading
        given += 1
    return given, considered


def main():
    os.makedirs(os.path.dirname(SNAPSHOT_DB), exist_ok=True)
    os.makedirs(os.path.dirname(STATIC_JSON), exist_ok=True)

    # A live database holding nothing is a reset, not a library. The committed snapshot is
    # still the index the site serves, and relabelling it - a new genre model, a new pass
    # over the words - must not need a scraper to have run first.
    from_snapshot = count_videos(LIVE_DB) == 0
    if from_snapshot:
        if count_videos(SNAPSHOT_DB) == 0:
            raise SystemExit("Neither the live database nor the snapshot holds any videos: %s" % LIVE_DB)
        print("live database is empty, relabelling the committed snapshot instead")

    if from_snapshot:
        snapshot = sqlite3.connect(SNAPSHOT_DB)
    else:
        # Checked before the backup, because the backup is what overwrites the snapshot.
        reason = shrink_refusal(count_videos(LIVE_DB), count_videos(SNAPSHOT_DB))
        if reason and "--allow-shrink" not in sys.argv[1:]:
            raise SystemExit(
                "%s. Refreshing from it would replace the index with a reset database, so "
                "nothing was written. Pass --allow-shrink if the drop is intended." % reason)

        # backup() copies a consistent database even while the app is running.
        live = sqlite3.connect(LIVE_DB)
        snapshot = sqlite3.connect(SNAPSHOT_DB)
        live.backup(snapshot)
        live.close()

    rows = snapshot.execute(
        "SELECT url, title, description, thumbnail, duration, source, click_count, tags FROM videos"
    ).fetchall()
    videos = [
        {"u": url, "t": plain_text(title), "d": plain_text(description), "th": thumbnail or "",
         "du": duration or "", "s": source or "", "c": clicks or 0, "tg": plain_text(tags)}
        for (url, title, description, thumbnail, duration, source, clicks, tags) in rows
    ]

    # Infer every genre here: this is the only place the model can run for a
    # static deployment, since the browser has no Python to ask.
    started = time.time()
    model = MusicGenreModel([f'{v["t"]} {v["d"]} {v["tg"]}'.lower() for v in videos])
    labelled = 0
    readings = 0
    for video in videos:
        guess = model.infer(video["t"], video["d"], video["tg"])
        if not guess:
            # Not a genre record, but the words can still lean somewhere. The page shows
            # those readings - "dubstep 0.4, techno 0.4: dark" - instead of printing
            # "unclassified" beside a title that clearly says something. Two candidates and
            # three words each: this repeats thousands of times, and the file is already
            # three megabytes.
            weak = model.rank(video["t"], video["d"], video["tg"])
            # The same refusal the model applies: a workshop page that says "metal" is not
            # heavy metal, and a reading on it is the page claiming a genre for a tutorial.
            if weak and model.is_music_listing(video["t"], video["d"], video["tg"]):
                readings += 1
                # One scale for both kinds of reading, and never 1.0: the number says how
                # much of the words a genre accounts for, not how sure anyone is. A single
                # candidate would otherwise print as a certainty.
                video["w"] = [[item["genre"], round(min(0.9, item["score"] / (item["score"] + 2.0)), 2),
                               item["terms"]] for item in weak[:2]]
            continue
        labelled += 1
        video["g"] = {
            "n": guess["genre"],
            "c": guess["confidence"],
            # Short keys: this repeats thousands of times in the file.
            "e": [[item["question"], item["answer"]] for item in guess["evidence"]],
            "a": guess["alternatives"][0]["genre"] if guess["alternatives"] else None,
        }
    neighbours, compared = neighbour_readings(videos, model)
    print("genres %d/%d in %.1fs, weak readings on %d more, neighbour readings on %d of %d" % (
        labelled, len(videos), time.time() - started, readings, neighbours, compared))
    searches = snapshot.execute("SELECT COUNT(*) FROM search_history").fetchone()[0]
    clicks = snapshot.execute("SELECT SUM(click_count) FROM videos").fetchone()[0] or 0
    snapshot.close()

    payload = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "count": len(videos),
        "searches": searches,
        "clicks": clicks,
        "videos": videos,
    }
    with open(STATIC_JSON, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, separators=(",", ":"), ensure_ascii=False)

    # A few hundred bytes the page reads at boot. The header's library figure came from
    # the server's ingest counter, which is seeded once and then only counts new rows, so
    # a re-export was always bigger than the number shown (8,067 against 8,724). What is
    # actually shipped now says so itself, without the 3 MB index being fetched first.
    # The keys are what the Worker compares against, and one video can be two rows, so the
    # count here is videos, not rows: the page and the database then agree on what they are
    # counting.
    seen_keys = sorted({video_key(video["u"]) for video in videos if video.get("u")})
    with open(os.path.join(ROOT, "public", "data", "library.json"), "w", encoding="utf-8") as handle:
        json.dump({
            "generated": payload["generated"],
            "count": len(seen_keys),
            "rows": len(videos),
            "searches": searches,
            "clicks": clicks,
            "genres": sum(1 for video in videos if video.get("g")),
            "readings": sum(1 for video in videos if video.get("w")),
        }, handle, separators=(",", ":"))

    # The keys of every row this export ships, about a hundred kilobytes. The Worker reads
    # it once per isolate to tell a video the library already has from one it is adding,
    # so an ingest skips the first without fetching the 3 MB index on every search, and the
    # total counts a video once.
    with open(os.path.join(ROOT, "public", "data", "seen.json"), "w", encoding="utf-8") as handle:
        json.dump({"generated": payload["generated"], "count": len(seen_keys), "keys": seen_keys},
                  handle, separators=(",", ":"))

    print("videos %d | sources %d | searches %d | clicks %d"
          % (len(videos), len({v["s"] for v in videos}), searches, clicks))
    print("wrote %s (%.1f KB) and %s (%.1f KB)"
          % (os.path.relpath(SNAPSHOT_DB, ROOT), os.path.getsize(SNAPSHOT_DB) / 1024,
             os.path.relpath(STATIC_JSON, ROOT), os.path.getsize(STATIC_JSON) / 1024))


if __name__ == "__main__":
    main()
