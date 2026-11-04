# Secret Santa Email Router

A Dockerised SMTP app that lets a **whitelist** of senders ask **anonymous questions**
to people in a Secret Santa gift exchange.

## How it works

1. A whitelisted sender sends an email to your bot's address
   (e.g. `santa@yourdomain.com`).
2. They put the **recipient's name** from the configured list in the subject,
   e.g. *"What size shirt do you wear, Bob?"*
3. The app matches the name (`bob`) in the subject, strips it from the subject,
   rewrites the headers so it comes from the bot's address (fully anonymous),
   and forwards it to Bob's secret Santa email (`bob.santa@example.com`).
4. Non-whitelisted senders are rejected. Subjects that match no name are
   forwarded to a fallback (e.g. the organiser) or rejected.

The app can receive mail two ways — use **one** of them, not both:

- **IMAP polling** (default): the app logs into a real mailbox (e.g. Zoho Mail)
  every `poll_seconds`, fetches unread mail and routes it. No MX changes needed.
- **SMTP listener**: something delivers mail to the app's port **1025**
  (MX record or a forwarding service). Keep `imap.enabled` off if you use this,
  or the same message may be routed twice.

Outbound mail always goes through the configured SMTP relay.

## Quick start

1. Edit `config.json`:
   - Put your real SMTP relay details under `relay` (for Zoho/Gmail use an app
     password — see the Zoho section below).
   - Add every allowed sender to `whitelist`.
   - Map each **name to be typed in the subject** to that person's **email**
     under `people`.
   - Set `not_found_recipient` to the organiser's email (optional safety net).
2. Start it:

   ```bash
   docker compose up -d --build
   ```

3. Get mail **in**: either enable IMAP polling (below) or point your MX (or an
   alias/forward rule) for the bot address at the host on port **1025**.
4. Open the web dashboard on port **8080** to manage everything from a browser
   (see below).

## Running the app

### Docker (recommended)

```bash
# build and start in the background
docker compose up -d --build

# follow the logs (the generated dashboard password is logged here)
docker compose logs -f santa-router

# stop it
docker compose down
```

`docker-compose.yml` mounts `./config.json` into the container and publishes
ports **1025** (SMTP) and **8080** (dashboard).

### Locally, without Docker

Requires Python 3.12+ (the Docker image uses `python:3.12-slim`).

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# CONFIG_PATH must be set before the app starts; the default is /app/config.json
CONFIG_PATH="$PWD/config.json" .venv/bin/python app/main.py
```

The app then listens on `0.0.0.0:1025` (SMTP) and serves the dashboard at
`http://localhost:8080`. Press Ctrl+C to stop. On Windows use
`.venv\Scripts\python` instead of `.venv/bin/python`.

If `dashboard.password` is empty, the generated password is printed in the log
on first start.

## Using Zoho Mail (santa@brenden.zip)

The shipped `config.json` is already pointed at Zoho; you only need to supply
the password and enable IMAP in Zoho:

1. In Zoho Mail: **Settings → Mail Accounts → IMAP → Enable IMAP access**.
2. If the account has two-factor authentication, create an **app-specific
   password** (Zoho account → Security → App Passwords) and use it as the
   password below; otherwise the normal account password works.
3. On the dashboard (or in `config.json`), set the **relay** password:
   host `smtp.zoho.com`, port `587`, STARTTLS on (or port `465` with SSL on),
   username `santa@brenden.zip`.
4. The **IMAP** section reads the same username/password by default:
   host `imap.zoho.com`, port `993`, SSL on, mailbox `INBOX`,
   `enabled: true`. Use the *Test saved IMAP settings* button to verify.
5. Add the real participants' addresses to `whitelist` and `people`.

Notes:

- Zoho's free plan includes IMAP/POP/SMTP access; if the IMAP test fails with
  an auth error, re-check step 1 and 2.
- With IMAP polling you do **not** point MX at this host — Zoho receives the
  mail and the app fetches it. Leave the SMTP listener free for local tests.
- The poller skips messages the app sent itself (its own From address or the
  `X-Original-Sender-Hidden` marker), so forwarded mail cannot loop back.
- Handled messages are marked read (`mark_seen`) so they are not re-processed.

## Web dashboard

The app serves a small admin dashboard (default `0.0.0.0:8080`) where you can:

- add/remove **whitelisted sender** addresses;
- add/edit/remove **people** — the name put in the subject → their email;
- edit the **relay settings**, including the app password (write-only:
  it is never shown or sent back to the browser), with a *test relay* button
  that connects, starts TLS and logs in without sending mail;
- edit the **IMAP settings** (mailbox polling for incoming mail), with a
  *test IMAP* button that logs in and opens the mailbox without reading mail;
- change the fallback recipient and subject template;
- see the last 20 emails the router handled (routed / fallback / rejected).

Changes are saved straight to `config.json` and apply immediately — no restart
needed.

**Dashboard password:** if `dashboard.password` is empty on first start, the
app generates one, saves it to `config.json` and logs it — read it with
`docker compose logs santa-router | grep dashboard`. Change it on the
dashboard's *Dashboard access* card. Sessions end when the app restarts.

**Security notes:** the dashboard is protected by that single password, a
session cookie and CSRF tokens, but it is plain HTTP — keep the port private
(firewall/VPN) or put it behind a TLS reverse proxy. `config.json` is mounted
read-write in `docker-compose.yml` so the dashboard can save changes.

## Config reference

| Key | Meaning |
| --- | --- |
| `listen_host` / `listen_port` | Where the app accepts incoming SMTP. |
| `relay` | Real SMTP server used to deliver the forwarded mail. |
| `relay.from_address` | The `From:` shown on forwarded mail (the anonymous identity). |
| `imap.enabled` | Poll a mailbox for incoming mail instead of (or in addition to) the SMTP listener. |
| `imap.host` / `imap.port` / `imap.ssl` | IMAP server to poll (Zoho: `imap.zoho.com`, `993`, SSL). |
| `imap.username` / `imap.password` | IMAP login; blank falls back to the relay credentials. |
| `imap.mailbox` | Mailbox to poll (default `INBOX`). |
| `imap.poll_seconds` | Seconds between polls (minimum 5 via the dashboard). |
| `imap.mark_seen` | Mark handled mail as read so it is not processed twice. |
| `whitelist` | Only these sender addresses may use the bot. |
| `people` | `{name: email}` — the name goes in the subject, mail goes to `email`. |
| `not_found_recipient` | Fallback address when no name matches the subject. |
| `subject_template` | Subject line of forwarded mail; `{original_subject}` is substituted. |
| `dashboard.host` / `dashboard.port` | Where the web dashboard listens (default `0.0.0.0:8080`). |
| `dashboard.password` | Dashboard login password; generated and logged on first start if empty. |

## Testing locally

There is an automated end-to-end check that boots a fake relay and a fake IMAP
server, starts the real app (SMTP + dashboard) against a temp config, exercises
the dashboard over HTTP, and sends mail through both the SMTP and IMAP paths:

```bash
.venv/bin/python e2e_test.py
```

You can also send a test mail to the container with Python:

```bash
python3 - <<'EOF'
import smtplib
s = smtplib.SMTP("localhost", 1025)
s.sendmail("alice@example.com", ["santa@yourdomain.com"],
           "From: alice@example.com\r\nTo: santa@yourdomain.com\r\n"
           "Subject: Question for Bob\r\n\r\nWhat size shirt do you wear?")
s.quit()
EOF
```

## Notes

- IMAP-fetched mail has no SMTP envelope, so the sender is taken from
  `Return-Path` (falling back to `From`) — the same whitelist applies.
- Matching is **case-insensitive** and **whitespace-insensitive**.
- The longest name wins, so `Ann` won't wrongly match `Anna`.
- The matched name is removed from the forwarded subject; the rest of the
  subject is kept so the question stays understandable.
- The original sender's address, name, and other identifying headers are
  **not** forwarded — the mail is rebuilt from scratch by the router.
