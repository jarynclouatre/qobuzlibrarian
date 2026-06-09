# Changelog

All notable changes to Qobuz Librarian are recorded here, newest first. The
project follows [semantic versioning](https://semver.org/); dates are when each
version was tagged during local development.

## [0.6.0] - 2026-06-09

- **Get one song.** Search has a Tracks mode and a *Get track* button that pulls
  a single track instead of the whole album. It lands in the right
  `Artist/Album (Year)/` folder over the same per-track path repair uses — never
  a full-album rip — and the partial folder it leaves is recorded so the bulk
  scans don't nag you to finish that album. An artist you own only a grabbed
  sample by isn't read as one you're collecting, so their back catalogue stays
  out of the scans and the new-release check too. If the grabbed track was the
  album's last missing one you now own the whole thing and it's filed as a
  normal complete album; finishing the album the usual way later clears the mark
  the same way. Asking for a track you already have downloads nothing. The
  Upgrade walk leaves grabbed singles alone unless you set
  `UPGRADE_SINGLES_ENABLED`. A finished grab carries an **Undo** that deletes the
  track, drops its beets row, clears the mark, and removes the folder if the grab
  created it and it's now empty.
- Two quick retries of the same album can no longer double-queue it — retry now
  re-checks for a job already touching that album under the submit lock, the same
  way the download route does.
- The downsample step caps the ffmpeg encode at ten minutes, so a track on a hung
  NFS or FUSE mount fails with a clear message and leaves the original untouched
  instead of pinning a worker forever.
- Behind a reverse proxy the entrypoint passes `--proxy-headers` and honours
  `FORWARDED_ALLOW_IPS`, so the login rate-limiter sees each client's real
  address instead of the proxy's and stops locking everyone out at once.

## [0.5.0] - 2026-06-05

First public release. The big additions over the private 0.4 line:

- **Migrate** mode turns an existing, messy or half-tagged collection into the
  `Artist/Album (Year)/` layout the rest of the tool expects. It reads each
  file's tags first and can fall back to AcoustID fingerprinting; it copies by
  default, so the originals are never touched, and anything it can't place
  confidently is left alone and listed in a manifest.
- ISRC-anchored **repair** now snapshots a truncated file's tags before it goes
  and restores them onto the refilled track, and backs up the source by ISRC
  before replacing it — a crash mid-refill can no longer strand a track.
- The awaiting-review list pages by artist and keeps its selection on the
  server, so approving thousands of candidates no longer rides on form state.
- Lyric state and the retry manifest are locked across processes; rejected
  staging files are quarantined instead of silently left in place.

## [0.4.1] - 2026-05-27

- A corrupt fetch-log line can no longer 500 the dashboard.
- `Retry-After: 0` from Qobuz is honoured instead of being treated as no header.
- An unrecognised `STREAMRIP_QUALITY` warns loudly rather than defaulting to the
  most permissive cap.

## [0.4.0] - 2026-05-21

- **Check for new releases** — across the whole library or one artist —
  compares each artist's current Qobuz catalogue against what you've seen and
  surfaces only what's genuinely new, pre-ticked. It reads the catalogue listing
  alone, so it's about one API call per artist.
- On-disk caches (album fetches, parsed FLAC tags keyed on path+mtime+size, and
  artist catalogues with a TTL) turn a re-scan of an unchanged library into
  seconds instead of minutes.
- Jobs survive a container restart: an awaiting-review list comes back, and an
  interrupted job returns marked as such with a retry hint instead of vanishing.

## [0.3.1] - 2026-04-30

- Multi-disc folders detect disc numbers for non-FLAC tracks.
- Two upgrade-backup restore edges (equal-byte and empty-backup-dir) no longer
  block automatic recovery.

## [0.3.0] - 2026-04-28

- **Upgrade** mode re-rips albums Qobuz can now serve at a higher quality,
  backing up the originals first.
- **Downsample** mode shrinks hi-res FLACs above CD rate to 44.1/48 kHz, each
  verified to decode cleanly before it replaces the original.
- **Repair** finds truncated or short FLACs and refills the exact missing tracks
  by ISRC, leaving good files untouched.
- **Lyrics** mode backfills synced lyrics across tracks already on disk.

## [0.2.1] - 2026-04-03

- The dashboard's stale-token banner flips the moment the API rejects the token,
  instead of only checking at startup.
- Cancelling a queued download stops cleanly instead of leaving a half-finished
  album to be swept into a later import.

## [0.2.0] - 2026-03-26

- A web UI (FastAPI) for searching, downloading and watching jobs stream their
  log live, alongside the existing CLI.
- A crash-safe persistent download queue that resumes after a restart, with a
  per-run lock so two instances can't fight over the same library.
- Whole-library and per-artist gap scans that list every missing album.
- Ships as a multi-stage Docker image with a compose stack.

## [0.1.0] - 2026-01-29

- First working version: download a single Qobuz album or a whole artist, scan a
  local library to know what's already there, and import cleanly with beets so
  only the genuinely missing tracks are fetched.
