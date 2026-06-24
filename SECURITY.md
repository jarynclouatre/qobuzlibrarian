# Security Policy

## Reporting a vulnerability

Please report security issues privately via GitHub's [private vulnerability reporting](https://github.com/jarynclouatre/qobuz-librarian/security/advisories/new) rather than opening a public issue. I'll acknowledge reports as soon as I reasonably can.

## Scope and expectations

Qobuz Librarian is a self-hosted, single-user tool.

- **Web UI login.** Auth is on by default. On first visit the UI asks you to set one username and password; after that every route — pages, POSTs, and the JSON/SSE endpoints — requires a session. The session is an `HttpOnly`, `SameSite=Strict` cookie; the password is stored as a salted PBKDF2-SHA256 hash (never plaintext) in the data volume, written `0600`, and login does a constant-time compare. Setting `WEB_AUTH=none` turns the login off entirely — the UI logs a warning at every boot when it does. Disabling it is an explicit opt-out; a blank value leaves auth on.
- **Network binding.** By default the Docker image publishes `WEB_PORT` on `0.0.0.0`, so the UI is reachable by anything on your LAN. To keep it reachable only from the Docker host, set `WEB_BIND=127.0.0.1` in `.env` (it binds the published port to localhost). Don't use `WEB_HOST` for this on Docker — `WEB_HOST` is the in-container bind and must stay `0.0.0.0`, or the published port can't reach the app; `WEB_HOST=127.0.0.1` is only for a bare-metal/non-Docker run.
- **Internet exposure.** The built-in login is a single shared credential. It throttles repeated failures (five per hour per IP, then a `429`), which is enough for a home LAN, but if you expose the UI to the internet, still front it with a reverse proxy (or VPN / Tailscale). A minimal Caddy example:

  ```caddy
  qobuz.example.com {
      basic_auth { you $2a$14$... }
      reverse_proxy localhost:8666
  }
  ```

  Generate the bcrypt hash with `caddy hash-password`. Tailscale Serve is another low-friction option if you already run Tailscale. Note that PWA install / offline mode need HTTPS, which a TLS-terminating proxy also gives you.
- **Qobuz credentials** are stored in the config volume in plaintext, as streamrip requires them at download time. Protect that volume the same way you'd protect any other service's config.

Reports about running with `WEB_AUTH=none` on a hostile network, or about exposing the UI to the internet without a proxy, are out of scope — those are documented deployment responsibilities, not vulnerabilities.
