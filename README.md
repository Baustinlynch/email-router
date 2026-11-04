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

## Quick start

1. Edit `config.json`:
   - Put your real SMTP relay details under `relay` (for Gmail use an App Password).
   - Add every allowed sender to `whitelist`.
   - Map each **name to be typed in the subject** to that person's **email**
     under `people`.
   - Set `not_found_recipient` to the organiser's email (optional safety net).
2. Start it:

   ```bash
   docker compose up -d --build
   ```

3. Point your MX (or an alias/forward rule) for the bot address at the host on
   port **1025**, or use an email-forwarding service that delivers via SMTP.
4. Open the web dashboard on port **8080** to manage everything from a browser
   (see below).

## Web dashboard

The app serves a small admin dashboard (default `0.0.0.0:8080`) where you can:

- add/remove **whitelisted sender** addresses;
- add/edit/remove **people** — the name put in the subject → their email;
- edit the **relay settings**, including the Gmail app password (write-only:
  it is never shown or sent back to the browser), with a *test relay* button
  that connects, starts TLS and logs in without sending mail;
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
| `whitelist` | Only these sender addresses may use the bot. |
| `people` | `{name: email}` — the name goes in the subject, mail goes to `email`. |
| `not_found_recipient` | Fallback address when no name matches the subject. |
| `subject_template` | Subject line of forwarded mail; `{original_subject}` is substituted. |
| `dashboard.host` / `dashboard.port` | Where the web dashboard listens (default `0.0.0.0:8080`). |
| `dashboard.password` | Dashboard login password; generated and logged on first start if empty. |

## Testing locally

There is an automated end-to-end check that boots a fake relay, starts the real
app (SMTP + dashboard) against a temp config, exercises the dashboard over HTTP
and sends mail through the SMTP path:

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

- Matching is **case-insensitive** and **whitespace-insensitive**.
- The longest name wins, so `Ann` won't wrongly match `Anna`.
- The matched name is removed from the forwarded subject; the rest of the
  subject is kept so the question stays understandable.
- The original sender's address, name, and other identifying headers are
  **not** forwarded — the mail is rebuilt from scratch by the router.
