# ── Builder ───────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

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
# beets gets --no-deps + a minimum-deps list. The full install pulls in
# chroma/acoustid prerequisites we never load.
# Pillow is pinned past the 10.4.x CVE family.
RUN pip install --no-cache-dir \
        "streamrip @ git+https://github.com/nathom/streamrip.git@e3291615ba6be34aa76df19da8aeb6f41673c6a0" \
        "syncedlyrics>=0.4" \
        "pillow>=12.2.0" \
 && pip install --no-cache-dir --no-deps "beets==2.11.0" \
 && pip install --no-cache-dir \
        confuse \
        jellyfish \
        lap \
        mediafile \
        munkres \
        packaging \
        platformdirs \
        pyyaml \
        requests \
        requests-ratelimiter \
        typing_extensions \
        unidecode

COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY LICENSE README.md pyproject.toml ./
COPY src/ ./src/
COPY tailwind.config.js ./

RUN npx --no-install @tailwindcss/cli \
        -i src/qobuz_fetch/web/static/src/app.css \
        -o src/qobuz_fetch/web/static/dist/app.css \
        --minify

RUN pip install --no-cache-dir -e .

# ── Runtime ───────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="Qobuz Librarian"
LABEL org.opencontainers.image.description="Qobuz downloader and library manager."
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.source="https://github.com/jarynclouatre/qobuz-librarian"

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        gosu \
        procps \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -g 1000 appuser && useradd -u 1000 -g 1000 -m -s /bin/bash appuser

ENV VIRTUAL_ENV=/opt/venv
ENV PATH="$VIRTUAL_ENV/bin:$PATH"
ENV QF_IN_CONTAINER=1

COPY --chown=appuser:appuser --from=builder /opt/venv /opt/venv
COPY --chown=appuser:appuser --from=builder /app/src /app/src
COPY --chown=appuser:appuser --from=builder /app/pyproject.toml /app/LICENSE /app/README.md /app/

WORKDIR /app

COPY --chown=appuser:appuser docker/streamrip-default.toml /app/docker/streamrip-default.toml
COPY --chown=appuser:appuser docker/beets-default.yaml /app/docker/beets-default.yaml

EXPOSE 8666

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8666/healthz', timeout=2).status == 200 else 1)" || exit 1

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["web"]
