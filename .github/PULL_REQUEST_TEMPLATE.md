## What this changes

<!-- One or two sentences. What is different after this pull request? -->

## Why

<!-- The problem this solves, or the issue it closes. "Closes #123" links it. -->

## Which part does it touch?

- [ ] Video search / scraping
- [ ] Ranking / ML scoring
- [ ] Voice assistant
- [ ] Song renderer (`song_service/`)
- [ ] Web UI (`public/`)
- [ ] Worker / deployment (`worker.js`, `wrangler.toml`)
- [ ] Build, tooling or measurement scripts (`build/`)
- [ ] Documentation or repository files

## The check I ran

<!-- The exact command, and what it printed. See "Checking your change" in CONTRIBUTING.md. -->

```
$ 
```

- [ ] I ran the command above and it passed
- [ ] It changes audio, ranking or search output, and here are the numbers:

| what was measured | before | after |
| --- | --- | --- |
|  |  |  |

- [ ] It downloads nothing at runtime that is not already documented

## Does the site need deploying?

<!-- Only served files matter: public/, worker.js, wrangler.toml. README and song_service/ edits are not part of the site. -->

- [ ] No — nothing the Worker serves changed
- [ ] Yes — and I ran `build\wr.ps1 deploy`, whose low-end check measured:

```
$ 
```

## What I did not verify

<!-- Be specific. Untested is fine to admit; a result you did not measure is not. -->
