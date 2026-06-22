# JSON API

[← README](../README.md)

The web server exposes a small read-only JSON API for home-automation dashboards, monitoring, or shell scripts that want job state without scraping HTML.

| Endpoint | Description |
|---|---|
| `GET /api/jobs` | All jobs, most recent first. Optional `?status=` (`pending`, `running`, `scanning`, `awaiting_review`, `done`, `failed`, `canceled`) and `?limit=N` (max 500, default 50). Returns `{"jobs": [...], "count": N}`. |
| `GET /api/jobs/{id}/status` | One job as JSON — list fields plus a `log_lines` array (last 50 lines). 404 if not found. |
| `GET /api/jobs/{id}/stream` | SSE stream for a live job: each log line is a `message` event, a `done` event signals completion. `progress` events and `: ping` keepalives may also appear. |

With login enabled, every endpoint needs the session cookie. A missing or invalid one returns `401 {"detail": "authentication required"}` rather than a redirect, so non-browser clients can detect the auth gate cleanly.

```bash
# Is anything currently downloading?
curl -s -b 'qf_session=<your-cookie>' http://localhost:8666/api/jobs?status=running | jq '.count'
```
