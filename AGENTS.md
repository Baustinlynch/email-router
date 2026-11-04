# AGENTS.md

## What this is

Secret Santa email router: a Python SMTP app (`app/main.py`, aiosmtpd) that checks
the envelope sender against a whitelist, finds a person's name in the subject, and
anonymously re-sends the body to that person's address through a real SMTP relay.
A Flask web dashboard (`app/dashboard.py` + `app/templates/`) runs in a daemon
thread in the same process; it edits the live `CFG` dict in place and persists
`config.json`, so changes apply without a restart. main.py injects cfg/helpers into
`dashboard.create_app()` — dashboard.py must NOT `import main` (main.py runs as
`__main__`; re-importing it would create a second module with its own CFG).
User-facing setup and config reference: `README.md`. Runtime data: `config.json`.

## Running and verifying

- No lint/typecheck/CI config exists. `e2e_test.py` automates the manual E2E pattern
  below (fake relay + real app subprocess + dashboard HTTP checks):
  run `.venv/bin/python e2e_test.py`.
- Local deps: `.venv/` (gitignored, host Python 3.14 — the Docker image pins 3.12-slim);
  dependencies are `aiosmtpd` and `flask`.
- `app/main.py` loads `config.json` at import time via `CONFIG_PATH` (default
  `/app/config.json`, a container path). For any local run or test, set `CONFIG_PATH`
  in the environment BEFORE importing/running `main`.
- E2E test pattern that works: run a throwaway aiosmtpd "relay catcher" on a second
  localhost port, point `config.json` `relay` at it with `"starttls": false` (that
  branch skips AUTH entirely, so no fake creds needed), start the app, send mail with
  `smtplib` to the listen port, inspect the captured `EmailMessage`.
- Docker: `docker compose up -d --build` mounts `./config.json` → `/app/config.json`
  (read-only) and maps port 1025. To test the container against a host-side fake
  relay, run with `--network host` and localhost ports in the config.

## Do not break

- Anonymity invariant: forwarded mail is rebuilt from scratch. Never forward original
  headers, the envelope sender, or raw HTML source. Trap: `str(part)` on an `email`
  object includes its headers — always use `part.get_content()`. (An earlier version
  leaked the asker's From/To/Subject into the forwarded body this way.)
- `handle_RCPT` accepts any recipient on purpose — routing is decided only by the
  subject. Gating `RCPT TO` breaks the design.
- Subject matching is plain substring over a whitespace-collapsed lowercase subject,
  longest configured name wins. There are no word boundaries; README's "Ann won't
  match Anna" claim rests on longest-wins only.
- `main()` must create its own event loop (`asyncio.new_event_loop()` +
  `asyncio.set_event_loop()`); aiosmtpd's `Controller` runs its own loop in a separate
  thread. `asyncio.get_event_loop()` raises on Python 3.12+ in this setup (already
  fixed once — don't revert).

## Gotchas

- `config.json` is user runtime data: placeholder Gmail creds and example.com
  addresses are intentional. Don't replace or "fix" them, and don't commit real
  secrets into the file.
- Whitelist matching is case-insensitive on the lowercased envelope `MAIL FROM`;
  `load_config()` lowercases the whitelist and `people` keys but not the `people`
  values (recipient addresses are used as configured).
- Dashboard: the compose mount for `config.json` is read-write (the dashboard saves
  changes) — don't re-add `:ro`. The relay password field is write-only (blank keeps
  the saved value); every POST requires the session CSRF token; the dashboard
  password is generated + logged on first start when `dashboard.password` is empty.
  `save_config()` writes a temp file then `os.replace()` — keep it atomic.
