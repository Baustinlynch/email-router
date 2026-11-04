# AGENTS.md

## What this is

Secret Santa email router: a Python SMTP app (`app/main.py`, aiosmtpd) that checks
the envelope sender against a whitelist, finds a person's name in the subject, and
anonymously re-sends the body to that person's address through a real SMTP relay.
Inbound mail has two paths — the SMTP listener and an IMAP poller (`_imap_worker`
in main.py, stdlib imaplib, polls `imap.*` config, re-reads CFG each cycle so the
dashboard can toggle it live). Both paths funnel into the shared `route_message()`;
keep new routing logic there, not in the handlers. The poller skips the app's own
mail (relay username/from_address or the `X-Original-Sender-Hidden` marker) to
prevent loops, and takes the sender from `Return-Path` → `X-Envelope-From` → `From`
(IMAP has no envelope). The live account is Zoho Mail (`santa@brenden.zip`):
smtp.zoho.com:587 STARTTLS (or 465 with `relay.ssl`) + imap.zoho.com:993 SSL;
`IMAP_PASSWORD` in the ignored `.env` falls back to `RELAY_PASSWORD` when blank.
A Flask web dashboard (`app/dashboard.py` + `app/templates/`) runs in a daemon
thread in the same process; it edits the live `CFG` dict in place, persists
non-secret settings to `config.json` and passwords to `.env`, so changes apply
without a restart. main.py injects cfg/helpers into
`dashboard.create_app()` — dashboard.py must NOT `import main` (main.py runs as
`__main__`; re-importing it would create a second module with its own CFG).
User-facing setup and config reference: `README.md`. Runtime data: `config.json`.

## Running and verifying

- No lint/typecheck/CI config exists. `e2e_test.py` automates the manual E2E pattern
  below (fake relay + fake IMAP server + real app subprocess + dashboard HTTP
  checks): run `.venv/bin/python e2e_test.py`.
- Local deps: `.venv/` (gitignored, host Python 3.14 — the Docker image pins 3.12-slim);
  dependencies are `aiosmtpd` and `flask`.
- Run locally: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`,
  copy `.env.example` to `.env`, set mode `600`, then run
  `CONFIG_PATH="$PWD/config.json" SECRETS_PATH="$PWD/.env" .venv/bin/python app/main.py`
  (CONFIG_PATH must be set before import; the default is the container path
  `/app/config.json`).
  Docker: `docker compose up -d --build` (logs: `docker compose logs -f santa-router`).
- `app/main.py` loads `config.json` at import time via `CONFIG_PATH` (default
  `/app/config.json`, a container path). For any local run or test, set `CONFIG_PATH`
  in the environment BEFORE importing/running `main`.
- E2E test pattern that works: run a throwaway aiosmtpd "relay catcher" on a second
  localhost port, point `config.json` `relay` at it with `"starttls": false` (that
  branch skips AUTH entirely, so no fake creds needed), start the app, send mail with
  `smtplib` to the listen port, inspect the captured `EmailMessage`. The same test
  runs a minimal fake IMAP server (`FakeIMAPServer` in e2e_test.py) and verifies the
  poller path end to end.
- Docker: create `.env` from `.env.example` before `docker compose up -d --build`;
  Compose mounts `./config.json` and `./.env` read-write so the dashboard can save
  settings/secrets, and maps ports 1025 (SMTP) and 8080 (dashboard). To test the
  container against a host-side fake relay, run with
  `--network host` and localhost ports in the config.

## Do not break

- Anonymity invariant: forwarded mail is rebuilt from scratch. Never forward original
  headers, the envelope sender, or raw HTML source. Trap: `str(part)` on an `email`
  object includes its headers — always use `part.get_content()`. (An earlier version
  leaked the asker's From/To/Subject into the forwarded body this way.)
- `handle_RCPT` accepts any recipient on purpose — routing is decided only by the
  subject. Gating `RCPT TO` breaks the design.
- If both the SMTP listener and IMAP polling are enabled and something delivers
  the same message to both, it is routed twice — document "use one inbound path"
  (README does).
- Subject matching is plain substring over a whitespace-collapsed lowercase subject,
  longest configured name wins. There are no word boundaries; README's "Ann won't
  match Anna" claim rests on longest-wins only.
- `main()` must create its own event loop (`asyncio.new_event_loop()` +
  `asyncio.set_event_loop()`); aiosmtpd's `Controller` runs its own loop in a separate
  thread. `asyncio.get_event_loop()` raises on Python 3.12+ in this setup (already
  fixed once — don't revert).

## Gotchas

- `config.json` is user runtime data: the example.com whitelist/people addresses
  are intentional placeholders. Don't replace or "fix" them, and don't commit real
  secrets into the file.
- Whitelist matching is case-insensitive on the lowercased envelope `MAIL FROM`;
  `load_config()` lowercases the whitelist and `people` keys but not the `people`
  values (recipient addresses are used as configured).
- Dashboard: the compose mounts for `config.json` and `.env` are read-write so the
  dashboard can save settings/secrets — don't re-add `:ro`. The relay password field
  is write-only (blank keeps the saved value); every POST requires the session CSRF
  token. A missing dashboard password is generated and saved in `.env` (not logged).
  `save_config()` strips secret fields from JSON and writes `.env` with mode `0600`;
  keep config writes atomic.
- Passwords live in `.env` as `RELAY_PASSWORD`, `IMAP_PASSWORD`, and
  `DASHBOARD_PASSWORD`; `.env` is ignored by Git and Docker build context. Legacy
  password fields in `config.json` are migrated to `.env` on startup. Never commit
  real secrets. Don't "fix" the example whitelist/people addresses.
