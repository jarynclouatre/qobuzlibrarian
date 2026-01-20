# Security Policy

## Reporting a vulnerability

Please report security issues privately via GitHub's
[private vulnerability reporting](https://github.com/jarynclouatre/qobuz-librarian/security/advisories/new)
rather than opening a public issue. I'll acknowledge reports as soon as
I reasonably can.

## Scope and expectations

Qobuz Librarian is a self-hosted tool with **no built-in authentication**.
The web UI assumes it runs on a trusted network:

- By default it binds to `0.0.0.0` on the port set by `WEB_PORT`, so it is
  reachable by anything on your LAN. Set `WEB_HOST=127.0.0.1` (or bind the
  published port to `127.0.0.1` in `compose.yaml`) if you don't want that.
- Do **not** expose it directly to the internet. Put it behind a reverse
  proxy with authentication (or a VPN / Tailscale) if you need remote
  access. A minimal Caddy example:

  ```caddy
  qobuz.example.com {
      basic_auth { you $2a$14$... }
      reverse_proxy localhost:8666
  }
  ```

  Generate the bcrypt hash with `caddy hash-password`. Tailscale Serve is
  another low-friction option if you already run Tailscale.
- Your Qobuz credentials are stored in the config volume in plaintext, as
  streamrip requires them at download time. Protect that volume the same
  way you'd protect any other service's config.

Reports about the intentional lack of authentication, or about exposing
the UI to a hostile network without a proxy, are out of scope — those are
documented deployment responsibilities, not vulnerabilities.
