#!/bin/bash
set -e

CONFIG_DIR="${CONFIG_DIR:-/config}"
BEETS_DIR="$CONFIG_DIR/beets"
STREAMRIP_DIR="$CONFIG_DIR/streamrip"

# ── First-run bootstrap ───────────────────────────────────────────────────────
# /staging /music /data /upgrade_backups are normally mounted volumes — the
# mkdir is a no-op when they exist. With --read-only and a missing mount,
# `mkdir -p` would fail under `set -e`; warn instead so the writability
# diagnostics below can run and surface the real problem to the user.
mkdir -p "$BEETS_DIR" "$STREAMRIP_DIR" 2>/dev/null || \
    echo "[warn] couldn't create $CONFIG_DIR subdirs — mount /config read-write." >&2
for d in /staging /music /data /upgrade_backups; do
    [ -d "$d" ] || mkdir -p "$d" 2>/dev/null || \
        echo "[warn] couldn't create $d — add a mount, or remove --read-only on the rootfs." >&2
done

if [ ! -f "$BEETS_DIR/config.yaml" ]; then
    echo "[init] Creating default beets config at $BEETS_DIR/config.yaml"
    cp /app/docker/beets-default.yaml "$BEETS_DIR/config.yaml" 2>/dev/null || \
        echo "[warn] couldn't seed beets config — mount /config read-write." >&2
fi

if [ ! -f "$STREAMRIP_DIR/config.toml" ]; then
    echo "[init] Creating default streamrip config at $STREAMRIP_DIR/config.toml"
    cp /app/docker/streamrip-default.toml "$STREAMRIP_DIR/config.toml" 2>/dev/null || \
        echo "[warn] couldn't seed streamrip config — mount /config read-write." >&2
fi

# Enforce the streamrip settings the librarian depends on, every boot. These
# aren't user-tunable (there's no UI for them) and the /config volume persists
# across image rebuilds, so a config written by an older build must be brought
# into line rather than left stale.
#   downloads_enabled=false  — its downloads.db otherwise blocks re-download of
#     any track the user removed by hand. ^anchor avoids failed_downloads_enabled.
#   add_singles_to_folder=true — per-track gap-fill must land each track in its
#     own folder, or beets routes multiple albums into one on-disk folder.
if grep -q '^downloads_enabled = true' "$STREAMRIP_DIR/config.toml" 2>/dev/null; then
    sed -i 's/^downloads_enabled = true/downloads_enabled = false/' \
        "$STREAMRIP_DIR/config.toml"
    echo "[init] Set streamrip downloads_enabled=false (was true)."
fi
if grep -q '^add_singles_to_folder = false' "$STREAMRIP_DIR/config.toml" 2>/dev/null; then
    sed -i 's/^add_singles_to_folder = false/add_singles_to_folder = true/' \
        "$STREAMRIP_DIR/config.toml"
    echo "[init] Set streamrip add_singles_to_folder=true (was false)."
fi

# The streamrip config holds the Qobuz token once creds are set. The web/env
# write path lands 0600 (atomic mkstemp+replace); the seeded default arrives
# 0644 via cp, so bring it in line here.
chmod 600 "$STREAMRIP_DIR/config.toml" 2>/dev/null || true

export STREAMRIP_CONFIG="$STREAMRIP_DIR/config.toml"
# So `docker exec qobuz-librarian beet …` finds the real config + DB
# instead of falling back to ~/.config/beets/ (which is empty inside the
# container). The app reads BEETS_CONFIG_DIR/BEETS_DB_PATH itself; this
# is purely for ad-hoc CLI use.
export BEETSDIR="$BEETS_DIR"

# ── Privilege drop ────────────────────────────────────────────────────────────
# Run as a non-root uid:gid so downloaded/imported files aren't root-owned.
# Defaults to 1000:1000; set PUID/PGID to match the owner of your media share
# (find them with `id -u` / `id -g`). PUID=0 PGID=0 deliberately runs as root.
# gosu takes a numeric uid:gid directly — no account needs to exist.
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
APP_USER="root"
case "${PUID}${PGID}" in
    *[!0-9]*)
        # gosu needs a numeric uid:gid; a name like "appuser" would make the
        # final exec fail fatally. Warn and stay root instead.
        echo "[warn] PUID/PGID must be numeric (got ${PUID}:${PGID}); running as root." >&2
        ;;
    *)
        APP_USER="${PUID}:${PGID}"
        # Only the small, app-owned dirs are chowned. The music/staging trees
        # may be huge NAS mounts — recursively chowning them would be slow and
        # wrong; their permissions are the user's to manage.
        chown -R "$APP_USER" "$CONFIG_DIR" /data 2>/dev/null || true
        echo "[init] Running as ${APP_USER}."
        ;;
esac

# ── Writability diagnostics ───────────────────────────────────────────────────
# A NAS export that doesn't grant the run user write access is the most
# common failure; surface it clearly instead of failing cryptically later.
for d in /music /staging /data /upgrade_backups "$CONFIG_DIR"; do
    if [ "$APP_USER" = "root" ]; then
        [ -w "$d" ] && ok=yes || ok=no
    elif gosu "$APP_USER" test -w "$d" 2>/dev/null; then
        ok=yes
    else
        ok=no
    fi
    if [ "$ok" != "yes" ]; then
        echo "[warn] $d is not writable by ${APP_USER}." >&2
        echo "       On a NAS: set PUID/PGID to the share owner and grant" >&2
        echo "       that user write access, or downloads/imports will fail." >&2
    fi
done

run() {
    if [ "$APP_USER" = "root" ]; then
        exec "$@"
    else
        # gosu resets HOME to the user's passwd entry (or / for unknown UIDs).
        # Pass HOME explicitly so rip/streamrip don't try to write to /.config
        # on a read-only rootfs. APP_HOME is overridable for users running
        # --read-only with a custom tmpfs target (e.g. /var/tmp instead of
        # /tmp); /tmp is the default because every plain docker run has one.
        exec gosu "$APP_USER" env HOME="${APP_HOME:-/tmp}" "$@"
    fi
}

# ── Dispatch ──────────────────────────────────────────────────────────────────
case "${1:-web}" in
    web)
        run uvicorn qobuz_librarian.web.app:app \
            --host "${WEB_HOST:-0.0.0.0}" \
            --port "${WEB_PORT:-8666}" \
            --workers 1 \
            --no-server-header
        ;;
    cli)
        shift
        run qobuz-librarian "$@"
        ;;
    *)
        run "$@"
        ;;
esac
