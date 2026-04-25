<p align="center">
  <img src="assets/logo.png" alt="Qobuz Librarian" width="520">
</p>

<p align="center"><em>Your Qobuz library, at full quality — and kept tidy.</em></p>

<p align="center">
  <a href="https://github.com/jarynclouatre/qobuz-librarian/actions/workflows/test.yml"><img src="https://github.com/jarynclouatre/qobuz-librarian/actions/workflows/test.yml/badge.svg" alt="Tests"></a>
  <a href="https://github.com/jarynclouatre/qobuz-librarian/actions/workflows/docker.yml"><img src="https://github.com/jarynclouatre/qobuz-librarian/actions/workflows/docker.yml/badge.svg" alt="Docker"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/python-3.12+-blue.svg" alt="Python 3.12+">
</p>

Qobuz Librarian downloads music from Qobuz — single albums, a whole artist
catalog, or a sweep of your entire library — and imports it cleanly with
[beets](https://beets.io/). It uses [streamrip](https://github.com/nathom/streamrip)
for the downloads and adds the part that's actually tedious by hand: knowing
what you already have so it only fetches what's missing. It runs from a web UI
or the CLI.

<p align="center">
  <img src="assets/screenshot-dashboard.png" alt="Web UI dashboard" width="800">
</p>

By default it pulls the **highest quality your subscription serves**. Want
smaller files instead? Drop to CD lossless or 320 kbps with one setting (see
[Download quality](#download-quality)).

## Contents

- [Features](#features)
- [How it ships](#how-it-ships)
- [How you use it](#how-you-use-it)
- [Quick start (Docker)](#quick-start-docker)
- [Pointing it at an existing library](#pointing-it-at-an-existing-library)
- [Migrating an existing library into the layout](#migrating-an-existing-library-into-the-layout)
- [Bringing your existing beets database](#bringing-your-existing-beets-database)
- [Tagging an untagged collection (AcoustID)](#tagging-an-untagged-collection-acoustid)
- [Default beets config](#default-beets-config)
- [First scan on a big library](#first-scan-on-a-big-library)
- [What it will and won't touch](#what-it-will-and-wont-touch-on-its-own)
- [Running on a NAS](#running-on-a-nas)
- [Using the CLI](#using-the-cli)
- [Troubleshooting](#troubleshooting)
- [Security and deployment shape](#security-and-deployment-shape)
- [Limitations](#limitations)
- [Development](#development)
- [Acknowledgements](#acknowledgements)
- [License](#license)

## Features

**Get music, without re-downloading what you own.** Point it at an album,
an artist, or your whole library and it fills the gaps. It compares Qobuz
against your files with a three-layer match so it won't grab duplicates of
tracks you already have under slightly different names:

1. ISRC (exact recording identity)
2. disc number + title
3. edition-stripped title (so "(Remastered)" / "(Deluxe)" don't cause dupes)

**Best available quality by default.** New downloads come in at the highest
quality Qobuz offers for that release. Already have an album in a lower quality? The
**Upgrade** mode finds everything Qobuz can now serve better and re-rips just
those.

**Catch new releases, without going looking.** A *Check for new releases* pass —
across your whole library, or one artist at a time — compares each artist's
current Qobuz catalogue against what you've already seen and surfaces only what's
genuinely new and you don't own, pre-ticked and ready to download. It reads the
catalogue listing alone (no per-track fetches), so it's quick — roughly one Qobuz
call per artist. Your normal gap scans keep their catalogue cache for speed; this
check is the always-fresh path, and it refreshes that cache as it runs.

**Clean import.** beets handles tagging and cover art; files land in your
library in one move so a scanner never sees a half-processed state. Synced
lyrics are fetched automatically (when `LYRICS_ENABLED` is on; default).

**Library maintenance.**

- Consolidates duplicate / sibling album folders into one canonical directory
- Handles multi-artist and "Various Artists" directory layouts
- Edition- and decoration-aware album-name matching and version dedup
- ISRC-anchored repair: finds truncated or short FLACs and refills the exact
  missing tracks, leaving good files untouched
- One-time migration of a messy or untagged collection into the
  `Artist/Album (Year)/` layout — tags first, optional AcoustID, copies by
  default so the originals are never touched

**Queue and safety.**

- Crash-safe persistent download queue that resumes after a restart
- Per-run lock so two instances can't fight over the same library

## How it ships

A single Docker image bundles streamrip, beets, and ffmpeg — no extra
containers. The web UI is the primary interface; the CLI runs the same engine
for hands-on interactive runs, scripting, and unattended jobs. Paths and ports
come from environment variables, behaviour from the Settings page. The bundled
tools' full config files live in a persistent volume, so you can change
anything they support.

## How you use it

Most things follow the same shape: **scan → review → download.** A scan runs
in the background (live log streamed to the page), then parks with a checklist
of what it found. You tick what you want and approve; nothing is downloaded or
changed until you do.

| Page        | What it does                                                       |
|-------------|--------------------------------------------------------------------|
| **Search**  | Find an album by name or Qobuz URL and download it                  |
| **Artist**  | Scan one artist's discography (always fresh; new releases flagged + pre-ticked), pick which to get |
| **Library** | Scan every artist for missing albums — or just *check for new releases* across the whole library |
| **Upgrade** | Find albums Qobuz can serve at higher quality, choose what to re-rip |
| **Repair**  | Find truncated/partial FLACs (ISRC-verified), choose what to refill |
| **Migrate** | One-time: reorganise an existing library into the layout (copies, never touches originals) |
| **Queue**   | Live progress, jobs awaiting review, and download history           |
| **Settings**| Qobuz credentials and behaviour toggles (applied without a restart) |

On the **Library** and **Upgrade** scans you can also *dismiss* albums you've
decided against — they're remembered and skipped on future scans, so a large
library can be triaged over days without re-reviewing the same things; restore
any of them from the **Hidden** view. The single-artist **Artist** scan never
hides anything, since typing a name is a deliberate request to see everything.

Dismissing is per *album*, not per artist, so a brand-new release by an artist
whose back catalogue you've already triaged still surfaces — the *Check for new
releases* pass (or the per-artist link in the **Hidden** view) brings it up
without un-hiding everything you dismissed.

The CLI runs the **same matching engine**, so it finds exactly the same missing
albums and track gaps the web does — it just works through them differently.
Instead of parking a checklist you review all at once, it walks you album by
album with yes/no prompts (skip, queue, fill, stop), which suits hands-on,
power-user runs. Launch with no arguments for the menu (Search · Artist ·
Library walk · Album gaps · Repair · Upgrade · Migrate), or pass flags for
unattended runs — `--help` lists them all.

**Web app and CLI take turns.** They share one download lock, so only one
can run at a time. To use the CLI without stopping the container, open
**Settings → Mode → Hand off to terminal**; the web UI pauses its downloads
and hands the lock over. Run your terminal commands
(`docker exec -it qobuz-librarian qobuz-librarian …`), then click **Resume web
app**. For a terminal-first box, set `QL_CLI_ONLY=1` so the container always
starts handed-off (the web UI still serves for browsing and Settings).

### Download quality

A fresh install downloads at quality `4` — the highest quality Qobuz serves for
each release (24-bit up to 192 kHz, falling back to CD-quality lossless when
that's all a release has). All four streamrip tiers are available:

| Tier | What you get                     | Good for                          |
|------|----------------------------------|-----------------------------------|
| `4`  | 24-bit ≤192 kHz                  | **Default.** Archival              |
| `3`  | 24-bit ≤96 kHz                   | Hi-res with a tighter size cap     |
| `2`  | CD-quality 16-bit / 44.1 kHz     | Smaller files, still lossless      |
| `1`  | 320 kbps lossy                   | Smallest, lossy                    |

Change it on the **Settings** page (live, no restart) or via
`STREAMRIP_QUALITY` in `compose.yaml`. To keep hi-res but not at full size,
see [downsampling](#optional-power-features) — pull the highest quality, then
downsample on import.

### Quality upgrades

Replacing files for higher quality is a separate **Upgrade** mode (CLI and
web): it backs up the originals first (with retention cleanup) and won't
replace an album if that would drop bonus tracks you have.

`AUTO_UPGRADE_ENABLED` (default off) controls only whether ordinary gap-fill
walks also surface upgrades along the way; the explicit Upgrade scan works
either way.

### Optional power features

Off by default because they change your files:

- **Downsample hi-res** (`DOWNSAMPLE_HIRES_ENABLED`): downsample high-sample-rate FLACs
  (88.2/176.4/352.8 kHz → 44.1, 96/192/384 kHz → 48; bit depth preserved) on
  import. Pairs with the hi-res default: grab the highest quality, then store
  it at a sane sample rate. Still lossless FLAC; originals are replaced
  atomically, so an interrupt can't corrupt a track.

## Quick start (Docker)

```bash
mkdir qobuz-librarian && cd qobuz-librarian
curl -O https://raw.githubusercontent.com/jarynclouatre/qobuz-librarian/main/compose.yaml
curl -O https://raw.githubusercontent.com/jarynclouatre/qobuz-librarian/main/.env.example
cp .env.example .env
# edit .env — at minimum point QL_MUSIC_DIR at your music folder
docker compose up -d
```

> **Point `QL_MUSIC_DIR` at a dedicated music library** — not your home folder or a
> drive with non-music files mixed in. Qobuz Librarian moves and merges files within
> that tree, and Upgrade mode replaces files in place.

On Windows, run those commands in WSL or Git Bash — Windows PowerShell's
`curl` is an alias for `Invoke-WebRequest` with different flags, and
`cp`/`mkdir` chained with `&&` won't work the same way.

`compose.yaml` pulls the prebuilt `latest` image from Docker Hub. See
[Building from source](#building-from-source) below if you'd rather build
it yourself.

Open <http://localhost:8666>. On first visit the UI asks you to **set a
username and password** for the web interface — pick those, sign in, and the
dashboard then prompts you to add your Qobuz credentials on the **Settings**
page (or set them in `.env` before starting).

> **Login is on by default.** The web UI requires sign-in out of the box. To
> run it without a login — only sensible on a trusted LAN or behind your own
> authenticating reverse proxy — set `WEB_AUTH=none` in `.env`. The container
> logs a warning on every boot while auth is off. There's no password reset:
> to change the login, stop the container and delete `.qobuz_web_auth.json`
> from the data volume, then set it again on next visit.

From there:

1. Open **Settings**, paste your `user_auth_token`, click **Test**, then **Save**.
2. Use the search bar on the dashboard to find an album.
3. Click **Download** — the job page streams the live log as it imports.

### Qobuz credentials

Auth is by **token**, not your password (it's the `password_or_token` field
streamrip uses). You need a paid Qobuz account; this only downloads what your
subscription already entitles you to. To get the token:

- **Qobuz web player** — sign in at [play.qobuz.com](https://play.qobuz.com), open
  your browser's dev tools (F12), then:
  - **Chrome / Edge**: Application → Local Storage → play.qobuz.com
  - **Firefox**: Storage → Local Storage → play.qobuz.com

  Click the `localuser` entry and copy its `token` value (the `id` field in the
  same entry is your user id), or
- if you already use streamrip outside Docker, copy `password_or_token`
  from `~/.config/streamrip/config.toml` on your host.

Paste it into the Auth Token field on the Settings page; "User ID / Email"
takes either your account email or the numeric Qobuz user id. Credentials
stay in the container's config volume and are never sent anywhere but Qobuz.

### Building from source

```bash
git clone https://github.com/jarynclouatre/qobuz-librarian.git
cd qobuz-librarian
cp .env.example .env
docker compose -f compose.yaml -f compose.dev.yaml up -d --build
```

### Configuration

Host paths and the web port are read from a gitignored `.env` file. Copy
`.env.example` to `.env` and edit these values:

| Variable             | Default             | Purpose                                |
|----------------------|---------------------|----------------------------------------|
| `QL_MUSIC_DIR`       | `./music`           | Music library; beets imports into this |
| `QL_STAGING_DIR`     | `./staging`         | Scratch space for in-progress downloads|
| `QL_UPGRADE_BACKUPS` | `./upgrade_backups` | Backups taken before a quality upgrade  |
| `WEB_PORT`           | `8666`              | Host port for the web UI               |

Point `QL_MUSIC_DIR` at a folder used **only** for music — not your home directory
or a drive with other files alongside it. The app moves and merges files inside that
tree, and Upgrade mode replaces files in place.

These four variables are read by Docker Compose on the **host** — they set
the mount sources. `compose.yaml` maps them to the container-side names
(`MUSIC_ROOT`, `STAGING_DIR`, etc.) the app reads.

Behaviour toggles (prefer hi-res master selection, consolidate folders,
multi-artist migration, upgrades-during-walks, compression) can be changed
live on the **Settings** page — no restart — or set as defaults in
`compose.yaml`. Tuning knobs (search limits, timeouts, fuzzy-match
thresholds) are environment variables in `compose.yaml`; each ships with a
working default you can override.

Advanced thresholds (fuzzy-match cutoffs, retention windows,
`POST_JOB_HOOK`) live in the `compose.yaml` env block; see
`src/qobuz_librarian/config.py` for what each does. `POST_JOB_HOOK` runs your
command in a shell — only set it to a command you trust.

### Lyrics & download quality

| Variable             | Default  | Purpose                                          |
|----------------------|----------|--------------------------------------------------|
| `STREAMRIP_QUALITY`  | `4`      | Download tier 1–4; see [Download quality](#download-quality), or the Settings page |
| `LYRICS_ENABLED`     | `true`   | Fetch lyrics on import (toggle on Settings page) |
| `LYRICS_FORMAT`      | `embed`  | `embed` (FLAC tag), `sidecar` (.lrc), or `both`  |
| `LYRICS_PROVIDERS`   | *(auto)* | Comma list, in order, e.g. `Lrclib,NetEase`      |

### beets & streamrip config

Their full config files live in the persistent `config` volume, seeded once and
**never overwritten** — anything beets or streamrip supports, you can set:

- `…/beets/config.yaml` — tagging, paths, plugins ([beets docs](https://beets.readthedocs.io/))
- `…/streamrip/config.toml` — downloader settings ([streamrip docs](https://github.com/nathom/streamrip))

For folder/file naming without hand-editing YAML, uncomment and fill in
`BEETS_PATH_DEFAULT` / `BEETS_PATH_SINGLETON` / `BEETS_PATH_COMP` in
`compose.yaml` — the lines are already there, just remove the leading `#`
(beets path syntax, e.g. `$albumartist/$album ($year)/$track - $title`).

Plugins are also overridable via `BEETS_PLUGINS` (comma list) in
`compose.yaml` or the Settings page — e.g. `fetchart,lastgenre,replaygain`.
The default seeded config enables only `fetchart`. Plugins that need
their own config block (lastgenre API key, replaygain backend, etc.) still
require an edit to `/config/beets/config.yaml`; the env var only controls
which plugins are loaded.

## Pointing it at an existing library

The defaults assume a fresh library. For a collection that's already
beets-managed (or just well-organised), the layout and migration notes are
below.

### Expected folder layout

The scanner expects a **two-level tree** — `$MUSIC_ROOT/<Artist>/<Album>/`:

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

The album folder *name* is flexible — `Album`, `Album (2017)`,
`Album [2017]`, `2017 - Album` all work; a year in the name is used when
present but isn't required, since presence matching is driven by the
track tags, not folder names. Per-disc subdirs (`CD1/`, `CD2/`) are
recursed into. Most beets path templates work as-is, as long as the
result is artist-then-album.

Hidden directories (`startswith(".")`) and the staging dir are skipped.
Layouts it won't detect: flat (`/music/<track>.flac`) or extra-nested
(`/music/<Genre>/<Artist>/…`, `/music/<Artist>/<Year>/<Album>/…`) — anything
that isn't exactly artist directory, then album directory. Set
`QL_MUSIC_DIR` in your `.env` to point at the artist-level directory on
the host.

## Migrating an existing library into the layout

Already have a collection that *isn't* in the `Artist/Album (Year)/` shape
above — flat folders, inconsistent names, half-tagged files? The **Migrate**
tool builds a tidy copy of it in the layout this tool expects, working from
what your files already say. It's a one-time setup step, separate from
downloading, and **it never needs a Qobuz login**.

How it decides where each file goes, in order of trust:

1. **Your tags first.** It reads each file's existing tags (album artist,
   album, title, track, disc) and places the file from those. Most libraries
   are mostly-tagged, so this pass is fast and works entirely offline.
2. **Audio fingerprint, only if you ask.** For files whose tags can't place
   them, an optional second pass identifies them by sound via AcoustID. **No
   API key is needed** — looking a fingerprint up is free; a personal key only
   matters for *submitting* fingerprints back, which this never does. It's
   slower and needs network access, so it's off by default; turn it on for a
   messier, less-tagged collection.

It is safe by design:

- **It copies — your originals are never touched.** The organised library is
  built at a *separate* destination; your source files are only read. (A "move
  instead of copy" option exists, but copying is the default and the safe
  choice.)
- **You preview before anything is written.** It shows exactly what would go
  where and what it couldn't place, and waits for you to confirm.
- **It never deletes anything.** Files it can't confidently identify are left
  where they are and listed in a report (`migration-manifest.csv`, written to
  the destination) — never moved into a wrong guess, never removed.

### Running it

Point it at two folders — the messy library to read and an empty one to build
into — and set them in your `.env` / `compose.yaml`:

```yaml
services:
  qobuz-librarian:
    environment:
      QL_MIGRATE_SRC: /old-library
      QL_MIGRATE_DEST: /organised
    volumes:
      - /path/to/your/messy/library:/old-library:ro   # :ro = read-only, belt-and-braces
      - /path/to/new/empty/folder:/organised
```

Then either:

- **Web:** open the **Migrate** page, optionally tick *fingerprint unidentified
  files*, and click **Preview migration**. Review the per-artist list (and the
  count of anything that couldn't be placed), then **Copy selected**.
- **CLI:** `docker compose run --rm qobuz-librarian cli --migrate` — add
  `--acoustid` for the fingerprint pass, or `--dry-run` to preview only. Source
  and destination come from the env vars above, or pass `--migrate-src` /
  `--migrate-dest`.

When it finishes, point `QL_MUSIC_DIR` at the new destination and run a normal
Library scan to pick it up.

### Caveats — read before you trust it

- **AcoustID isn't always right.** Fingerprint matches are a best guess; review
  the result, don't assume it nailed every track.
- **Tag-less files may not be identifiable at all.** A file with no usable tags
  and no confident fingerprint match is left in place and flagged — never
  silently dropped, but you'll have to sort it by hand.
- **Compilations, "Various Artists", and multi-disc albums are the tricky
  cases.** They're handled when the tags say so — a compilation flag, an album
  artist of "Various Artists", disc numbers. A compilation with *none* of those
  signals has no way to be recognised as one: each track is placed under its own
  track-artist, so it scatters across artist folders. Check the result for
  compilations and re-file them by hand.
- **The year comes from tags only.** A file tagged without a year lands in
  `Artist/Album/` rather than `Artist/Album (Year)/`, even if the original
  folder name had a year — both forms scan fine, but they won't all match.
- **Spot-check before you adopt it.** Open the destination and the manifest and
  look it over before pointing the tool at the result as your main library.
  It's a copy, so the worst case costs only disk space — delete it and re-run.

## Bringing your existing beets database

The container creates `/config/beets/musiclibrary.db` fresh on first start
if none exists. To use your existing beets database instead:

1. Stop the container if it's running.
2. Copy your DB and config into the `qobuz-librarian-config` volume:
   ```bash
   docker run --rm -v qobuz-librarian-config:/dest -v /your/beets/dir:/src alpine \
     sh -c 'mkdir -p /dest/beets && cp /src/config.yaml /dest/beets/ && \
            cp /src/library.db /dest/beets/musiclibrary.db'
   ```
   Replace `library.db` with your actual database filename. If you're not
   sure of the name, check the `library:` path in your beets `config.yaml`.
   The container expects the DB at `/config/beets/musiclibrary.db`.

   (Or bind-mount a host directory at `/config/beets` in `compose.yaml`.)
3. Start the container. It will not overwrite either file.

## Tagging an untagged collection (AcoustID)

Qobuz downloads always arrive fully tagged, so the normal import leaves the
autotagger off. But for older, untagged files you already have, beets' built-in
`chroma` plugin can identify them by audio fingerprint (AcoustID) and tag them.
The fingerprint tool (`fpcalc`) and its Python binding ship in the image.

This tags files *where they sit*. If you also want them reorganised into the
`Artist/Album (Year)/` layout, use the
[Migrate tool](#migrating-an-existing-library-into-the-layout) instead — it
does both, tags-first with the same AcoustID fallback, and copies by default.

This is a separate, opt-in run — it does not change Qobuz downloads. A
ready-made config is bundled so you don't have to touch your normal beets
settings:

```bash
docker compose run --rm -e BEETSDIR=/config/beets qobuz-librarian \
  beet -c /app/docker/beets-chroma.yaml import /music/<your-untagged-folder>
```

- It fingerprints each file, looks it up on AcoustID, and shows the matching
  MusicBrainz releases for you to confirm one album at a time. AcoustID isn't
  always right, so you review each match — accept, skip, or pick another.
- It tags files **in place**: tags are written where the files already are and
  the files are added to your library database; nothing is moved, copied, or
  deleted. Run `beet move` afterwards if you also want them re-foldered.
- Lookups work out of the box — beets uses a built-in AcoustID key. You only
  need your own key to *submit* fingerprints back with `beet submit`; get one
  at <https://acoustid.org/new-application> and add `acoustid: {apikey: "KEY"}`
  to your beets config.
- Needs outbound network to acoustid.org and musicbrainz.org, and is best run
  when no download/import is active (both use the same beets database).

## Default beets config

The seeded `beets/config.yaml` is conservative so it doesn't surprise you
on first import:

| Setting | Default | Why this default | When to change it |
|---------|---------|------------------|-------------------|
| `autotag` | `no` | Keeps Qobuz's tags; the downloader already produces tagged FLACs. | Turn on if you trust MusicBrainz over Qobuz for tagging, or want auto-cover-art beyond what Qobuz embeds. |
| `move` | `yes` | Files leave staging and land in `MUSIC_ROOT` cleanly. | Switch to `copy` if `/music` and `/staging` are on different filesystems and you want to keep the staging copy. |
| `duplicate_action` | `merge` | Gap-fill merges new tracks into an existing album folder. | Use `skip` to skip anything that collides; `keep` to keep both copies on disk. |
| `incremental` | `no` | Always rescans staging so a retry sees the same files. | Turn on to skip already-seen staging dirs. |

Edit `/config/beets/config.yaml` and the changes apply on the next import
— no restart needed.

## First scan on a big library

A library-wide scan does one Qobuz API call per artist directory, with a
small pause between calls (`ARTIST_API_DELAY`, default 0.4 s). ~2000
artists is roughly 13 minutes before the review screen opens. Library
walks log progress line by line; nothing is downloaded until you approve
the review. It's a scan-once-then-review flow rather than a daemon — re-run
it whenever you've added music, not on a schedule.

Singles and very short EPs are hidden from the missing-albums step by
default — bump `MISSING_ALBUMS_MIN_TRACKS` in `compose.yaml` (or pass
`--include-singles` on the CLI) if you want them surfaced.

## What it will and won't touch on its own

- It **never** deletes a track during gap-fill. A bare gap-fill only adds
  missing tracks; the Upgrade walk is the only path that replaces files,
  and it backs them up first (see `UPGRADE_BACKUP_RETENTION_DAYS`).
- Consolidation (merging sibling/duplicate album folders) is **CLI-only**
  and **off by default**. It needs per-folder confirmation, which the CLI
  prompts for; the web UI has no review screen for it, so web downloads
  always skip consolidation regardless of `CONSOLIDATE`. Run a `cli`
  command (and set `CONSOLIDATE=true`) if you want it.
- `MIGRATE_MULTI_ARTIST` is off by default. With it on, after import a
  folder named `Primary Artist, Other Artist/Album` is moved into
  `Primary Artist/Album`. Off keeps your paths stable across scans.

## Permissions (NAS and shared storage)

The container runs as `PUID:PGID`, which defaults to `1000:1000`, so
downloaded files are owned by a normal user rather than root. If your host
user or media-share owner isn't `1000`, set the right values in your `.env`:

```bash
PUID=1000   # id -u
PGID=1000   # id -g
```

They flow straight into the container — no need to edit `compose.yaml`. If
you genuinely need the app to run as root, set `PUID=0` / `PGID=0`.

The app drops to that user at startup and warns in the logs if a mounted
path isn't writable by it. Only the small `config`/`data` volumes are
chowned automatically; grant that user write access for `/music` and
`/staging`. If your music share is read-only on the NAS, append `:ro` to the
`/music` bind in `compose.yaml` — the app will refuse to mutate but scans and
upgrade detection still work.

If the bind dirs already exist (typical: Compose auto-created them as root
on first `up`), `chown` them to your `PUID`/`PGID` before enabling those
settings, or you'll see the "volume not writable" warning every boot:

```bash
sudo chown -R 1000:1000 ./music ./staging ./upgrade_backups
```

## Using the CLI

The CLI runs inside the same container as the web UI — no separate install.
Only one writer can hold the download lock, so free it first: either hand it
over from the web UI (**Settings → Mode → Hand off to terminal**, then
**Resume web app** when you're done — no restart), or stop the web container
outright with `docker compose stop qobuz-librarian` and `start` it after.

For an interactive menu:

```bash
docker compose run --rm -it qobuz-librarian cli
```

A few common unattended forms:

```bash
# Download a specific album (URL or "Artist Album" string)
docker compose run --rm qobuz-librarian cli https://open.qobuz.com/album/abcd1234

# Work through one artist's catalog
docker compose run --rm qobuz-librarian cli --artist "Stars of the Lid"

# Sweep every artist for quality upgrades, auto-confirming the safe ones
docker compose run --rm qobuz-librarian cli --upgrade-walk --auto-safe

# Full flag reference
docker compose run --rm qobuz-librarian cli --help
```

The CLI honours the same `.env` and `compose.yaml` settings as the web UI.

## JSON API

The web server exposes a small read-only JSON API — useful for home-automation
dashboards, external monitoring, or shell scripts that want job state without
scraping HTML.

| Endpoint | Description |
|---|---|
| `GET /api/jobs` | List all jobs, most recent first. Optional `?status=running` filter (`pending`, `running`, `scanning`, `awaiting_review`, `done`, `failed`, `canceled`). Optional `?limit=N` (max 500, default 50). Returns `{"jobs": [...], "count": N}`. |
| `GET /api/jobs/{id}/status` | Single job as JSON — same shape as one element of the list above. 404 if not found. |
| `GET /api/jobs/{id}/stream` | SSE stream for a live job. Events: `log` (one log line), `done` (job finished, close the stream). |

All endpoints require the session cookie when login is enabled. A missing or
invalid cookie returns `401 {"detail": "authentication required"}` rather than
a redirect, so non-browser clients can detect the auth gate cleanly.

Example — check whether anything is currently downloading:

```bash
curl -s -b 'qf_session=<your-cookie>' http://localhost:8080/api/jobs?status=running \
  | jq '.count'
```

## Troubleshooting

| Symptom | Likely cause / next step |
|---------|--------------------------|
| `Another Qobuz Librarian run is in progress` | Web container holds the lock — use the web UI, or `docker compose stop qobuz-librarian` for CLI. |
| `MUSIC_ROOT missing or inaccessible` | Bind mount unset or wrong path — check `QL_MUSIC_DIR` in `.env` and that the host path exists. |
| Container exits immediately on `docker compose up` | `.env` missing from the compose dir, or a required `QL_*` var is unset. `docker compose logs qobuz-librarian` shows which. |
| `Volume not writable` (Settings → Diagnostics shows FAIL) | `PUID`/`PGID` don't match the host owner of the bind mount — `chown -R $(id -u):$(id -g) ./music ./staging` or set `PUID`/`PGID` in `.env`. |
| Web UI loads but Library scan says "no artist folders found" | `/music` is mounted at an empty directory or one level off — make sure `QL_MUSIC_DIR` points at the artist-level folder, not the parent. |
| Token rejected (Settings → Test) | Token expired, copied with surrounding quotes, or pasted with trailing whitespace — re-grab it from play.qobuz.com (dev tools → Local Storage → `localuser` → `token`; Chrome/Edge: Application tab, Firefox: Storage tab), paste clean. |
| Download stalls in "Importing into beets…" | A beets plugin is loaded without its required config block (e.g. lastgenre API key, replaygain backend). Disable it via `BEETS_PLUGINS` or add the block to `/config/beets/config.yaml`. |
| `docker compose pull` 404 | Image hasn't been published under that tag yet — build from source (see [Building from source](#building-from-source)). |
| Healthcheck failing but port reachable | Container couldn't reach its own `/healthz` — check container resource limits and `docker logs qobuz-librarian`. |
| Upgrade walk fails with `Permission denied` backing up an album | An earlier `docker exec qobuz-librarian beet …` (or similar) ran as root, leaving root-owned files the librarian (PUID 1000) can't move. Either rerun with `docker exec --user 1000:1000 …`, or fix on the host: `sudo chown -R 1000:1000 ./music`. |
| Music files vanished from `/music` after a manual `beet` command | `beet -d /config/beets …` reads `-d` as the *destination* directory, so with `move: yes` it relocates the whole library into the config volume (`beet ls -p` still prints `/music/…` because paths are stored relative). The container already exports `BEETSDIR`; run `beet …` with no `-d`, or `BEETSDIR=/config/beets beet …`. |

## Security and deployment shape

The bundled `compose.yaml` ships hardened — on a fresh `docker compose up -d`:

- `mem_limit: 1g`, `pids_limit: 256` — a runaway streamrip child can't
  exhaust host resources.
- `no-new-privileges` and `cap_drop: [ALL]`, adding back only the few caps
  gosu needs for the PUID/PGID handover.
- Multi-arch image (`linux/amd64`, `linux/arm64`) — native arm64 NAS builds.
- Token files land `0600`; `QL_CHECK_VOLUMES=1` fails loudly with a 503 on a
  wrong PUID/PGID rather than silently writing mis-owned files.

`--read-only` rootfs deployments work as long as you include `--tmpfs /tmp`
(or set `APP_HOME=/var/tmp` with `--tmpfs /var/tmp`).

The web UI has a built-in login, on by default: the first visit prompts you
to set a username and password, and every page and endpoint requires sign-in
after that. The password is stored as a salted PBKDF2 hash (`0600`, never
plaintext) and the session is an `HttpOnly` cookie. Set `WEB_AUTH=none` to
turn the login off — the container logs a warning every boot when you do, and
you should only run that way on a trusted network. The built-in login is a single shared credential with per-IP brute-force
limiting (5 failures per hour before a 429 response), but for internet
exposure you should still put it behind an authenticating reverse proxy (or
VPN / Tailscale). See [SECURITY.md](SECURITY.md).

## Limitations

- **One library, one container.** The `/staging` dir is single-writer. The
  run-lock prevents the CLI and web container from clashing inside one
  compose stack, but two stacks against the same mount will collide.
- **Qobuz only.** This tool drives streamrip's Qobuz path specifically;
  Tidal/Deezer/SoundCloud aren't wired up here, even though streamrip
  itself can reach them. (Their config still lives in your `config.toml`
  if you use streamrip directly.)
- **No lossy transcoding output.** `DOWNSAMPLE_HIRES_ENABLED` can downsample hi-res
  FLACs to 44.1/48 kHz before import (still FLAC); there's no path to MP3
  or other lossy formats.
- **English / Latin metadata matching is best.** Fuzzy matching uses
  case-fold + edit distance; CJK / right-to-left titles still work but
  the thresholds were tuned on Latin scripts. Edition-variant stripping
  and lyric-title normalisation use English keyword lists — non-Latin
  editions (e.g. "豪华版") are kept verbatim rather than stripped.
- **PWA install / offline mode need HTTPS.** The service-worker API only
  activates on HTTPS or `localhost` — front the container with a
  TLS-terminating reverse proxy if you want to install it as an app.
- **Windows console:** the CLI uses `·` (U+00B7 middle-dot) for progress
  lines. Windows terminals without UTF-8 mode (`chcp 65001`) will show a
  replacement character instead. The tool otherwise runs fine under WSL.

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[test]"
python -m pytest -q
```

PowerShell activation is `.venv\Scripts\Activate.ps1` instead of `source`.

`ruff check src tests` runs in CI; keep it clean before opening a PR.
`scripts/smoke_test.sh` boots the container and exercises the main CLI
modes against a temp music dir — run it before tagging a release.
See [CONTRIBUTING.md](CONTRIBUTING.md) for the rest.

## Acknowledgements

Qobuz Librarian is the glue around several excellent open-source projects,
which are bundled into the Docker image:

- **[streamrip](https://github.com/nathom/streamrip)** (Nathaniel Thomas) —
  the actual Qobuz downloader. GPL-3.0.
- **[beets](https://beets.io/)** ([github](https://github.com/beetbox/beets)) —
  tagging, cover art, and library organisation. MIT.
- **[mutagen](https://github.com/quodlibet/mutagen)** — audio metadata
  reading/writing. GPL-2.0-or-later.
- **[FFmpeg](https://ffmpeg.org/)** — audio probing and transcoding.
  LGPL/GPL depending on build.

Lyrics are sourced via [syncedlyrics](https://github.com/moehmeni/syncedlyrics)
(LRCLIB, NetEase, Musixmatch). The web UI uses
[FastAPI](https://fastapi.tiangolo.com/),
[htmx](https://htmx.org/), [Tailwind CSS](https://tailwindcss.com/) and
[daisyUI](https://daisyui.com/). Thanks to all of their maintainers.

## License

This project's own code is **MIT** — see [LICENSE](LICENSE).

The Docker image redistributes the third-party tools listed under
[Acknowledgements](#acknowledgements), each of which keeps its own license.
Notably **streamrip (GPL-3.0)** and **mutagen (GPL-2.0-or-later)** are
copyleft — if you redistribute the image or a derivative, review their
terms. Qobuz Librarian invokes streamrip as a separate program (subprocess),
not as a linked library; see each project for authoritative license text.
