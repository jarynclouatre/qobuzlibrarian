# Existing libraries

[← README](../README.md)

The defaults assume a fresh library. For a collection you already have — beets-managed or just well-organised — here's the layout the scanner expects and how to bring one in.

## Folder layout

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

The album folder name is flexible — `Album`, `Album (2017)`, `Album [2017]`, and `2017 - Album` all work. A year is used when present but isn't required, since matching is driven by track tags, not folder names. Per-disc subdirs (`CD1/`, `CD2/`) are recursed into; hidden directories and the staging dir are skipped. Flat (`/music/<track>.flac`) and extra-nested (`/music/<Genre>/<Artist>/…`) layouts aren't detected, so point `QL_MUSIC_DIR` at the artist-level directory.

## Migrating into the layout

If your collection isn't in `Artist/Album (Year)/` shape — flat folders, inconsistent names, half-tagged files — the **Migrate** tool builds a tidy copy in the expected layout from what your files already say. It's a one-time step, separate from downloading, and needs no Qobuz login.

It places each file by tags first (album artist, album, title, track, disc), which is fast and entirely offline. For files whose tags can't place them, an optional AcoustID fingerprint pass identifies them by sound; it's slower and needs network access, so it's off by default. No API key is needed.

Migrate copies by default, so your originals are only read. It previews the full plan first — where each file goes, what it couldn't place, and the space the copy needs against what's free — then waits for you to confirm. Files it can't confidently identify are left in place and listed, never moved into a wrong guess. Two CSVs land at the destination: `migration-manifest.csv` (the full plan, including everything left behind and why) and `migration-results.csv` (what the run copied, skipped, or failed on).

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
- **CLI:** `docker compose run --rm qobuz-librarian cli --migrate` — add `--acoustid` for fingerprinting, `--in-place` to move instead of copy, or `--dry-run` to preview. Source and destination come from the env vars above or `--migrate-src` / `--migrate-dest`.

When it's done, point `QL_MUSIC_DIR` at the new destination and run a Library scan.

A few things to check in the result: AcoustID matches are a best guess, so review them. A compilation with no signal at all (no compilation flag, no "Various Artists" album artist, no disc numbers) can't be recognised as one — each track lands under its own track-artist and scatters across folders. The year comes from tags only, so a file tagged without one lands in `Artist/Album/` rather than `Artist/Album (Year)/`. It's a copy, so the worst case is wasted disk: spot-check, then delete and re-run if you don't like it.

## Bringing an existing beets database

The container creates `/config/beets/musiclibrary.db` on first start if none exists. To use yours, stop the container and copy your DB and config into the `qobuz-librarian-config` volume (the DB must end up named `musiclibrary.db`):

```bash
docker run --rm -v qobuz-librarian-config:/dest -v /your/beets/dir:/src alpine \
  sh -c 'mkdir -p /dest/beets && cp /src/config.yaml /dest/beets/ && \
         cp /src/library.db /dest/beets/musiclibrary.db'
```

Replace `library.db` with your filename (check the `library:` path in your `config.yaml` if unsure), or bind-mount a host directory at `/config/beets` instead. The container won't overwrite either file on start. If renaming the DB to `musiclibrary.db` isn't convenient, point `BEETS_DB_PATH` at your file instead (e.g. `BEETS_DB_PATH=/config/beets/library.db`) and the app reads it from there.

## Tagging an untagged collection

Qobuz downloads arrive fully tagged, so import leaves the autotagger off. For older untagged files, beets' `chroma` plugin can identify them by audio fingerprint (AcoustID) and tag them in place — nothing is moved, copied, or deleted (`fpcalc` ships in the image). A ready-made config keeps it separate from your normal beets settings:

```bash
docker compose run --rm -e BEETSDIR=/config/beets qobuz-librarian \
  beet -c /app/docker/beets-chroma.yaml import /music/<your-untagged-folder>
```

It shows the matching MusicBrainz releases one album at a time, for you to accept, skip, or replace. Lookups use beets' built-in AcoustID key; you only need your own (from <https://acoustid.org/new-application>, added as `acoustid: {apikey: "KEY"}`) to submit fingerprints with `beet submit`. Run it when no download is active, since both use the same beets database. To also re-folder the files into the layout, use [Migrate](#migrating-into-the-layout) instead.

## The first scan on a big library

A library-wide scan makes roughly one Qobuz call per artist directory (cached on re-scans, so repeats are mostly free), fanned across a few artists at once (`ARTIST_SCAN_WORKERS`, default 4). There's no artificial delay between calls (`ARTIST_API_DELAY`, default 0); Qobuz's rate limit is handled by automatic retry and back-off, so raise it only if you get throttled. It's scan-then-review, not a daemon, so re-run it whenever you've added music. Singles and very short EPs are hidden from the missing-albums step by default; lower `MISSING_ALBUMS_MIN_TRACKS` (e.g. to 1) or pass `--include-singles` to surface them.

On **Library**, **Upgrade**, and **Downsample** scans you can dismiss albums you've decided against; they're remembered and skipped next time, so a big library can be triaged over several sessions. Restore them from the **Hidden** view. Dismissing is per album, so a new release by an already-reviewed artist still surfaces. The single-artist **Artist** scan never hides anything.
