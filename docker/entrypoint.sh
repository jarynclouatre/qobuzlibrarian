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
mkdir -p "$BEETS_DIR" "$STREAMRIP_DIR"
for d in /staging /music /data /upgrade_backups; do
    [ -d "$d" ] || mkdir -p "$d" 2>/dev/null || \
        echo "[warn] couldn't create $d — add a mount, or remove --read-only on the rootfs." >&2
done

if [ ! -f "$BEETS_DIR/config.yaml" ]; then
    echo "[init] Creating default beets config at $BEETS_DIR/config.yaml"
    cp /app/docker/beets-default.yaml "$BEETS_DIR/config.yaml"
fi

if [ ! -f "$STREAMRIP_DIR/config.toml" ]; then
    echo "[init] Creating default streamrip config at $STREAMRIP_DIR/config.toml"
    cp /app/docker/streamrip-default.toml "$STREAMRIP_DIR/config.toml"
fi

export STREAMRIP_CONFIG="$STREAMRIP_DIR/config.toml"
# So `docker exec qobuz-librarian beet …` finds the real config + DB
# instead of falling back to ~/.config/beets/ (which is empty inside the
# container). The app reads BEETS_CONFIG_DIR/BEETS_DB_PATH itself; this
# is purely for ad-hoc CLI use.
export BEETSDIR="$BEETS_DIR"

# ── Optional privilege drop (NAS-friendly) ────────────────────────────────────
# Set PUID/PGID to the owner of your media share so downloaded/imported
# files aren't root-owned. Unset = run as root (simplest; fine for a
# single-user box). gosu takes a numeric uid:gid directly — no account
# needs to exist.
APP_USER="root"
if [ -n "$PUID" ] || [ -n "$PGID" ]; then
    PUID="${PUID:-1000}"
    PGID="${PGID:-1000}"
    APP_USER="${PUID}:${PGID}"
    # Only the small, app-owned dirs are chowned. The music/staging trees
    # may be huge NAS mounts — recursively chowning them would be slow and
    # wrong; their permissions are the user's to manage on the NAS side.
    chown -R "$APP_USER" "$CONFIG_DIR" /data 2>/dev/null || true
    echo "[init] Running as ${APP_USER} (PUID/PGID)."
fi

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
        run uvicorn qobuz_fetch.web.app:app \
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
