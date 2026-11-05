# AGENTS.md — Secret Santa Email Router

## What this is

A single-process Python app (`app/main.py`, aiosmtpd + Flask) that checks the envelope sender against a whitelist, finds a person's name in the subject, and anonymously forwards the body to that person's address through a real SMTP relay. A Flask dashboard runs in a daemon thread and mutates the live config in place. An IMAP poller (`_imap_worker` in main.py) re-reads `CFG` each cycle so dashboard toggles apply live.

## Architecture — things an agent will miss

- **`main()` must create its own event loop**: `asyncio.new_event_loop()` + `asyncio.set_event_loop()`. `asyncio.get_event_loop()` raises on Python 3.12+. Do not revert.
- **`dashboard.py` must NOT `import main`**: main.py runs as `__main__`; re-importing creates a second module with its own `CFG`. `main()` injects `cfg`/`helpers` into `dashboard.create_app(cfg=CFG, ...)`.
- **All routing funnels through `route_message()`**: SMTP handler, IMAP poller, and dashboard all share this one function. Add new routing logic there, not in handlers.
- **`handle_RCPT` accepts any recipient on purpose**: routing is decided only by the subject. Gating `RCPT TO` breaks the design.
- **Two inbound paths**: SMTP listener and IMAP polling. If both are enabled and the same message arrives both ways, it is routed twice — use one inbound path.
- **IMAP poller skips the app's own mail**: matches against relay `username`/`from_address`, or `X-Original-Sender-Hidden` marker.

## Anonymity invariant (never break)

- Forwarded mail is rebuilt from scratch. **Never** forward original headers, the envelope sender, or raw HTML source.
- Trap: `str(part)` on an `email` object includes its headers — always use `part.get_content()` (an earlier version leaked the asker's From/To/Subject this way).
- Forwarded mail gets `X-Original-Sender-Hidden: true` (a marker, no real address) — the IMAP poller skips messages carrying it to prevent loops.

## Subject matching

- Plain substring over a whitespace-collapsed, lowercased subject. **No word boundaries.**
- Longest configured name wins (e.g. `Ann` won't match `Anna` — that claim rests on longest-wins only).
- Matched name is stripped from the subject; the rest is kept.

## Whitelist

- Envelope `MAIL FROM` is lowercased; matching is case-insensitive.
- Every `people` email is automatically whitelisted in addition to explicit `whitelist` entries.

## Config vs secrets

- `config.json` = non-secret runtime data only. The example.com whitelist/people addresses are intentional placeholders — don't "fix" them, don't commit real secrets.
- `.env` = `RELAY_PASSWORD`, `IMAP_PASSWORD`, `DASHBOARD_PASSWORD`, `WHITELIST`, `PEOPLE`, `NOT_FOUND_RECIPIENT`. Ignored by Git and Docker build context. Never commit it.
- `load_config()` precedence per secret: saved `.env` entry → environment variable → legacy `config.json` field (migrated to `.env` on startup). A saved blank `.env` value is an explicit removal/reset, not an env fallback.
- `save_config()` strips secrets from JSON, writes `.env` with mode `0600`. Keep config writes atomic.

## Dashboard behavior

- Every POST requires the session CSRF token (missing → 400).
- Password fields are write-only: blank keeps the saved value. Use `clear_password` to remove the relay password; use `use_relay_credentials` to reset IMAP overrides to relay creds.
- Changes apply immediately — no restart needed (IMAP poller picks them up next cycle).
- The dashboard is plain HTTP; keep port 8080 behind a firewall/VPN or TLS reverse proxy.
- The dashboard exposes email fields (`WHITELIST`, `PEOPLE`, `NOT_FOUND_RECIPIENT`) as editable values — they are stored in `.env` and should be treated as runtime configuration, not secrets.

## IMAP poller

- Default enabled; polls every `poll_seconds` (minimum 5 via dashboard), marks handled mail seen (`mark_seen`).
- Skips the app's own mail (relay username/from_address match, or `X-Original-Sender-Hidden` marker).
- If both SMTP listener and IMAP polling are enabled and the same message arrives both ways, it is routed twice — use one inbound path.

## Running and verifying

- **No lint/typecheck/CI config exists.** Dependencies are `aiosmtpd` and `flask`. `.venv/` is gitignored.
- Run locally: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`, copy `.env.example` to `.env`, `chmod 600 .env`, then `CONFIG_PATH="$PWD/config.json" SECRETS_PATH="$PWD/.env" .venv/bin/python app/main.py` (CONFIG_PATH must be set before import; defaults are `/app/config.json` and `/app/.env` container paths — wrong for local dev).
- App listens on `0.0.0.0:1025` (SMTP) and `0.0.0.0:8080` (dashboard).
- Docker: `docker compose up -d --build`, logs `docker compose logs -f santa-router`. Compose mounts `config.json` and `.env` read-write — don't add `:ro`. Ports 1025/8080 mapped to host.
- E2E test: `.venv/bin/python e2e_test.py` (spins a fake relay + fake IMAP server, runs the real app subprocess, checks dashboard HTTP).