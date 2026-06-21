<p align="center">
  <img src="assets/logo.png" alt="Qobuz Librarian" width="520">
</p>

<p align="center"><em>Build and maintain a complete, lossless library from Qobuz.</em></p>

<p align="center">
  <a href="https://github.com/jarynclouatre/qobuz-librarian/actions/workflows/test.yml"><img src="https://github.com/jarynclouatre/qobuz-librarian/actions/workflows/test.yml/badge.svg" alt="Tests"></a>
  <a href="https://github.com/jarynclouatre/qobuz-librarian/actions/workflows/docker.yml"><img src="https://github.com/jarynclouatre/qobuz-librarian/actions/workflows/docker.yml/badge.svg" alt="Docker"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/python-3.12+-blue.svg" alt="Python 3.12+">
</p>

Qobuz Librarian downloads music from Qobuz — a single album, an artist's whole catalogue, or a sweep of your entire library — and imports it cleanly with [beets](https://beets.io/). It wraps [streamrip](https://github.com/nathom/streamrip) for the downloading and adds the part that's tedious by hand: tracking what you already own, so it only fetches what's missing. Web UI or CLI.

<p align="center">
  <img src="assets/screenshot-dashboard.png" alt="Web UI dashboard" width="800">
</p>

## Contents

- [Features](#features)
- [How it works](#how-it-works)
- [Quick start (Docker)](#quick-start-docker)
- [Configuration](#configuration)
- [Existing libraries](#existing-libraries)
- [What it changes on its own](#what-it-changes-on-its-own)
- [Permissions](#permissions-nas-and-shared-storage)
- [Using the CLI](#using-the-cli)
- [JSON API](#json-api)
- [Troubleshooting](#troubleshooting)
- [Security and deployment](#security-and-deployment)
- [Limitations](#limitations)
- [Development](#development)
- [License](#license)

## Features

**Gap-fill downloads.** Point it at an album, an artist, or your whole library and it fills what's missing. It matches Qobuz against your files in three layers — ISRC, then disc + title, then edition-stripped title — so "(Remastered)" and "(Deluxe)" variants don't re-download as duplicates. To keep a different edition *alongside* one you own, pick **Download this edition too** and it imports into its own folder.

**Single tracks.** Switch search from **Albums** to **Tracks** to grab one song. It's filed under its album but flagged as a deliberate single, so gap scans won't push you to complete the album and won't surface a singles-only artist's whole catalogue as "missing". Download the full album later and it graduates back automatically.

**Quality upgrades.** New downloads arrive at the best quality Qobuz serves for the release. **Upgrade** mode re-rips albums Qobuz can now serve better, backing up the originals first and refusing any swap that would drop bonus tracks you have.

**Downsample.** **Downsample** mode shrinks hi-res FLACs (above CD rate) down to 44.1 / 48 kHz to reclaim space. Still lossless FLAC; every file is verified to decode before it replaces the original, and it pre-selects nothing.

**New-release checks.** A periodic pass compares each artist's current Qobuz catalogue against what you've already seen and surfaces only genuinely new albums you don't own, pre-ticked. It reads the catalogue listing alone — about one Qobuz call per artist — so it's quick.

**Clean import.** beets handles tagging and cover art, and files land in your library in a single move, so a scanner never catches a half-processed album. Synced lyrics are fetched on import (`LYRICS_ENABLED`, default on); **Lyrics** mode backfills tracks already in your library.

**Library maintenance.**

- ISRC-anchored repair finds truncated or short FLACs and refills the exact missing tracks, leaving good files alone. Every track's length is checked against its real Qobuz recording, so a file that plays fine but is cut short (or had its header rewritten to hide the truncation) is caught too — not just an obviously tiny one. It also flags FLACs that won't decode even without an ISRC tag or a Qobuz match, so a library brought from elsewhere still gets its corrupt files surfaced.
- Consolidates duplicate and sibling album folders; handles multi-artist and "Various Artists" layouts; edition-aware name matching and dedup.
- One-time **Migrate** of a messy or untagged collection into the `Artist/Album (Year)/` layout — tags first, optional AcoustID, copies by default.

**Crash-safe queue.** The download queue persists and resumes after a restart, and a per-run lock keeps two instances off the same library.

## How it works

A single Docker image bundles streamrip, beets, ffmpeg, and the FLAC tools — no sidecar containers. The web UI is the primary interface; the CLI runs the same engine for interactive, scripted, or unattended jobs. Paths and ports come from environment variables; everything else lives on the **Settings** page, applied live with no restart. The bundled tools' own config files sit in a persistent volume, so anything they support, you can set.

Every mode follows the same shape: **scan → review → download.** A scan runs in the background with its log streamed to the page, then parks a checklist of what it found. Nothing downloads or changes on disk until you tick what you want and approve.

| Page | What it does |
|---|---|
| **Search** | Find an album by name or Qobuz URL and download it |
| **Artist** | Scan one artist's discography; new releases flagged and pre-ticked |
| **Library** | Scan every artist for missing albums, or just check for new releases |
| **Upgrade** | Re-rip albums Qobuz can now serve at higher quality |
| **Downsample** | Shrink hi-res files to CD rate (a tab on Upgrade; local, no login) |
| **Repair** | Refill truncated or partial FLACs (ISRC-verified) |
| **Lyrics** | Fetch lyrics for tracks missing them (local, no login) |
| **Migrate** | Reorganise an existing library into the layout (copies, never touches originals) |
| **Queue** | Live progress, reviews awaiting approval, and download history |
| **Settings** | Qobuz credentials and behaviour toggles |

On **Library**, **Upgrade**, and **Downsample** scans you can *dismiss* albums you've decided against; they're remembered and skipped next time, so a big library can be triaged over several sessions. Restore them from the **Hidden** view. Dismissing is per album, so a new release by an already-reviewed artist still surfaces. The single-artist **Artist** scan never hides anything.

The CLI uses the same matching engine, so it finds the same gaps — it just walks them album by album with yes/no prompts instead of parking a checklist. Run it with no arguments for the menu, or pass flags for unattended runs (`--help` lists them).

The web app and CLI share one download lock, so only one runs at a time. To use the CLI without stopping the container, open **Settings → Mode → Hand off to terminal**, run your commands, then click **Resume web app**. Set `QL_CLI_ONLY=1` for a terminal-first box that always starts handed off (the web UI still serves browsing and Settings).

### Download quality

A fresh install downloads at tier `4` — the best Qobuz serves per release (24-bit up to 192 kHz, falling back to CD lossless when that's all a release has). Change it on **Settings** (live) or via `STREAMRIP_QUALITY`.

| Tier | Quality | Notes |
|---|---|---|
| `4` | 24-bit ≤192 kHz | Default; archival |
| `3` | 24-bit ≤96 kHz | Hi-res, smaller cap |
| `2` | 16-bit / 44.1 kHz | CD lossless, smallest |

To keep hi-res masters at a saner size, pull tier `4` and either enable import-time downsampling (`DOWNSAMPLE_HIRES_ENABLED`) or run **Downsample** on demand.

## Quick start (Docker)

```bash
mkdir qobuz-librarian && cd qobuz-librarian
curl -O https://raw.githubusercontent.com/jarynclouatre/qobuz-librarian/main/compose.yaml
curl -O https://raw.githubusercontent.com/jarynclouatre/qobuz-librarian/main/.env.example
cp .env.example .env
# edit .env — at minimum, point QL_MUSIC_DIR at your music folder
docker compose up -d
```

> **Point `QL_MUSIC_DIR` at a dedicated music library**, not your home folder or a drive with other files mixed in. The app moves and merges files within that tree, and Upgrade replaces files in place.

On Windows, run these in WSL or Git Bash — PowerShell's `curl` is an alias for `Invoke-WebRequest` with different flags, and `&&`-chained `cp`/`mkdir` don't behave the same.

`compose.yaml` pulls the prebuilt `latest` image from Docker Hub; see [Building from source](#building-from-source) to build it yourself.

> **On an untrusted or shared network, lock the box down before the first boot.** The default Compose publishes the port on all interfaces, and the first-visit setup screen stays open until an account exists — so whoever reaches the port first can claim the admin account. Seed `WEB_AUTH_USER` / `WEB_AUTH_PASSWORD` in `.env` before `docker compose up`, or bind the port to `127.0.0.1` (or keep it behind your own reverse proxy) until you've created the account. On a private home LAN the risk is low, but seeding the credentials up front is the safe default.

Open <http://localhost:8666>. The first visit prompts you to set a web username and password; sign in, then add your Qobuz token on **Settings**:

1. Paste your `user_auth_token` and click **Save & connect** (it's validated against Qobuz before saving).
2. Search for an album from the dashboard.
3. Click **Download** — the job page streams the import log.

### Login

The web UI requires sign-in out of the box. The password is stored as a salted PBKDF2 hash (`0600`, never plaintext) and the session is an `HttpOnly` cookie. Either set the credentials on the first-visit screen, or seed them with `WEB_AUTH_USER` / `WEB_AUTH_PASSWORD` in `.env` so the box comes up already locked down. Those two double as a password reset: change them and restart. To reset without env vars, stop the container, delete `.qobuz_web_auth.json` from the data volume, and set new credentials on the next visit.

Set `WEB_AUTH=none` to turn the login off — only sensible on a trusted LAN or behind your own authenticating reverse proxy. The container logs a warning every boot while it's off.

Behind a reverse proxy, set `FORWARDED_ALLOW_IPS` to the proxy's address so the failed-login throttle counts attempts per real client instead of treating every request as the single proxy IP (which lets one wrong-password run lock everyone out). Point it at your proxy, not `*`.

### Qobuz credentials

Auth is by **token**, not your password — it's the `password_or_token` field streamrip uses. You need a paid Qobuz account; this only downloads what your subscription already entitles you to.

Get the token from the [Qobuz web player](https://play.qobuz.com): sign in, open dev tools (F12), then Local Storage for `play.qobuz.com` (**Application** tab in Chrome/Edge, **Storage** tab in Firefox). Click the `localuser` entry and copy its `token` value (the `id` field in the same entry is your user id). Or, if you already run streamrip outside Docker, copy `password_or_token` from `~/.config/streamrip/config.toml`.

Paste it into **Auth Token** on Settings; "User ID / Email" takes either your account email or the numeric user id. Credentials stay in the container's config volume and go nowhere but Qobuz. To keep the token out of the environment (where `docker inspect` would show it), point `QOBUZ_USER_AUTH_TOKEN_FILE` at a file holding just the token — a [Docker secret](https://docs.docker.com/engine/swarm/secrets/) or read-only bind mount — instead of setting `QOBUZ_USER_AUTH_TOKEN`.

### Building from source

```bash
git clone https://github.com/jarynclouatre/qobuz-librarian.git
cd qobuz-librarian
cp .env.example .env
docker compose -f compose.yaml -f compose.dev.yaml up -d --build
```

## Configuration

Host paths and the web port come from a gitignored `.env` (copy `.env.example`). Docker Compose reads these on the **host** and maps them to the container-side names (`MUSIC_ROOT`, `STAGING_DIR`, …) the app reads.

| Variable | Default | Purpose |
|---|---|---|
| `QL_MUSIC_DIR` | `./music` | Music library; beets imports into this |
| `QL_STAGING_DIR` | `./staging` | Scratch space for in-progress downloads |
| `QL_UPGRADE_BACKUPS` | `./upgrade_backups` | Backups taken before a quality upgrade |
| `WEB_PORT` | `8666` | Host port for the web UI |

Everything else is a behaviour toggle, settable live on **Settings** or as a default in `compose.yaml`:

| Variable | Default | Purpose |
|---|---|---|
| `STREAMRIP_QUALITY` | `4` | Download tier 2–4 (see [Download quality](#download-quality)) |
| `LYRICS_ENABLED` | `true` | Fetch lyrics on import |
| `LYRICS_FORMAT` | `embed` | `embed` (FLAC tag), `sidecar` (.lrc), or `both` |
| `LYRICS_PROVIDERS` | *(auto)* | Ordered comma list, e.g. `Lrclib,NetEase` |
| `ARTWORK` | `sidecar` | Cover art: `sidecar`, `embed`, or `both` |
| `AUTO_LIBRARY_SCAN` | `true` | Run the one-time library scan on first launch |
| `NEW_RELEASE_CHECK_INTERVAL` | — | How often to auto-check for new releases (also set on Settings) |
| `ARTIST_CATALOG_CACHE_TTL` | — | How long artist album-lists stay cached |
| `AUTO_UPGRADE_ENABLED` | `false` | Surface upgrades during ordinary gap-fill walks |
| `DOWNSAMPLE_HIRES_ENABLED` | `false` | Downsample hi-res FLACs as they download (see below) |
| `UPGRADE_SINGLES_ENABLED` | `false` | Let the Upgrade walk re-rip tracks you pulled as singles |
| `MIGRATE_MULTI_ARTIST` | `false` | Re-file `A, B/Album` under `A/Album` after import |
| `CONSOLIDATE` | `false` | Merge sibling/duplicate album folders (CLI-only) |

`DOWNSAMPLE_HIRES_ENABLED` only touches *new* downloads (88.2 / 176.4 / 352.8 kHz → 44.1; 96 / 192 / 384 → 48; bit depth preserved; originals replaced atomically, so an interrupt can't corrupt a track). To shrink hi-res already in your library, use the on-demand **Downsample** mode instead.

Advanced thresholds — fuzzy-match cutoffs, `ARTIST_SCAN_WORKERS`, `ARTIST_API_DELAY`, retention windows, `POST_JOB_HOOK` — live in the `compose.yaml` env block, each with a working default; see `src/qobuz_librarian/config.py` for what each does. `POST_JOB_HOOK` runs in a shell, so only set it to a command you trust.

### beets & streamrip config

The bundled tools' full config files live in the persistent `config` volume, seeded once and **never overwritten**:

- `…/beets/config.yaml` — tagging, paths, plugins ([beets docs](https://beets.readthedocs.io/))
- `…/streamrip/config.toml` — downloader settings ([streamrip docs](https://github.com/nathom/streamrip))

For folder/file naming without hand-editing YAML, uncomment `BEETS_PATH_DEFAULT` / `BEETS_PATH_SINGLETON` / `BEETS_PATH_COMP` in `compose.yaml` (beets path syntax, e.g. `$albumartist/$album ($year)/$track - $title`). Set which plugins load with `BEETS_PLUGINS` (comma list); the seeded config enables `fetchart` and `inline` — keep `inline`, it backs the multi-disc folder field. Plugins that need their own config block (lastgenre API key, replaygain backend) still require an edit to `config.yaml`.

The downloader pins four beets settings for its own imports, regardless of your config: `autotag: no` (keep Qobuz's tags), `move: yes` (clear staging cleanly, even when `/music` and `/staging` are on different filesystems), `incremental: no` (rescan on retry), and `duplicate_action: merge` (gap-fill into the existing album folder, and never delete your files). Your own `beet` commands read your config unchanged, and paths, plugins, and artwork always come from your config. Edits apply on the next import — no restart.

## Existing libraries

The defaults assume a fresh library. For a collection you already have — beets-managed or just well-organised — here's the layout it expects and how to bring one in.

### Folder layout

The scanner expects a two-level tree, `$MUSIC_ROOT/<Artist>/<Album>/`:

```
$MUSIC_ROOT/
├── Artist Name/
│   ├── Album Name/
│   │   └── 01 - Track.flac
│   └── Album (2017)/
│       ├── CD1/
│       └── CD2/
└── Other Artist/
    └── 2017 - Album/
        └── 01 - Track.flac
```

The album folder *name* is flexible — `Album`, `Album (2017)`, `Album [2017]`, `2017 - Album` all work; a year is used when present but isn't required, since matching is driven by track tags, not folder names. Per-disc subdirs (`CD1/`, `CD2/`) are recursed into; hidden directories and the staging dir are skipped. It won't detect flat (`/music/<track>.flac`) or extra-nested (`/music/<Genre>/<Artist>/…`) layouts — point `QL_MUSIC_DIR` at the artist-level directory.

### Migrating into the layout

If your collection *isn't* in `Artist/Album (Year)/` shape — flat folders, inconsistent names, half-tagged files — the **Migrate** tool builds a tidy copy in the expected layout from what your files already say. It's a one-time step, separate from downloading, and needs no Qobuz login.

It places each file by tags first (album artist, album, title, track, disc) — fast and entirely offline. For files whose tags can't place them, an optional AcoustID fingerprint pass identifies them by sound; it's slower and needs network access, so it's off by default. No API key is needed — lookups are free.

It copies by default, so your originals are only ever read and the organised library is built at a separate destination. It previews the full plan first — where each file goes, what it couldn't place, and the space the copy needs against what's free — then waits for you to confirm. Files it can't confidently identify are left in place and listed, never moved into a wrong guess, never deleted. Two CSVs land at the destination: `migration-manifest.csv` (the full plan, including everything left behind and why) and `migration-results.csv` (what the confirmed run copied, skipped, or failed on).

Point it at two folders — the messy source (read-only) and an empty destination — in `.env` / `compose.yaml`:

```yaml
services:
  qobuz-librarian:
    environment:
      QL_MIGRATE_SRC: /old-library
      QL_MIGRATE_DEST: /organised
    volumes:
      - /path/to/your/messy/library:/old-library:ro   # :ro = read-only
      - /path/to/new/empty/folder:/organised
```

Then either:

- **Web:** open **Migrate**, optionally tick *Fingerprint files my tags can't place*, click **Preview migration**, review the per-artist list, then **Copy N selected** (reads **Move N selected** in move mode).
- **CLI:** `docker compose run --rm qobuz-librarian cli --migrate` — add `--acoustid` for fingerprinting, `--in-place` to move instead of copy (relocates files and prunes the folders it empties), or `--dry-run` to preview. Source/destination come from the env vars above or `--migrate-src` / `--migrate-dest`.

When it's done, point `QL_MUSIC_DIR` at the new destination and run a Library scan.

Worth knowing before you adopt the result: AcoustID matches are a best guess, so review them. A compilation with *no* signal (no compilation flag, no "Various Artists" album artist, no disc numbers) can't be recognised as one — each track lands under its own track-artist and scatters across folders, so check those by hand. The year comes from tags only, so a file tagged without one lands in `Artist/Album/` rather than `Artist/Album (Year)/`. It's a copy, so the worst case is wasted disk — spot-check, and delete and re-run if you don't like it.

### Bringing an existing beets database

The container creates `/config/beets/musiclibrary.db` on first start if none exists. To use yours instead, stop the container and copy your DB and config into the `qobuz-librarian-config` volume (the DB must end up named `musiclibrary.db`):

```bash
docker run --rm -v qobuz-librarian-config:/dest -v /your/beets/dir:/src alpine \
  sh -c 'mkdir -p /dest/beets && cp /src/config.yaml /dest/beets/ && \
         cp /src/library.db /dest/beets/musiclibrary.db'
```

Replace `library.db` with your filename (check the `library:` path in your `config.yaml` if unsure), or bind-mount a host directory at `/config/beets` instead. The container won't overwrite either file on start.

### Tagging an untagged collection

Qobuz downloads arrive fully tagged, so import leaves the autotagger off. For older untagged files you already have, beets' `chroma` plugin can identify them by audio fingerprint (AcoustID) and tag them **in place** — nothing is moved, copied, or deleted (`fpcalc` ships in the image). A ready-made config keeps it separate from your normal beets settings:

```bash
docker compose run --rm -e BEETSDIR=/config/beets qobuz-librarian \
  beet -c /app/docker/beets-chroma.yaml import /music/<your-untagged-folder>
```

It shows the matching MusicBrainz releases one album at a time, for you to accept, skip, or replace. Lookups use beets' built-in AcoustID key; you only need your own (from <https://acoustid.org/new-application>, added as `acoustid: {apikey: "KEY"}`) to *submit* fingerprints with `beet submit`. Run it when no download is active — both use the same beets database. To also re-folder the files into the layout, use [Migrate](#migrating-into-the-layout) instead, which does both.

### First scan on a big library

A library-wide scan makes roughly one Qobuz call per artist directory (cached on re-scans, so repeats are mostly free), fanned across a few artists at once (`ARTIST_SCAN_WORKERS`, default 4). There's no artificial delay between calls (`ARTIST_API_DELAY`, default 0) — Qobuz's rate limit is handled by automatic retry/back-off, so raise it only if you get throttled. It's scan-then-review, not a daemon: re-run it whenever you've added music. Singles and very short EPs are hidden from the missing-albums step by default; raise `MISSING_ALBUMS_MIN_TRACKS` or pass `--include-singles` to surface them.

## What it changes on its own

Scans run on their own, but only to *show* you things. On first launch it runs a one-time library scan (`AUTO_LIBRARY_SCAN`); after that it periodically checks for new releases (`NEW_RELEASE_CHECK_INTERVAL`, also on Settings). Both only read Qobuz and park a review list — nothing downloads or changes a file until you approve it.

- **Gap-fill only ever adds** missing tracks — it never deletes or rewrites one.
- **Upgrade** and **Downsample** replace files, but only when you start them. Upgrade backs up the originals first (`UPGRADE_BACKUP_RETENTION_DAYS`); Downsample rewrites in place with no backup, so it verifies every file decodes before replacing and pre-selects nothing.
- **Lyrics** writes lyric tags or `.lrc` sidecars into existing tracks, never the audio — on import (`LYRICS_ENABLED`) and via the on-demand Lyrics mode.
- **Consolidation** (`CONSOLIDATE`, off) merges duplicate album folders. It's CLI-only — it needs per-folder confirmation the web UI has no screen for, so web downloads always skip it regardless of the setting.
- **`MIGRATE_MULTI_ARTIST`** (off) re-files `A, B/Album` under `A/Album` after import; off keeps your paths stable across scans.

## Permissions (NAS and shared storage)

The container runs as `PUID:PGID` (default `1000:1000`), so downloads are owned by a normal user rather than root. If your host or media-share owner isn't `1000`, set them in `.env` — they flow straight into the container, no `compose.yaml` edit needed:

```bash
PUID=1000   # id -u
PGID=1000   # id -g
```

On boot it chowns the app-managed volumes (`config`, `data`, `staging`, `upgrade_backups`) to that user and warns if a mounted path isn't writable. `/music` is deliberately left alone — it's often a large NAS mount — so make sure the run user can write to it. If your music share is read-only, append `:ro` to the `/music` bind; scans and upgrade detection still work, but `QL_CHECK_VOLUMES=1` will then return 503 on scan endpoints (the write test fails), so leave that flag unset in that case. To run as root, set `PUID=0` / `PGID=0`.

If the bind dirs were auto-created as root on first `up`, chown them before enabling those settings, or you'll get the "volume not writable" warning every boot:

```bash
sudo chown -R 1000:1000 ./music ./staging ./upgrade_backups
```

## Using the CLI

The CLI runs inside the same container as the web UI — no separate install. It shares the download lock (see [How it works](#how-it-works)), so free it first: hand it over from **Settings → Mode → Hand off to terminal**, or stop the web container with `docker compose stop qobuz-librarian` and `start` it after.

Interactive menu:

```bash
docker compose run --rm -it qobuz-librarian cli
```

Common unattended forms:

```bash
# Download a specific album (URL or "Artist Album" string)
docker compose run --rm qobuz-librarian cli https://open.qobuz.com/album/abcd1234

# Work through one artist's catalogue (--include-singles and/or
# --include-comps to also offer singles and compilation appearances)
docker compose run --rm qobuz-librarian cli --artist "Stars of the Lid"

# Sweep every artist for quality upgrades, auto-confirming the safe ones
docker compose run --rm qobuz-librarian cli --upgrade-walk --auto-safe

# Preview which hi-res library files would shrink to CD rate (changes nothing)
docker compose run --rm qobuz-librarian cli --downsample-walk --dry-run

# Fetch lyrics for tracks missing them (--lyrics-synced-only for timed
# lyrics only; --lyrics-rescan to re-query tracks already checked)
docker compose run --rm qobuz-librarian cli --lyrics-walk

# Start the next walk fresh, revisiting artists you've already reviewed
docker compose run --rm qobuz-librarian cli --reset-walk-seen

# Full flag reference
docker compose run --rm qobuz-librarian cli --help
```

The CLI honours the same `.env` and `compose.yaml` settings as the web UI.

## JSON API

The web server exposes a small read-only JSON API — useful for home-automation dashboards, monitoring, or shell scripts that want job state without scraping HTML.

| Endpoint | Description |
|---|---|
| `GET /api/jobs` | All jobs, most recent first. Optional `?status=` (`pending`, `running`, `scanning`, `awaiting_review`, `done`, `failed`, `canceled`) and `?limit=N` (max 500, default 50). Returns `{"jobs": [...], "count": N}`. |
| `GET /api/jobs/{id}/status` | One job as JSON — list fields plus a `log_lines` array (last 50 lines). 404 if not found. |
| `GET /api/jobs/{id}/stream` | SSE stream for a live job: each log line is a `message` event, a `done` event signals completion. `progress` events and `: ping` keepalives may also appear. |

With login enabled, all endpoints need the session cookie; a missing or invalid one returns `401 {"detail": "authentication required"}` rather than a redirect, so non-browser clients can detect the auth gate cleanly.

```bash
# Is anything currently downloading?
curl -s -b 'qf_session=<your-cookie>' http://localhost:8666/api/jobs?status=running | jq '.count'
```

## Troubleshooting

| Symptom | Likely cause / next step |
|---|---|
| `Another Qobuz Librarian run is in progress` | The web container holds the lock — use the web UI, or `docker compose stop qobuz-librarian` for the CLI. |
| `MUSIC_ROOT missing or inaccessible` | Bind mount unset or wrong — check `QL_MUSIC_DIR` in `.env` and that the host path exists. |
| Container exits immediately on `up` | `.env` missing from the compose dir, or a host bind-mount path doesn't exist. `docker compose logs qobuz-librarian` shows which. |
| `Volume not writable` (Settings → Diagnostics: FAIL) | `PUID`/`PGID` don't match the host owner — `chown -R $(id -u):$(id -g) ./music ./staging` or set them in `.env`. |
| Library scan says "no artist folders found" | `/music` is mounted at an empty or one-level-off directory — `QL_MUSIC_DIR` must point at the artist-level folder, not its parent. |
| Token rejected (Save & connect) | Expired, copied with quotes, or trailing whitespace — re-grab it from play.qobuz.com (dev tools → Local Storage → `localuser` → `token`) and paste clean. |
| Stalls in "Importing into beets…" | A beets plugin is loaded without its required config block (lastgenre key, replaygain backend) — disable it via `BEETS_PLUGINS` or add the block to `config.yaml`. |
| `docker compose pull` 404 | Image not published under that tag yet — [build from source](#building-from-source). |
| Healthcheck failing but port reachable | Container couldn't reach its own `/healthz` — check resource limits and `docker logs qobuz-librarian`. |
| Upgrade fails with `Permission denied` backing up an album | An earlier `docker exec … beet …` ran as root, leaving root-owned files PUID 1000 can't move — rerun with `docker exec --user 1000:1000 …`, or `sudo chown -R 1000:1000 ./music`. |
| Files vanished from `/music` after a manual `beet` command | `beet -d /config/beets …` reads `-d` as the *destination*, so with `move: yes` it relocates the library into the config volume. The container already exports `BEETSDIR`; run `beet …` with no `-d`. |

## Security and deployment

The bundled `compose.yaml` ships hardened: `mem_limit: 1g` and `pids_limit: 256` (a runaway streamrip child can't exhaust the host), `no-new-privileges`, `cap_drop: [ALL]` (adding back only the few caps gosu needs for the PUID/PGID handover), and `0600` token files. It's a multi-arch image (`linux/amd64`, `linux/arm64`), so arm64 NAS boxes run natively. `--read-only` rootfs works with `--tmpfs /tmp` (or `APP_HOME=/var/tmp` with `--tmpfs /var/tmp`).

The login (see [Login](#login)) is a single shared credential with per-IP brute-force limiting — 5 failures an hour before a 429. It's enough for a trusted network, but for internet exposure put it behind an authenticating reverse proxy, VPN, or Tailscale rather than relying on the built-in login alone. See [SECURITY.md](SECURITY.md).

## Limitations

- **One library, one container.** `/staging` is single-writer. The run-lock keeps the CLI and web container from clashing in one compose stack, but two stacks against the same mount will collide.
- **Qobuz only.** This drives streamrip's Qobuz path specifically; Tidal/Deezer/SoundCloud aren't wired up here, even though streamrip itself can reach them.
- **No lossy output.** `DOWNSAMPLE_HIRES_ENABLED` can downsample hi-res FLACs to 44.1 / 48 kHz (still FLAC); there's no path to MP3 or other lossy formats.
- **Latin metadata matches best.** Fuzzy matching uses case-fold + edit distance; CJK and right-to-left titles work but the thresholds were tuned on Latin scripts, and edition-variant stripping uses English keyword lists (e.g. "豪华版" is kept verbatim rather than stripped).
- **PWA install needs HTTPS.** The service-worker API only activates on HTTPS or `localhost` — front the container with a TLS-terminating proxy to install it as an app.
- **Windows console:** the CLI uses `·` (U+00B7) for progress lines; terminals without UTF-8 mode (`chcp 65001`) show a replacement character. It runs fine under WSL.

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate      # PowerShell: .venv\Scripts\Activate.ps1
pip install -e ".[test]"
python -m pytest -q
```

Running the web UI from a source checkout (rather than the Docker image) needs the CSS built once — `src/qobuz_librarian/web/static/dist/` is gitignored, so only the image build produces it:

```bash
npm ci && npm run build      # writes src/qobuz_librarian/web/static/dist/app.css
```

`ruff check src tests` runs in CI; keep it clean before opening a PR. `scripts/smoke_test.sh` builds the image, boots the container, checks the web routes respond, and confirms the bundled tools (rip/beet/ffmpeg/flac) are present — run it before tagging a release. See [CONTRIBUTING.md](CONTRIBUTING.md) for the rest.

## Acknowledgements

Qobuz Librarian is the glue around several open-source projects, bundled into the Docker image:

- **[streamrip](https://github.com/nathom/streamrip)** (nathom) — the Qobuz downloader. GPL-3.0.
- **[beets](https://beets.io/)** — tagging, cover art, library organisation. MIT.
- **[mutagen](https://github.com/quodlibet/mutagen)** — audio metadata reading/writing. GPL-2.0-or-later.
- **[FFmpeg](https://ffmpeg.org/)** — audio probing and transcoding. LGPL/GPL depending on build.
- **[FLAC](https://xiph.org/flac/)** (Xiph.Org) — `flac -t` integrity verification and `metaflac` header reads. BSD.

Lyrics are sourced via [syncedlyrics](https://github.com/moehmeni/syncedlyrics) (LRCLIB, NetEase, Musixmatch). The web UI uses [FastAPI](https://fastapi.tiangolo.com/), [htmx](https://htmx.org/), [Tailwind CSS](https://tailwindcss.com/), and [daisyUI](https://daisyui.com/). Thanks to all their maintainers.

## License

This project's own code is **MIT** — see [LICENSE](LICENSE).

The Docker image redistributes the third-party tools above, each under its own license. Two are copyleft and coupled differently: **streamrip (GPL-3.0)** is invoked as a separate program (a subprocess), not linked into this code, while **mutagen (GPL-2.0-or-later)** is imported as a Python library — linked in-process — for reading and writing FLAC tags. If you redistribute the image or a derivative, honour both projects' terms; see each project for authoritative license text.
