"""Install the voice service without ever leaving CPU torch behind.

Run it with the environment's own interpreter, from the project root:

    voice_service\\.venv\\Scripts\\python.exe voice_service\\install.py
    voice_service\\.venv\\Scripts\\python.exe voice_service\\install.py --check

Why this exists instead of a plain `pip install -r requirements.txt`:
`chatterbox-tts` pins `torch==2.6.0`, and PyPI's wheel for that version is the
**CPU** build. Installing the requirements normally therefore replaces a working
CUDA torch with one where `torch.cuda.is_available()` is False - silently, with no
error, and the service then takes minutes per phrase on the processor. This script
installs the requirements and then puts the `+cu126` wheels of the same version
back, so one command leaves the machine in the state it needs. Re-running it is
safe: it repairs that state if something else has broken it since.

The voice weights are not downloaded here; the model fetches them from Hugging
Face on the first request, into the usual cache.
"""

import importlib.util
import subprocess
import sys

# chatterbox's own pins: numpy 2.x breaks its audio helpers at import.
PACKAGES = [
    'chatterbox-tts==0.1.7',
    'numpy==1.26.4',
    # perth (the watermarker chatterbox instantiates) imports pkg_resources, which
    # setuptools 81+ removed. Without it the watermarker is None and the model
    # fails to load with "'NoneType' object is not callable".
    'setuptools<81',
    'soundfile',
    'flask',
    'flask-cors',
]

TORCH_VERSION = '2.6.0'
CUDA_INDEX = 'https://download.pytorch.org/whl/cu126'
TORCH_SPECS = [f'torch=={TORCH_VERSION}+cu126', f'torchaudio=={TORCH_VERSION}+cu126']


def run(command, label):
    print(f'\n== {label} ==')
    print('+', ' '.join(command))
    result = subprocess.run(command)
    if result.returncode != 0:
        raise SystemExit(f'failed: {label}')
    return result.returncode == 0


def check():
    """Report what is installed, and whether a card is actually usable."""
    print('== packages ==')
    ok = True
    for label, module in (('chatterbox', 'chatterbox'), ('numpy', 'numpy'),
                          ('soundfile', 'soundfile'), ('flask', 'flask'),
                          ('flask-cors', 'flask_cors'), ('torch', 'torch')):
        found = importlib.util.find_spec(module) is not None
        print(f'  {"ok      " if found else "MISSING "} {label}')
        ok = ok and found
    if not ok:
        print('\nRun this script without --check to install what is missing.')
        return 1

    import torch

    cuda = torch.cuda.is_available()
    print(f'\n  torch {torch.__version__}')
    print(f'  cuda  {cuda}' + (f'  ({torch.cuda.get_device_name(0)})' if cuda else ''))
    if not cuda:
        print('\nThis is the CPU build: renders will take minutes instead of seconds.')
        print(f'Fix it with the same script (no --check), which installs {CUDA_INDEX}.')
        return 1
    print('\nReady. The launcher starts it on 127.0.0.1:8095; the weights download on'
          '\nthe first "Sing the lyrics".')
    return 0


def main(argv):
    if '--check' in argv:
        return check()

    print(f'interpreter: {sys.executable}')
    if not sys.executable.lower().endswith('python.exe'):
        print('  (not a venv python: this installs into whatever runs it)')

    run([sys.executable, '-m', 'pip', 'install', '--upgrade', 'pip'], 'pip')
    run([sys.executable, '-m', 'pip', 'install', *PACKAGES], 'voice packages')
    # Last, so nothing above can undo it: the +cu126 wheels of chatterbox's own pin.
    run([sys.executable, '-m', 'pip', 'install', '--force-reinstall',
         '--index-url', CUDA_INDEX, *TORCH_SPECS], 'CUDA torch')
    return check()


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
