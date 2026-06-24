# CLI

[← README](../README.md)

The CLI runs from the same image and Compose service as the web UI, with no separate install — `docker compose run` starts a one-off container from that service that shares the same volumes, config, and download lock. It uses the same matching engine as the web app, so it finds the same gaps — it just walks them album by album with yes/no prompts instead of parking a checklist. Run it with no arguments for the menu, or pass flags for unattended runs (`--help` lists them all).

## The download lock

The web app and CLI share one download lock, so only one runs at a time. Free it before a CLI run: hand it over from **Settings → Mode → Hand off to terminal** and click **Resume web app** when you're done, or stop the web container with `docker compose stop qobuz-librarian` and `start` it after.

Set `QL_CLI_ONLY=1` for a terminal-first box that always starts handed off (the web UI still serves browsing and Settings).

## Interactive menu

```bash
docker compose run --rm -it qobuz-librarian cli
```

## Common unattended forms

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
