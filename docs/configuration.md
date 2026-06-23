# Configuration

[← README](../README.md)

Host paths and the web port come from a gitignored `.env` (copy `.env.example`). Docker Compose reads these on the host and maps them to the container-side names the app uses. Everything else is a behaviour toggle: set it live on **Settings**, or as a default in `compose.yaml`.

## Host paths

| Variable | Default | Purpose |
|---|---|---|
| `QL_MUSIC_DIR` | `./music` | Music library; beets imports into this |
| `QL_STAGING_DIR` | `./staging` | Scratch space for in-progress downloads |
| `QL_UPGRADE_BACKUPS` | `./upgrade_backups` | Backups taken before a quality upgrade |
| `WEB_PORT` | `8666` | Host port for the web UI |

## Behaviour toggles

| Variable | Default | Purpose |
|---|---|---|
| `STREAMRIP_QUALITY` | `4` | Download tier 2–4 (see [Download quality](#download-quality)) |
| `LYRICS_ENABLED` | `true` | Fetch lyrics on import |
| `LYRICS_FORMAT` | `embed` | `embed` (FLAC tag), `sidecar` (.lrc), or `both` |
| `LYRICS_PROVIDERS` | *(auto)* | Ordered comma list, e.g. `Lrclib,NetEase` |
| `ARTWORK` | `sidecar` | Cover art: `sidecar`, `embed`, or `both` |
| `AUTO_LIBRARY_SCAN` | `true` | Run the one-time library scan on first launch |
| `NEW_RELEASE_CHECK_INTERVAL` | — | How often to auto-check for new releases (also on Settings) |
| `ARTIST_CATALOG_CACHE_TTL` | — | How long artist album-lists stay cached |
| `REPAIR_CACHE_ENABLED` | `true` | Cache the repair scan's Qobuz ISRC lookups (files are still decode-tested fresh every scan) |
| `REPAIR_CACHE_TTL_DAYS` | `30` | How long a cached ISRC lookup is reused before re-verifying (`0` = keep until the db is deleted) |
| `AUTO_UPGRADE_ENABLED` | `false` | Surface upgrades during ordinary gap-fill walks |
| `DOWNSAMPLE_HIRES_ENABLED` | `false` | Downsample hi-res FLACs as they download (see below) |
| `UPGRADE_SINGLES_ENABLED` | `false` | Let the Upgrade walk re-rip tracks you pulled as singles |
| `MIGRATE_MULTI_ARTIST` | `false` | Re-file `A, B/Album` under `A/Album` after import |
| `CONSOLIDATE` | `false` | Merge sibling/duplicate album folders (CLI-only) |

`DOWNSAMPLE_HIRES_ENABLED` only touches new downloads (88.2 / 176.4 / 352.8 kHz → 44.1; 96 / 192 / 384 → 48; bit depth preserved, originals replaced atomically). To shrink hi-res already in your library, use the on-demand **Downsample** mode instead.

Advanced thresholds — fuzzy-match cutoffs, `ARTIST_SCAN_WORKERS`, `ARTIST_API_DELAY`, `REPAIR_LOOKUP_MIN_INTERVAL`, retention windows, `POST_JOB_HOOK` — ship with working defaults. The common ones are shown (commented) in `compose.yaml`, the full set is in `.env.example`, and `src/qobuz_librarian/config.py` documents each. `POST_JOB_HOOK` runs in a shell, so only set it to a command you trust.

## Download quality

A fresh install downloads at tier `4`, the best Qobuz serves per release (24-bit up to 192 kHz, falling back to CD lossless). Change it on **Settings** or via `STREAMRIP_QUALITY`.

| Tier | Quality | Notes |
|---|---|---|
| `4` | 24-bit ≤192 kHz | Default; archival |
| `3` | 24-bit ≤96 kHz | Hi-res, smaller cap |
| `2` | 16-bit / 44.1 kHz | CD lossless, smallest |

To keep hi-res masters at a saner size, pull tier `4` and either enable import-time downsampling (`DOWNSAMPLE_HIRES_ENABLED`) or run **Downsample** on demand.

## beets & streamrip config

The bundled tools' full config files live in the persistent `config` volume, seeded once and never overwritten:

- `…/beets/config.yaml` — tagging, paths, plugins ([beets docs](https://beets.readthedocs.io/))
- `…/streamrip/config.toml` — downloader settings ([streamrip docs](https://github.com/nathom/streamrip))

For folder and file naming without hand-editing YAML, uncomment `BEETS_PATH_DEFAULT` / `BEETS_PATH_SINGLETON` / `BEETS_PATH_COMP` in `compose.yaml` (beets path syntax, e.g. `$albumartist/$album ($year)/$track - $title`). Set which plugins load with `BEETS_PLUGINS`; the seeded config enables `fetchart` and `inline` — keep `inline`, it backs the multi-disc folder field. Plugins that need their own config block (lastgenre API key, replaygain backend) still require an edit to `config.yaml`.

For its own imports the downloader pins four beets settings regardless of your config: `autotag: no` (keep Qobuz's tags), `move: yes` (clear staging even across filesystems), `incremental: no` (rescan on retry), and `duplicate_action: merge` (gap-fill into the existing folder without deleting your files). Your own `beet` commands read your config unchanged. Edits apply on the next import, no restart.

## Permissions (NAS and shared storage)

The container runs as `PUID:PGID` (default `1000:1000`), so downloads are owned by a normal user rather than root. If your host or media share owner isn't `1000`, set them in `.env` and they flow straight in:

```bash
PUID=1000   # id -u
PGID=1000   # id -g
```

On boot it chowns the app-managed volumes (`config`, `data`, `staging`, `upgrade_backups`) to that user and warns if a mounted path isn't writable. `/music` is left alone, since it's often a large NAS mount, so make sure the run user can write to it. If your music share is read-only, append `:ro` to the `/music` bind: scans and upgrade detection still work, but leave `QL_CHECK_VOLUMES` unset (its write test will otherwise return 503 on scan endpoints). To run as root, set `PUID=0` and `PGID=0` explicitly — a non-numeric typo makes the container refuse to start rather than silently fall back to root.

If the bind dirs were auto-created as root on first `up`, chown them before enabling those settings:

```bash
sudo chown -R 1000:1000 ./music ./staging ./upgrade_backups
```

## Deployment

**Login.** The web UI requires sign-in out of the box. The password is stored as a salted PBKDF2 hash (`0600`, never plaintext) and the session is an `HttpOnly` cookie. Set the credentials on the first-visit screen, or seed `WEB_AUTH_USER` / `WEB_AUTH_PASSWORD` in `.env` so the box comes up already locked down. Those two double as a password reset: change them and restart. To reset without env vars, stop the container, delete `.qobuz_web_auth.json` from the data volume, and set new credentials on the next visit.

`WEB_AUTH=none` turns the login off — only sensible on a trusted LAN or behind your own authenticating proxy. The container logs a warning every boot while it's off.

**Behind a reverse proxy.** Set `FORWARDED_ALLOW_IPS` to the proxy's address so the failed-login throttle counts attempts per real client, not per the single proxy IP (otherwise one wrong-password run locks everyone out). Point it at your proxy, not `*`.

**Keeping the token out of the environment.** `docker inspect` exposes environment variables, so to keep the Qobuz token out of them, point `QOBUZ_USER_AUTH_TOKEN_FILE` at a file holding just the token — a [Docker secret](https://docs.docker.com/engine/swarm/secrets/) or read-only bind mount — instead of setting `QOBUZ_USER_AUTH_TOKEN`. The web-login password takes the same treatment: point `WEB_AUTH_PASSWORD_FILE` at a file holding just the password instead of setting `WEB_AUTH_PASSWORD`.

**Hardening.** The bundled `compose.yaml` ships with `mem_limit: 1g` and `pids_limit: 256` (a runaway streamrip child can't exhaust the host), `no-new-privileges`, `cap_drop: [ALL]` (adding back only the few caps gosu needs for the PUID/PGID handover), and `0600` token files. It's a multi-arch image (`linux/amd64`, `linux/arm64`), so arm64 NAS boxes run natively. A `--read-only` rootfs works with `--tmpfs /tmp` (or `APP_HOME=/var/tmp` with `--tmpfs /var/tmp`). The built-in login is a single shared credential with per-IP brute-force limiting (5 failures an hour before a 429) — enough for a trusted network, but front it with a proxy, VPN, or Tailscale for internet exposure. See [SECURITY.md](../SECURITY.md).

## What the app does on its own

Scans run automatically, but only to show you things. On first launch it runs a one-time library scan (`AUTO_LIBRARY_SCAN`); after that it periodically checks for new releases (`NEW_RELEASE_CHECK_INTERVAL`). Both only read Qobuz and park a review list — nothing downloads or changes a file until you approve it.

- **Gap-fill** only ever adds missing tracks; it never deletes or rewrites one.
- **After a download finishes**, it re-checks the new album's track lengths against Qobuz and flags it for **Repair** if one came up short. This is a read-only check (a clean truncation can decode fine yet be cut), and it changes nothing.
- **Upgrade** and **Downsample** replace files, but only when you start them. Upgrade backs up the originals first (`UPGRADE_BACKUP_RETENTION_DAYS`); Downsample rewrites in place after verifying every file decodes, and pre-selects nothing.
- **Lyrics** writes lyric tags or `.lrc` sidecars into existing tracks, never the audio.
- **Consolidation** (`CONSOLIDATE`, off) merges duplicate folders. It's CLI-only — it needs per-folder confirmation the web UI has no screen for, so web downloads always skip it.
- **`MIGRATE_MULTI_ARTIST`** (off) re-files `A, B/Album` under `A/Album` after import; off keeps your paths stable across scans.
