# Contributing

Thanks for wanting to help. This repo is a local-first video search engine with
an ML index, a voice assistant, a song renderer on its own GPU service, and a
static site on Cloudflare Workers. It is maintained on Windows, so the commands
below are written the way they are actually run here.

## Before you start

- Open an issue first for anything larger than a typo or a small fix. Feature
  ideas have a template: **Feature request** when you click *New issue*.
- Bug reports are most useful with the command you ran, what it printed, and the
  file or number that was wrong.
- This project is MIT licensed, so anything you contribute lands under the same
  terms.

## Getting set up

The web app and the song service use **separate** environments on purpose: the
song service's CUDA wheels must not leak into Flask.

### The web app

```
uv venv .venv --python 3.11 --seed
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python app.py
```

Then open <http://127.0.0.1:5001>. `PORT` overrides the port, except `PORT=0`,
which falls back to 5001.

Always install into a virtual environment. A half-installed or broken global
interpreter is what makes PyInstaller freeze an empty package, which shows up as
`cannot import name 'emit' from 'flask_socketio'` in the frozen app.

`public/` is plain HTML/CSS/JS with no build step — edit and reload. For the
local offline copy of the site:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\start_services.ps1
```

### The song service

```
uv venv song_service\.venv --python 3.11 --seed
song_service\.venv\Scripts\python song_service\install.py --check
song_service\.venv\Scripts\python song_service\install.py
```

Versions are declared in `song_service/models.json` and nowhere else. A bump is
an edit to that manifest, not a hand-run `pip install`. `--check` reports what is
present without changing anything; the bare invocation installs packages and
then the weights (~10 GB).

## Changing things

- **Python** is 3.11. Keep the Flask app's dependencies out of the song service's
  environment, and the other way round.
- **`public/`** has no build step and no bundler: plain files the Worker serves.
  `npm run vendor` is the only generated asset (the Socket.IO client).
- **The Worker** (`worker.js`, `wrangler.toml`) serves `public/`, `/api/transcribe`
  and the download proxy. If you change what it serves, the site needs a deploy.
- **Deployment numbers and docs** belong in the READMEs, not in commit messages.

## Numbers, not impressions

This project's documentation makes claims that can be checked, and contributors
are expected to keep it that way:

- If you change audio, ranking or search behaviour and you want to describe the
  result, **measure it**. Say which file, which command and what the number was.
  "Sounds cleaner" and "feels faster" are not results.
- Every number in a README must come from a run on a file that still exists in
  the repo. If the evidence is deleted, delete or re-measure the claim.
- The measurement tools live in `build/measure/`. Run one, paste its output, and
  keep the transcript if the README quotes it.

## Checking your change

There is no test suite in the repo root; verify with the smallest check that
covers what you touched, and say what you ran:

| You changed | Run |
| --- | --- |
| Python source | `.venv\Scripts\python -m py_compile <file>`, then start the app and hit the route |
| The song service | `song_service\.venv\Scripts\python song_service\install.py --check` |
| `public/` | load the page — `tools\start_services.ps1`, or `npm run preview` |
| The Worker | `npm run preview`, then `build/wr.ps1 deploy` |
| Audio / analysis | the relevant script in `build/measure/` |

`build\wr.ps1 deploy` measures the page it just published: `tools\check_fit_low_end.py`
runs the voice-fit test tone through the deployed `fitRender` and checks 80–120 Hz
across all 14 profiles, failing if any of them rises. Use `--no-check` only when
you know why you are skipping it; `--config` deploys the pool worker and never
runs the check. A deploy is needed only when the served files changed — README
and `song_service/` edits are not part of the site.

## Commits and pull requests

- One topic per commit. Write the message as what changed and why, in the
  imperative: `Measure the whole song in the README, on a render that exists.`
- Keep URLs, tokens and personal paths out of commits. Never commit
  `build/song_endpoint.key`, logs or rendered audio — `.gitignore` already covers
  the known ones; do not weaken those rules to make a file fit.
- In a pull request, describe the change, the check you ran and its output, and
  anything you deliberately did not test. Untested and unmeasured is fine to
  admit; claiming a result you did not measure is not.

## Reporting conduct issues

Please read [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md). Report unacceptable
behaviour to the maintainer at bpiersing@outlook.com.
