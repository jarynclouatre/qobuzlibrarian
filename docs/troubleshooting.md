# Troubleshooting

[← README](../README.md)

| Symptom | Likely cause / next step |
|---|---|
| `Another Qobuz Librarian run is in progress` | The web container holds the download lock — use the web UI, or `docker compose stop qobuz-librarian` for the CLI. |
| `MUSIC_ROOT missing or inaccessible` | Bind mount unset or wrong — check `QL_MUSIC_DIR` in `.env` and that the host path exists. |
| Container exits immediately on `up` | `.env` missing from the compose dir, or a host bind-mount path doesn't exist. `docker compose logs qobuz-librarian` shows which. |
| `Volume not writable` (Settings → Diagnostics: FAIL) | `PUID`/`PGID` don't match the host owner — `chown -R $(id -u):$(id -g) ./music ./staging`, or set them in `.env`. |
| Library scan says "no artist folders found" | `/music` is mounted at an empty or one-level-off directory — `QL_MUSIC_DIR` must point at the artist-level folder, not its parent. |
| Token rejected (Save & connect) | Expired, copied with quotes, or trailing whitespace — re-grab it from play.qobuz.com (dev tools → Local Storage → `localuser` → `token`) and paste clean. |
| Stalls in "Importing into beets…" | A beets plugin is loaded without its required config block (lastgenre key, replaygain backend) — disable it via `BEETS_PLUGINS` or add the block to `config.yaml`. |
| `docker compose pull` 404 | Image not published under that tag yet — [build from source](../README.md#development). |
| Healthcheck failing but port reachable | Container couldn't reach its own `/healthz` — check resource limits and `docker logs qobuz-librarian`. |
| Upgrade fails with `Permission denied` backing up an album | An earlier `docker exec … beet …` ran as root, leaving root-owned files `PUID 1000` can't move — rerun with `docker exec --user 1000:1000 …`, or `sudo chown -R 1000:1000 ./music`. |
| Files vanished from `/music` after a manual `beet` command | `beet -d /config/beets …` reads `-d` as the destination, so with `move: yes` it relocates the library into the config volume. The container already exports `BEETSDIR`; run `beet …` with no `-d`. |
| `curl` / `cp` / `mkdir` misbehave on Windows | PowerShell's `curl` is an alias for `Invoke-WebRequest` with different flags, and `&&`-chained commands don't behave the same — run the setup in WSL or Git Bash. |
| CLI progress lines show `�` on Windows | The CLI uses `·` (U+00B7); enable UTF-8 mode with `chcp 65001`, or run under WSL. |
