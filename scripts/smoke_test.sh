#!/usr/bin/env bash
# Smoke test: build the image, boot the container, and confirm the web UI
# actually serves. This is NOT an end-to-end download test (that needs real
# Qobuz credentials) — it catches "the release is fundamentally broken"
# before you ship: the image builds, the server starts, routes respond,
# and the bundled tools (rip/beet/ffprobe) are present.
#
# Usage:  ./scripts/smoke_test.sh
# Exits non-zero on the first failure.
set -euo pipefail

# Loopback + high port so this never collides with whatever the user is
# already running on the standard host port. Override with PORT=...
IMAGE="qobuz-librarian:smoke"
NAME="qobuz-librarian-smoke"
PORT="${PORT:-18080}"
BASE="http://127.0.0.1:${PORT}"

cleanup() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "==> Building image"
docker build -t "$IMAGE" .

echo "==> Starting container"
cleanup
docker run -d --name "$NAME" -p "127.0.0.1:${PORT}:8666" "$IMAGE" >/dev/null

echo -n "==> Waiting for the web server"
up=0
for _ in $(seq 1 30); do
    if curl -fsS -o /dev/null "${BASE}/" 2>/dev/null; then
        echo " — up"
        up=1
        break
    fi
    echo -n "."
    sleep 1
done
if [ "$up" -ne 1 ]; then
    echo ""
    echo "FAIL: web server didn't respond after 30s. Container logs:"
    docker logs "$NAME" 2>&1 | tail -40
    exit 1
fi

fail() { echo "FAIL: $1"; exit 1; }

check() {
    local path="$1" expect="$2"
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' "${BASE}${path}")
    if [ "$code" != "$expect" ]; then
        fail "${path} returned ${code}, expected ${expect}"
    fi
    echo "  ok  ${path} -> ${code}"
}

echo "==> Checking routes"
check /                       200
check /search                 200
check /artist                 200
check /library                200
check /upgrade                200
check /repair                 200
check /audit                  308   # legacy alias; redirects to /repair
check /queue                  200
check /settings               200
check /static/icon.png        200
check /static/logo.png        200
check /api/jobs/nope/status   404   # unknown job id

echo "==> Checking bundled tools in the image"
for bin in rip beet ffprobe; do
    if docker exec "$NAME" sh -c "command -v $bin" >/dev/null 2>&1; then
        echo "  ok  $bin present"
    else
        fail "$bin missing from image"
    fi
done

echo "==> Checking branding"
if docker exec "$NAME" sh -c "curl -fsS http://localhost:8666/" 2>/dev/null \
     | grep -q "Qobuz Librarian"; then
    echo "  ok  page title shows 'Qobuz Librarian'"
else
    fail "branding 'Qobuz Librarian' not found in served HTML"
fi

echo
echo "SMOKE TEST PASSED"
