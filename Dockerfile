# ── Builder ───────────────────────────────────────────────────────────────────
# Builds a self-contained virtualenv and the Tailwind CSS bundle. `git`,
# build-essential, and the Node toolchain live ONLY here, so they never
# reach the runtime image.
FROM python:3.12-slim AS builder

# git: for the pinned streamrip install (git+https). build-essential: some
# transitive deps compile C extensions if no wheel is available. nodejs/npm:
# Tailwind CLI for the production CSS bundle.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        build-essential \
        nodejs \
        npm \
    && rm -rf /var/lib/apt/lists/*

ENV VIRTUAL_ENV=/opt/venv
RUN python -m venv "$VIRTUAL_ENV"
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

WORKDIR /app

# Heavy upstream deps first (rarely change → cached above app source).
# streamrip 2.2.0 isn't on PyPI; pin to a known-working dev-branch commit.
# beets is pinned to a verified minor.
# Bump policy: track nathom/streamrip's dev branch, refresh quarterly or
# when a Qobuz-side schema change forces it; verify with scripts/smoke_test.sh
# before changing the SHA. Last verified: 2026-04 against streamrip dev.
#
# beets gets --no-deps + an explicit dependency list so the image stays lean
# and reproducible rather than resolving beets' optional extras we never
# enable. The list already covers everything the bundled configs touch: core
# beets, the fetchart + inline plugins (beets-default.yaml), and the
# chroma/AcoustID path — pyacoustid here, fpcalc via libchromaprint-tools in
# the runtime stage — that beets-chroma.yaml and the library-migration
# fingerprint stage depend on.
RUN pip install --no-cache-dir \
        "streamrip @ git+https://github.com/nathom/streamrip.git@e3291615ba6be34aa76df19da8aeb6f41673c6a0" \
        "syncedlyrics>=0.4" \
 && pip install --no-cache-dir --no-deps "beets==2.11.0" \
 && pip install --no-cache-dir \
        confuse \
        jellyfish \
        lap \
        mediafile \
        munkres \
        packaging \
        "pillow>=12.2.0" \
        platformdirs \
        pyacoustid \
        pyyaml \
        requests \
        requests-ratelimiter \
        typing_extensions \
        unidecode

# App + its Python deps. requirements.txt is the resolved lockfile (regenerate
# via `uv pip compile pyproject.toml`); the editable install picks up source
# without re-resolving. The .pth references /app/src — kept at the same path
# in the runtime stage.
COPY LICENSE README.md pyproject.toml requirements.txt ./
COPY src/ ./src/
RUN pip install --no-cache-dir -r requirements.txt \
 && pip install --no-cache-dir -e . --no-deps

# Tailwind production bundle: scans templates + static JS for class names,
# emits a minified CSS file shipped at /static/dist/app.css.
COPY package.json package-lock.json tailwind.config.js ./
RUN npm ci --no-audit --no-fund \
 && ./node_modules/.bin/tailwindcss \
        -i src/qobuz_librarian/web/static/src/app.css \
        -o src/qobuz_librarian/web/static/dist/app.css \
        --minify

# ── Runtime ───────────────────────────────────────────────────────────────────
# No git, no compilers, no pip caches — just the venv + ffmpeg + tiny helpers.
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="Qobuz Librarian"
LABEL org.opencontainers.image.description="Qobuz downloader + library maintenance (CLI + web UI)"
LABEL org.opencontainers.image.source="https://github.com/jarynclouatre/qobuz-librarian"
LABEL org.opencontainers.image.licenses="MIT"

# ffmpeg: rip/compress (runtime). flac: `flac -t` integrity checks and
# `metaflac` header reads — the reference tools verify frame CRCs and ignore
# embedded cover art, which ffmpeg's decoder does not. gosu: clean PUID/PGID
# drop. procps: ps/top for operators debugging from inside the container.
# libchromaprint-tools: fpcalc, for the optional beets `chroma` (AcoustID)
# plugin used to identify untagged files.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        flac \
        gosu \
        procps \
        libchromaprint-tools \
    && rm -rf /var/lib/apt/lists/*

ENV VIRTUAL_ENV=/opt/venv
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# Dedicated uid:gid (1000:1000), the default the entrypoint drops to. The
# container starts as root so the entrypoint can chown the config/data volumes
# on first run, then hands off to PUID:PGID (default 1000:1000) via gosu so the
# app never runs as root unless you explicitly ask for it (PUID=0 PGID=0).
RUN groupadd -g 1000 appuser \
 && useradd -u 1000 -g 1000 -m -s /bin/bash appuser

COPY --from=builder --chown=appuser:appuser /opt/venv /opt/venv

WORKDIR /app

# Bundled scripts ship next to /app (discovered by a parent-walk probe).
COPY --chown=appuser:appuser scripts/lyric_fetch.py ./lyric_fetch.py
COPY --chown=appuser:appuser scripts/compress.py    ./compress.py

# Source kept at /app/src so the editable install from the builder resolves.
COPY --chown=appuser:appuser pyproject.toml ./
COPY --chown=appuser:appuser src/ ./src/

# Tailwind CSS bundle built in the builder stage.
COPY --from=builder --chown=appuser:appuser \
     /app/src/qobuz_librarian/web/static/dist/ \
     /app/src/qobuz_librarian/web/static/dist/

# Default config templates (entrypoint seeds them into /config).
COPY --chown=appuser:appuser docker/beets-default.yaml /app/docker/beets-default.yaml
COPY --chown=appuser:appuser docker/beets-chroma.yaml /app/docker/beets-chroma.yaml
COPY --chown=appuser:appuser docker/streamrip-default.toml /app/docker/streamrip-default.toml
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8666

# Explicit marker for the `_in_container()` runtime check in cli.py — Docker
# also creates /.dockerenv, but this also covers Podman/Buildah/rootless.
ENV QL_IN_CONTAINER=1
# entrypoint.sh exports BEETSDIR for the PID-1 process tree, but
# `docker exec ... beet ...` doesn't inherit that. Set it at the image
# level so ad-hoc beets commands inside the container find the library
# without needing -e BEETSDIR=/config/beets every time.
ENV BEETSDIR=/config/beets

# Lets `docker compose ps` / orchestrators detect a wedged container.
# 0.0.0.0 is a bind address, not a destination, so coerce it to 127.0.0.1
# (uvicorn binds to all interfaces in that case, loopback included). A
# user who pins WEB_HOST to a specific interface gets that hostname back.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,os; h=os.environ.get('WEB_HOST','0.0.0.0'); h='127.0.0.1' if h=='0.0.0.0' else h; urllib.request.urlopen('http://'+h+':'+os.environ.get('WEB_PORT','8666')+'/healthz', timeout=4)" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["web"]
