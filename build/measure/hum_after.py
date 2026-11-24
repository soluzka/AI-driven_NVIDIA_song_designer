"""Write what the shipped sub treatment makes of a render, so the hum can be measured
on the audio that actually ships rather than on its input.

`_fix_low_end` is lifted from song_service/app.py by name and executed from its source
(the same loader test_sub_fix.py uses), so the file written here is the service's own
output. Nothing else is applied - no room, no mix - which is what isolates the question
"is the low band still one held pitch?" from everything else in the chain.

Usage: hum_after.py <wav> [...]      (writes build/measure/after_<name>.wav)
"""
import os
import sys

import soundfile as sf

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from test_sub_fix import load_helper  # noqa: E402


def main():
    paths = sys.argv[1:]
    if not paths:
        raise SystemExit('usage: hum_after.py <wav> [...]')
    namespace = load_helper()
    for path in paths:
        data, rate = sf.read(path, always_2d=True, dtype='float32')
        after = namespace['_fix_low_end'](data.copy(), rate)
        name = os.path.basename(path).replace('.wav', '')
        out = os.path.join(HERE, 'after_%s.wav' % name)
        sf.write(out, after, rate, subtype='PCM_24')
        print('%s -> %s' % (os.path.basename(path), os.path.basename(out)))


if __name__ == '__main__':
    main()
