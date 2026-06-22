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

Qobuz Librarian downloads from Qobuz — one album, a whole discography, or your entire library — and imports it with [beets](https://beets.io/). It wraps [streamrip](https://github.com/nathom/streamrip) for the downloading and keeps track of what you already own, so it only fetches what's missing. Web UI or CLI.

<p align="center">
  <img src="assets/screenshot-dashboard.png" alt="Web UI dashboard" width="800">
</p>

## Features

- **Gap-fill.** Point it at an album, an artist, or your whole library and it downloads only the tracks you're missing. Matching is edition-aware, so remasters and deluxe versions don't re-download as duplicates. Pick **Download this edition too** to keep a variant alongside what you own.
- **Single tracks.** Switch search to **Tracks** to grab one song. It's flagged as a deliberate single, so gap scans won't push you to complete the album. Download the full album later and it graduates back automatically.
- **Quality upgrades.** **Upgrade** mode re-rips albums Qobuz can now serve at higher quality, backing up the originals first.
- **Downsample.** Shrink hi-res FLACs to CD rate (44.1 / 48 kHz) to reclaim space, still lossless. Run it on demand, or apply it automatically to new downloads.
- **New releases.** A periodic pass flags new albums an artist has put out that you don't own, pre-ticked.
- **Clean import.** beets handles tagging and cover art, and files land in your library in a single move. Synced lyrics are fetched on import; **Lyrics** mode backfills tracks you already have.
- **Repair.** ISRC-anchored scanning finds truncated or corrupt FLACs and refills the exact missing tracks, leaving good files alone. It catches files that play but are cut short, not just obviously tiny ones.
- **Tidy up.** Consolidates duplicate album folders and handles Various Artists layouts. **Migrate** brings a messy or untagged collection into a clean `Artist/Album (Year)/` layout.
- **Crash-safe queue.** The download queue persists and resumes after a restart; a per-run lock keeps two instances off the same library.

## How it works

A single Docker image bundles streamrip, beets, ffmpeg, and the FLAC tools, with no sidecar containers. The web UI is the primary interface; the CLI runs the same engine for scripted or unattended jobs. Paths and ports come from environment variables, and everything else lives on the **Settings** page and applies live.

Every mode works the same way: **scan → review → download.** A scan runs in the background and parks a checklist of what it found. Nothing downloads or changes on disk until you tick what you want and approve.

| Page | What it does |
|---|---|
| **Search** | Find an album by name or Qobuz URL and download it |
| **Artist** | Scan one artist's discography; new releases pre-ticked |
| **Library** | Scan every artist for missing albums, or just check for new releases |
| **Upgrade** | Re-rip albums Qobuz can now serve at higher quality |
| **Downsample** | Shrink hi-res files to CD rate (local, no login) |
| **Repair** | Refill truncated or partial FLACs (ISRC-verified) |
| **Lyrics** | Fetch lyrics for tracks missing them (local, no login) |
| **Migrate** | Reorganise an existing library into the layout (copies, never touches originals) |
| **Queue** | Live progress, reviews awaiting approval, and download history |
| **Settings** | Qobuz credentials and behaviour toggles |

New downloads arrive at the best quality Qobuz serves for the release (24-bit up to 192 kHz, down to CD lossless). Change the tier on **Settings** or with `STREAMRIP_QUALITY` — see [Configuration](docs/configuration.md#download-quality).

## Quick start (Docker)

```bash
mkdir qobuz-librarian && cd qobuz-librarian
curl -O https://raw.githubusercontent.com/jarynclouatre/qobuz-librarian/main/compose.yaml
curl -O https://raw.githubusercontent.com/jarynclouatre/qobuz-librarian/main/.env.example
cp .env.example .env
# edit .env — at minimum, point QL_MUSIC_DIR at your music folder
docker compose up -d
```

Then open <http://localhost:8666>. The first visit sets a web username and password; sign in, paste your Qobuz token on **Settings** (see below), and search for an album.

> **Point `QL_MUSIC_DIR` at a dedicated music library**, not your home folder or a drive with other files mixed in. The app moves and merges files within that tree, and Upgrade replaces files in place.

> **On an untrusted or shared network, lock the box down before the first boot.** The default Compose publishes the port on all interfaces, and the first-visit setup screen stays open until an account exists, so whoever reaches it first claims the admin login. Seed `WEB_AUTH_USER` / `WEB_AUTH_PASSWORD` in `.env`, or bind the port to `127.0.0.1`, before you start it. On a private home LAN the risk is low.

`compose.yaml` pulls the prebuilt `latest` image from Docker Hub. On Windows, run the setup in WSL or Git Bash. To build the image yourself, see [Development](#development).

### Your Qobuz token

Auth is by token, not your password. You need a paid Qobuz account; this only downloads what your subscription entitles you to.

Get the token from the [Qobuz web player](https://play.qobuz.com): sign in, open dev tools (F12), and find Local Storage for `play.qobuz.com` (**Application** tab in Chrome/Edge, **Storage** in Firefox). Open the `localuser` entry and copy its `token` value; the `id` field next to it is your user id. Paste the token into **Auth Token** on Settings — "User ID / Email" takes either your email or that numeric id. Credentials stay in the container and go nowhere but Qobuz.

If you already run streamrip elsewhere, copy `password_or_token` from `~/.config/streamrip/config.toml` instead.

## Documentation

- **[Configuration](docs/configuration.md)** — environment variables, download quality, beets/streamrip config, NAS permissions, and what the app does on its own.
- **[Existing libraries](docs/existing-libraries.md)** — the folder layout it expects, migrating a messy collection into it, bringing your own beets database, and the first big scan.
- **[CLI](docs/cli.md)** — running the same engine from the terminal.
- **[JSON API](docs/api.md)** — read-only job state for dashboards and scripts.
- **[Troubleshooting](docs/troubleshooting.md)** — common errors and what to check.

## Security

The web UI requires sign-in by default: a single shared credential, stored as a salted PBKDF2 hash, with per-IP brute-force limiting. The bundled `compose.yaml` ships hardened (`no-new-privileges`, `cap_drop: [ALL]`, memory and PID limits, `0600` token files) and runs as `PUID:PGID` rather than root.

That's enough for a trusted network. For internet exposure, put it behind an authenticating reverse proxy, a VPN, or Tailscale rather than the built-in login alone. See [SECURITY.md](SECURITY.md), and [Configuration](docs/configuration.md#deployment) for the deployment knobs.

## Limitations

- **One library, one container.** The staging area is single-writer. The run-lock keeps the CLI and web container from clashing in one stack, but two stacks against the same mount will collide.
- **Qobuz only.** This drives streamrip's Qobuz path; Tidal, Deezer, and SoundCloud aren't wired up here.
- **No lossy output.** Downsampling stays in FLAC; there's no path to MP3 or other lossy formats.
- **Latin metadata matches best.** Fuzzy matching was tuned on Latin scripts. CJK and right-to-left titles work, but edition-stripping uses English keyword lists.
- **PWA install needs HTTPS.** The service worker only activates on HTTPS or `localhost`, so front the container with a TLS proxy to install it as an app.

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate      # PowerShell: .venv\Scripts\Activate.ps1
pip install -e ".[test]"
python -m pytest -q
```

To build the Docker image from a checkout:

```bash
git clone https://github.com/jarynclouatre/qobuz-librarian.git
cd qobuz-librarian
cp .env.example .env
docker compose -f compose.yaml -f compose.dev.yaml up -d --build
```

Running the web UI from source needs the CSS built once (`npm ci && npm run build`); the image build does this for you. `ruff check src tests` runs in CI. See [CONTRIBUTING.md](CONTRIBUTING.md) for the rest.

## Acknowledgements

Qobuz Librarian is glue around several open-source projects, bundled into the Docker image:

- **[streamrip](https://github.com/nathom/streamrip)** (nathom) — the Qobuz downloader. GPL-3.0.
- **[beets](https://beets.io/)** — tagging, cover art, library organisation. MIT.
- **[mutagen](https://github.com/quodlibet/mutagen)** — audio metadata reading/writing. GPL-2.0-or-later.
- **[FFmpeg](https://ffmpeg.org/)** — audio probing and transcoding. LGPL/GPL depending on build.
- **[FLAC](https://xiph.org/flac/)** (Xiph.Org) — integrity verification and header reads. BSD.

Lyrics come via [syncedlyrics](https://github.com/moehmeni/syncedlyrics) (LRCLIB, NetEase, Musixmatch). The web UI uses [FastAPI](https://fastapi.tiangolo.com/), [htmx](https://htmx.org/), [Tailwind CSS](https://tailwindcss.com/), and [daisyUI](https://daisyui.com/). Thanks to all their maintainers.

## License

This project's own code is **MIT** — see [LICENSE](LICENSE).

The Docker image redistributes the third-party tools above, each under its own license. Two are copyleft and coupled differently: **streamrip (GPL-3.0)** is invoked as a separate program (a subprocess), while **mutagen (GPL-2.0-or-later)** is imported as a Python library. If you redistribute the image or a derivative, honour both projects' terms.
