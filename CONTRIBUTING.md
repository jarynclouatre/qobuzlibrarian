# Contributing

Thanks for taking the time. This file covers filing bugs, suggesting
features, and submitting PRs.

## Reporting bugs

Open an issue with:

- What you ran (CLI flags, the URL/album you fed it, or which web page).
- What happened, what you expected.
- A relevant chunk of the log. The fetch log lives at `/data/.qobuz_fetch_log.json`
  inside the container; the Docker log (`docker compose logs qobuz-librarian`)
  is also useful.
- Container or pipx install, and your `docker compose version` / Python version.

Don't paste your `auth_token`. It's in `/config/streamrip/config.toml`; redact
it before sharing logs.

## Suggesting a feature

A short issue describing the workflow you want is enough. The project is
opinionated about staying focused on **lossless gap-fill + library
maintenance**, so things that move it toward "general music app" (playlist
management, tag editor UI, transcoding to lossy, multi-service downloads)
are likely out of scope.

## Pull requests

1. Fork, branch from `main`.
2. Install dev deps: `pip install -e ".[test]"`.
3. Add or update tests. Ours run with `python -m pytest -q` — they don't
   touch the network, beets, or streamrip; everything is mocked. Keep it
   that way.
4. Run `ruff check src tests --fix` before pushing. The `[test]` extras
   bundle it.
5. CI (`.github/workflows/test.yml`) runs the same checks on push/PR.

## Dev notes

**Building the image locally.** Use `compose.dev.yaml`:

```bash
docker compose -f compose.yaml -f compose.dev.yaml up -d --build
```

On M-series Macs add `--platform linux/amd64` if you want the same image
as CI (the arm64 path is slower to build and may expose platform-specific
surprises):

```bash
docker buildx build --platform linux/amd64 -t qobuz-librarian:dev .
```

**Smoke test.** `scripts/smoke_test.sh` runs a lightweight end-to-end
check against a live container. Read the script header for required env
vars before running.

**Logo.** `scripts/make_logo.py` regenerates `assets/logo.png`. It
requires `Pillow`; run it outside the container.

**Test isolation caveat.** If `/music` exists on your dev machine, the
`conftest.py` fixture that redirects `DATA_DIR` won't redirect `MUSIC_ROOT`
— any test that resolves a path relative to `MUSIC_ROOT` may touch real
files. The safe workaround is to run tests in a fresh shell where
`QF_MUSIC_ROOT` is not set and no `/music` directory is present.

## Behavioral changes

If your change touches how albums are matched, how upgrades are gated, or
how files are moved, write a paragraph in the PR describing the old vs new
behaviour with at least one concrete example. The match/upgrade/move logic
has historically caused the worst real-world surprises and is worth
explaining out loud.

## Style

- Black-compatible formatting (we don't run black in CI — just stay close).
- Comments explain *why*, not *what*. The code can usually speak for itself.
- No new top-level dependencies without a strong reason. The Docker image
  is bigger than it needs to be already.

## Releases

Tagged releases trigger the Docker workflow. Versioning is plain SemVer.
