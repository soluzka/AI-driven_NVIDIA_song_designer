"""Line timing for a rendered song.

The page draws the words while the song plays and records them into the saved
video. Until now it guessed when they are sung: an equal slice of the clock per
line, then a slice weighted by the bars of each section. Those guesses drift,
which is what "the lyrics are not in sync" looks like.

This measures the timing instead, from the song's own voice. The acoustic model
is the one the service already runs: demucs (htdemucs), the same separator the
voice map uses, which leaves a stem with the singing and almost no band. The
words are then fitted to the sung phrases in that stem - a line may only begin
and end at a place where the singing starts or stops - and the syllables decide
how the phrases are shared out between lines.

No new network is trained here, and nothing new is downloaded: it is the
existing separator plus a deterministic fit. Exact per-word timestamps would
need a speech recogniser with word-level output; this is line-level and
measured rather than assumed.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

# 20 ms is short enough that a syllable boundary is visible and long enough that
# the envelope does not chase single samples.
HOP_SECONDS = 0.02
# A gap shorter than this is a breath inside a phrase, not between two lines.
MERGE_GAP_SECONDS = 0.12
# Anything shorter than this is a click, not singing.
MIN_PHRASE_SECONDS = 0.15
# A boundary moves to a phrase edge when one is this close; otherwise the
# syllable arithmetic stands. Snapping further would push words into the band.
SNAP_SECONDS = 0.4
# The smallest gap two lines may share, so a burst of short lines cannot invert.
MIN_LINE_SECONDS = 0.12


def separate_vocal(audio_path, work_dir):
    """The singing on its own, from the separator the service already has."""
    out_dir = os.path.join(work_dir, 'align-stems')
    os.makedirs(out_dir, exist_ok=True)
    command = [sys.executable, '-m', 'demucs', '--two-stems=vocals', '-n', 'htdemucs',
               '--out', out_dir, audio_path]
    print('align: separating ' + os.path.basename(audio_path), flush=True)
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError('demucs could not separate the song for alignment: '
                           + (result.stderr or result.stdout or '')[-300:])
    stem = os.path.splitext(os.path.basename(audio_path))[0]
    vocal = os.path.join(out_dir, 'htdemucs', stem, 'vocals.wav')
    if not os.path.exists(vocal):
        raise RuntimeError('demucs wrote no vocal stem to align against')
    return vocal


def _envelope(path, hop=HOP_SECONDS):
    import librosa
    import numpy as np

    samples, rate = librosa.load(path, sr=16000, mono=True)
    if samples.size == 0:
        raise RuntimeError('the vocal stem is empty')
    step = max(1, int(round(rate * hop)))
    frames = samples.size // step
    trimmed = samples[:frames * step].reshape(frames, step)
    rms = np.sqrt(np.mean(trimmed.astype('float64') ** 2, axis=1))
    return rms, hop, samples.size / float(rate)


def phrases_from(rms, hop, min_seconds=MIN_PHRASE_SECONDS, merge=MERGE_GAP_SECONDS):
    """Runs of frames where the voice is present, in order."""
    import numpy as np

    peak = float(np.max(rms)) if rms.size else 0.0
    if peak <= 1e-6:
        return []
    gate = max(0.004, peak * 0.14)
    loud = rms > gate
    runs = []
    start = None
    for index, value in enumerate(loud):
        if value and start is None:
            start = index
        elif not value and start is not None:
            runs.append([start * hop, index * hop])
            start = None
    if start is not None:
        runs.append([start * hop, len(loud) * hop])
    merged = []
    for run in runs:
        if merged and run[0] - merged[-1][1] < merge:
            merged[-1][1] = run[1]
        else:
            merged.append(list(run))
    return [run for run in merged if run[1] - run[0] >= min_seconds]


def syllables(line):
    """Enough of a count to weigh lines against each other."""
    word = str(line).strip().lower()
    if not word:
        return 1
    groups = 0
    previous = False
    for char in word:
        vowel = char in 'aeiouy'
        if vowel and not previous:
            groups += 1
        previous = vowel
    return max(1, groups)


def fit_lines(lines, phrases, seconds):
    """Place each line inside the sung phrases, by syllable weight.

    The word boundaries are computed on a "voiced timeline" that only contains
    the phrases, so a line cannot creep through an instrumental gap; each
    boundary is then moved onto a phrase edge when one is near, because a line
    in a lyric video starts when the singing does.
    """
    if not lines:
        return []
    if not phrases:
        # Nothing measurable: fall back to the plain split rather than refusing.
        total = max(1.0, float(seconds or 30.0))
        step = total / len(lines)
        return [{'start': round(i * step, 3), 'end': round((i + 1) * step, 3)}
                for i in range(len(lines))]

    weights = [syllables(line) for line in lines]
    total_weight = float(sum(weights)) or 1.0
    voiced_total = float(sum(to - start for start, to in phrases)) or 1.0

    def to_clock(position):
        """A point on the voiced timeline, as a time in the song."""
        left = max(0.0, min(position, voiced_total))
        for start, to in phrases:
            span = to - start
            if left <= span:
                return start + left
            left -= span
        return phrases[-1][1]

    edges = sorted(x for phrase in phrases for x in phrase)
    boundaries = []
    passed = 0.0
    for weight in weights[:-1]:
        passed += weight
        at = to_clock(voiced_total * (passed / total_weight))
        nearest = min(edges, key=lambda edge: abs(edge - at))
        boundaries.append(nearest if abs(nearest - at) <= SNAP_SECONDS else at)

    schedule = []
    cursor = phrases[0][0]
    for index, boundary in enumerate(boundaries + [phrases[-1][1]]):
        end = max(boundary, cursor + MIN_LINE_SECONDS)
        end = min(end, phrases[-1][1])
        schedule.append({'start': round(cursor, 3), 'end': round(end, 3)})
        cursor = end
    # A very short last phrase must not swallow the final line.
    if schedule and schedule[-1]['end'] <= schedule[-1]['start']:
        schedule[-1]['end'] = round(schedule[-1]['start'] + MIN_LINE_SECONDS, 3)
    return schedule


def align_lines(audio_path, lines, work_dir=None, separate=True):
    """Where each line is sung: [{start, end}] in seconds, one per line."""
    keep = work_dir or tempfile.mkdtemp(prefix='align-')
    owned = work_dir is None
    try:
        source = audio_path
        used = 'the song itself'
        if separate:
            try:
                source = separate_vocal(audio_path, keep)
                used = 'the separated vocal'
            except Exception as exc:  # noqa: BLE001 - the mix is a usable fallback
                print('align: separation failed, using the mix: %s' % exc, flush=True)
                source = audio_path
        rms, hop, seconds = _envelope(source)
        phrases = phrases_from(rms, hop)
        schedule = fit_lines(lines, phrases, seconds)
        return {
            'lines': schedule,
            'seconds': round(seconds, 3),
            'phrases': [[round(start, 3), round(to, 3)] for start, to in phrases],
            'source': used,
        }
    finally:
        if owned:
            shutil.rmtree(keep, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description='Time lyric lines against a rendered song.')
    parser.add_argument('audio', help='the song (wav, mp3, ...)')
    parser.add_argument('--lines', required=True,
                        help='JSON file holding the list of lines, or a plain text file with one per line')
    parser.add_argument('--no-separate', action='store_true',
                        help='skip the vocal separator and fit against the mix')
    args = parser.parse_args()

    with open(args.lines, 'r', encoding='utf-8') as handle:
        text = handle.read()
    try:
        lines = json.loads(text)
    except ValueError:
        lines = [line for line in text.splitlines() if line.strip()]
    lines = [str(line) for line in lines if str(line).strip()]

    result = align_lines(args.audio, lines, separate=not args.no_separate)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
