"""
Secret Santa Email Router
=========================
Receives email on a local SMTP port, checks the sender against a whitelist,
finds a person's name in the subject line, and anonymously forwards the
email to that person's address.

Configuration lives in config.json (mounted into the container at /app/config.json)
and can be edited from the bundled web dashboard (dashboard.py, default :8080),
which mutates the live config in place — routing changes need no restart.

Email-related settings (whitelist, people, not_found_recipient) are stored in
.env (a non-git-tracked file) as secrets, not in config.json.
"""

import asyncio
import imaplib
import json
import logging
import os
import re
import smtplib
import threading
import time
from collections import deque
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr

from aiosmtpd.controller import Controller

# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("santa-router")

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/app/config.json")
SECRETS_PATH = os.environ.get(
    "SECRETS_PATH", os.path.join(os.path.dirname(CONFIG_PATH), ".env"))
SECRET_FIELDS = {
    "relay": [
        ("password", "RELAY_PASSWORD"),
        ("username", "RELAY_USERNAME"),
        ("from_address", "RELAY_FROM_ADDRESS"),
    ],
    "imap": [
        ("password", "IMAP_PASSWORD"),
        ("username", "IMAP_USERNAME"),
    ],
    "dashboard": [
        ("password", "DASHBOARD_PASSWORD"),
    ],
}

# Email-related settings stored in .env (non-git-tracked) rather than config.json.
EMAIL_SECRET_FIELDS = {
    "whitelist": "WHITELIST",
    "people": "PEOPLE",
    "not_found_recipient": "NOT_FOUND_RECIPIENT",
}


def _read_secrets() -> dict[str, str]:
    values = {}
    try:
        with open(SECRETS_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        # Try to parse as JSON if it starts with [ or {
        if value.startswith('"'):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = value.strip('"')
        elif value.startswith("'") and value.endswith("'"):
            value = value[1:-1]
        elif value.startswith(("[", "{")):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                pass  # keep as string if invalid JSON
        values[key] = value
    return values


def _write_secrets(values: dict[str, str]) -> None:
    existing = _read_secrets()
    existing.update(values)
    directory = os.path.dirname(SECRETS_PATH) or "."
    os.makedirs(directory, exist_ok=True)
    with open(SECRETS_PATH, "w", encoding="utf-8") as f:
        os.chmod(SECRETS_PATH, 0o600)
        f.writelines(
            f"{key}={json.dumps(existing[key], ensure_ascii=False)}\n"
            for key in sorted(existing)
        )


def _write_config(data: dict) -> None:
    tmp = f"{CONFIG_PATH}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    try:
        os.replace(tmp, CONFIG_PATH)
    except OSError:
        # A bind-mounted config.json (e.g. the Docker compose mount) cannot be
        # replaced via rename because the mount point is busy. Fall back to
        # writing the file in place so dashboard edits still persist.
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
    finally:
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass


SERVER_LOCATIONS = {
    "com": {
        "relay": {"host": "smtp.zoho.com", "port": 587, "starttls": True, "ssl": False,
                      "username": "santa@example.com", "from_address": "Secret Santa <santa@example.com>"},
        "imap": {"host": "imap.zoho.com", "port": 993, "ssl": True,
                     "mailbox": "INBOX"},
    },
    "eu": {
        "relay": {"host": "smtppro.zoho.eu", "port": 587, "starttls": True, "ssl": False,
                  "username": "santa@example.com", "from_address": "Secret Santa <santa@example.com>"},
        "imap": {"host": "imap.zoho.eu", "port": 993, "ssl": True,
                     "mailbox": "INBOX"},
    },
    "uk": {
        "relay": {"host": "smtppro.zoho.uk", "port": 465, "starttls": False, "ssl": True,
                  "username": "santa@example.com", "from_address": "Secret Santa <santa@example.com>"},
        "imap": {"host": "imappro.zoho.uk", "port": 993, "ssl": True,
                     "mailbox": "INBOX"},
    },
}


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    file_secrets = _read_secrets()
    legacy_secrets = {}
    has_legacy_fields = False
    for section, fields in SECRET_FIELDS.items():
        section_cfg = cfg.setdefault(section, {})
        for field, key in fields:
            has_legacy_fields = has_legacy_fields or field in section_cfg
            # A saved blank value is an explicit removal, not an environment fallback.
            if key in file_secrets:
                value = file_secrets[key]
            elif key in os.environ:
                value = os.environ[key]
            else:
                value = section_cfg.get(field, "")
                if value:
                    legacy_secrets[key] = value
            section_cfg[field] = value

    # Email-related settings live in .env as secrets (not config.json).
    # Migrate any legacy config.json values into .env on startup.
    for field, key in EMAIL_SECRET_FIELDS.items():
        has_legacy_fields = has_legacy_fields or field in cfg
        if key in file_secrets:
            value = file_secrets[key]
        elif key in os.environ:
            value = os.environ[key]
        else:
            value = cfg.get(field, "")
            if value:
                legacy_secrets[key] = value
        if field == "whitelist":
            # value may be a list (from JSON) or a string (from env/legacy)
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    value = []
            cfg["whitelist"] = sorted(set(value)) if value else []
        elif field == "people":
            # value may be a dict (from JSON) or a string (from env/legacy)
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    value = {}
            cfg["people"] = {name: addr for name, addr in (value or {}).items()}
        elif field == "not_found_recipient":
            cfg["not_found_recipient"] = value or ""

    if legacy_secrets:
        _write_secrets(legacy_secrets)
    if has_legacy_fields:
        persisted_cfg = dict(cfg)
        for section, fields in SECRET_FIELDS.items():
            persisted_cfg[section] = dict(cfg[section])
            for field, _ in fields:
                persisted_cfg[section].pop(field, None)
        _write_config(persisted_cfg)
        # Remove migrated email fields from config.json
        for field in EMAIL_SECRET_FIELDS:
            persisted_cfg.pop(field, None)
        _write_config(persisted_cfg)

    # ------------------------------------------------------------------
    # Apply server-location defaults for relay & IMAP if not already set.
    # ------------------------------------------------------------------
    location = cfg.get("server_location", "uk").lower()
    defaults = SERVER_LOCATIONS.get(location, SERVER_LOCATIONS["uk"])

    # Ensure relay keys exist
    if "relay" not in cfg:
        cfg["relay"] = {}
    for k, v in defaults["relay"].items():
        if k not in cfg["relay"]:
            cfg["relay"][k] = v

    # Ensure imap keys exist
    if "imap" not in cfg:
        cfg["imap"] = {}
    for k, v in defaults["imap"].items():
        if k not in cfg["imap"]:
            cfg["imap"][k] = v

    # Normalise whitelist emails to lowercase for case-insensitive matching
    cfg["whitelist"] = {addr.strip().lower() for addr in cfg.get("whitelist", [])}
    cfg["people"] = {
        name.strip().lower(): addr.strip()
        for name, addr in cfg.get("people", {}).items()
    }
    return cfg


# --------------------------------------------------------------------------
# Recent routing events (surfaced on the web dashboard)
def save_config() -> None:
    """Persist non-secret config and credentials separately in the ignored .env."""
    secret_values = {}
    data = dict(CFG)
    for section, fields in SECRET_FIELDS.items():
        section_data = dict(CFG.get(section, {}))
        for field, key in fields:
            secret_values[key] = section_data.pop(field, "") or ""
        data[section] = section_data

    # Email-related settings are stored in .env only (not config.json).
    # Do NOT persist whitelist/people/not_found_recipient to config.json.
    secret_values["WHITELIST"] = sorted(CFG.get("whitelist", []))
    secret_values["PEOPLE"] = CFG.get("people", {})
    secret_values["NOT_FOUND_RECIPIENT"] = CFG.get("not_found_recipient", "")

    _write_secrets(secret_values)
    # Strip email fields from config data so they are not written to config.json
    data.pop("whitelist", None)
    data.pop("people", None)
    data.pop("not_found_recipient", None)
    _write_config(data)


CFG = load_config()


# --------------------------------------------------------------------------
# Recent routing events (surfaced on the web dashboard)
# --------------------------------------------------------------------------
EVENTS: deque = deque(maxlen=20)
_EVENTS_LOCK = threading.Lock()


def record_event(kind: str, detail: str) -> None:
    with _EVENTS_LOCK:
        EVENTS.appendleft(
            {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind, "detail": detail}
        )


def events_snapshot() -> list:
    with _EVENTS_LOCK:
        return list(EVENTS)


# --------------------------------------------------------------------------
# Email routing logic
# --------------------------------------------------------------------------
def normalise(name: str) -> str:
    """Lowercase and collapse whitespace so '  Bob   Smith ' matches 'bob smith'."""
    return re.sub(r"\s+", " ", name.strip().lower())


def find_person(subject: str) -> tuple[str | None, str | None]:
    """
    Return (matched_name, recipient_address) if the subject contains one of
    the configured people's names. Longest name wins to avoid partial matches
    (e.g. 'Ann' matching inside 'Anna').
    """
    subject_norm = normalise(subject)
    for name in sorted(CFG["people"], key=len, reverse=True):
        if name in subject_norm:
            return name, CFG["people"][name]
    return None, None


def _html_to_text(src: str) -> str:
    """Crude HTML -> text so we never forward raw HTML source either."""
    from html.parser import HTMLParser

    class _Extract(HTMLParser):
        def __init__(self):
            super().__init__()
            self.buf = []

        def handle_data(self, data):
            self.buf.append(data)

    p = _Extract()
    try:
        p.feed(src)
        p.close()
    except Exception:
        log.exception("HTML text extraction failed, returning raw source")
        return src
    return re.sub(r"\n{3,}", "\n\n", "".join(p.buf)).strip()


def build_forward(original: EmailMessage, recipient: str,
                  matched_name: str | None) -> EmailMessage:
    """Build a new, anonymous message forwarding the original content."""
    original_subject = str(original.get("Subject", "")).strip()
    subject_template = CFG.get("subject_template", "{original_subject}")

    # Strip the matched name from the subject so it stays anonymous-ish,
    # but keep the rest of the text.
    cleaned = original_subject
    if matched_name:
        cleaned = re.sub(re.escape(matched_name), "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+([,;:.!?])", r"\1", cleaned)   # "Hey ," -> "Hey,"
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -_[](),:;") or "(no subject)"

    fwd = EmailMessage()
    fwd["From"] = CFG["relay"]["from_address"]
    fwd["To"] = recipient
    fwd["Subject"] = subject_template.format(original_subject=cleaned)
    fwd["X-Original-Sender-Hidden"] = "true"  # marker, no real address leaked

    # Rebuild ONLY the body text — deliberately not the original headers or
    # HTML source, so nothing in the forwarded mail identifies the sender.
    body_part = original.get_body(preferencelist=("plain", "html"))
    if body_part is not None:
        body = body_part.get_content()
        if body_part.get_content_type() == "text/html":
            body = _html_to_text(body)
    else:
        parts = [p.get_content() for p in original.walk()
                 if p.get_content_type() == "text/plain"]
        if not parts:
            parts = [_html_to_text(p.get_content()) for p in original.walk()
                     if p.get_content_type() == "text/html"]
        body = "\n".join(parts).strip() or "(empty message)"
    fwd.set_content(body)
    return fwd


def send_via_relay(msg: EmailMessage) -> None:
    relay = CFG["relay"]
    host = relay["host"]
    port = int(relay["port"])
    if relay.get("ssl"):
        # Implicit TLS, e.g. Zoho on port 465.
        with smtplib.SMTP_SSL(host, port, timeout=30) as smtp:
            smtp.ehlo()
            smtp.login(relay["username"], relay["password"])
            smtp.send_message(msg)
    elif relay.get("starttls", True):
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(relay["username"], relay["password"])
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.send_message(msg)


def send_acknowledgement(sender: str, success: bool, person: str | None = None,
                         reason: str | None = None, *, include_names: bool = False) -> None:
    """Tell a whitelisted sender whether their message was forwarded."""
    sender = parseaddr(sender)[1].strip().lower()
    if not sender:
        return

    msg = EmailMessage()
    msg["From"] = CFG["relay"]["from_address"]
    msg["To"] = sender
    msg["Auto-Submitted"] = "auto-replied"
    if success:
        target = person or "the organiser (fallback recipient)"
        msg["Subject"] = "Your Secret Santa email was sent"
        body = f"Your email was successfully forwarded to {target}."
    else:
        msg["Subject"] = "Your Secret Santa email was not sent"
        body = f"Your email could not be sent. {reason or 'Please try again later.'}"

    if include_names:
        names = sorted(CFG.get("people", {}))
        if names:
            body += ("\n\nNo configured person's name was found in your subject. "
                     "Please use one of these names in the subject and send again:\n\n"
                     + "\n".join(f"- {name.title()}" for name in names))
        else:
            body += "\n\nNo recipient names are configured yet. Please contact the organiser."
    msg.set_content(body)

    try:
        send_via_relay(msg)
    except Exception:
        log.exception("Could not send routing acknowledgement to %s", sender)


# --------------------------------------------------------------------------
# Routing (shared by the SMTP listener and the IMAP poller)
# --------------------------------------------------------------------------
def _effective_whitelist() -> set:
    """Compute the effective whitelist: explicit whitelist entries plus all people's emails (lowercased, stripped)."""
    explicit = set(CFG["whitelist"])
    for email in CFG["people"].values():
        explicit.add(email.strip().lower())
    return explicit


def route_message(original: EmailMessage, sender: str) -> str:
    """Route one parsed message. Returns an SMTP-style status string."""
    sender = (sender or "").strip().lower()
    log.info("Received mail from %s", sender)

    if sender not in _effective_whitelist():
        log.warning("Rejected non-whitelisted sender %s", sender)
        record_event("rejected", f"{sender} is not whitelisted")
        return "550 5.7.1 Sender not whitelisted"

    subject = str(original.get("Subject", ""))
    matched_name, recipient = find_person(subject)

    if not matched_name:
        fallback = CFG.get("not_found_recipient")
        if not fallback:
            log.warning("No name matched subject %r and no fallback set", subject)
            record_event("rejected",
                         f"no name in subject {subject!r} (from {sender})")
            send_acknowledgement(
                sender, False,
                reason=("no configured person's name was found in the subject, "
                        "and no fallback recipient is configured."),
                include_names=True,
            )
            return "550 5.1.1 No valid recipient name in subject"
        log.info("No match for subject %r, forwarding to fallback %s",
                 subject, fallback)
        recipient = fallback
        matched_name = None

    assert recipient is not None
    log.info("Matched %r -> forwarding to %s", matched_name or "(fallback)", recipient)
    try:
        fwd = build_forward(original, recipient, matched_name)
        send_via_relay(fwd)
    except Exception:
        log.exception("Relay send failed for mail from %s", sender)
        record_event("error", f"relay send failed for mail from {sender}")
        target = matched_name.title() if matched_name else "the organiser (fallback recipient)"
        send_acknowledgement(
            sender, False, reason=f"the router could not forward it to {target}. Please try again later.",
            include_names=not matched_name,
        )
        return "451 4.3.0 Relay error, try again later"

    record_event("routed", f"{sender} → {matched_name or 'fallback'} ({recipient})")
    send_acknowledgement(sender, True, matched_name.title() if matched_name else None,
                         include_names=not matched_name)
    return "250 Message accepted and routed"


# --------------------------------------------------------------------------
# IMAP polling (inbound mail from a real mailbox, e.g. Zoho Mail)
# --------------------------------------------------------------------------
def _message_sender(original: EmailMessage) -> str:
    """Best-effort envelope sender for mail fetched over IMAP."""
    for header in ("Return-Path", "X-Envelope-From", "From"):
        value = original.get(header)
        if not value:
            continue
        addr = parseaddr(str(value))[1].strip().lower()
        if addr:
            return addr
    return ""


def _is_own_message(original: EmailMessage, sender: str) -> bool:
    """True for mail this app sent itself, so IMAP polling cannot loop."""
    relay = CFG.get("relay") or {}
    own = set()
    username = str(relay.get("username", "")).strip().lower()
    if username:
        own.add(username)
    from_addr = parseaddr(str(relay.get("from_address", "")))[1].strip().lower()
    if from_addr:
        own.add(from_addr)
    if sender and sender in own:
        return True
    return bool(original.get("X-Original-Sender-Hidden"))


def _poll_imap_once(imap_cfg: dict) -> None:
    relay = CFG.get("relay") or {}
    host = imap_cfg.get("host") or ""
    port = int(imap_cfg.get("port") or 993)
    username = imap_cfg.get("username") or relay.get("username", "")
    password = imap_cfg.get("password") or relay.get("password", "")
    mailbox = imap_cfg.get("mailbox") or "INBOX"
    if not username or not password:
        log.warning("IMAP enabled but no username/password configured; skipping")
        return

    if imap_cfg.get("ssl", True):
        conn = imaplib.IMAP4_SSL(host, port, timeout=30)
    else:
        conn = imaplib.IMAP4(host, port, timeout=30)
    try:
        conn.login(username, password)
        typ, _ = conn.select(mailbox, readonly=False)
        if typ != "OK":
            log.warning("IMAP SELECT %s failed: %s", mailbox, typ)
            return
        typ, data = conn.uid("search", "UNSEEN")
        if typ != "OK":
            log.warning("IMAP SEARCH failed: %s", typ)
            return
        uids = data[0].split() if data and data[0] else []
        for uid in uids:
            typ, msg_data = conn.uid("fetch", uid, "(RFC822)")
            if typ != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                log.warning("IMAP FETCH %s failed: %s", uid, typ)
                continue
            raw = msg_data[0][1]
            try:
                original = BytesParser(policy=policy.default).parsebytes(raw)
            except Exception:
                log.exception("Could not parse IMAP message %s", uid)
                record_event("error", f"could not parse IMAP message {uid!r}")
                if imap_cfg.get("mark_seen", True):
                    conn.uid("store", uid, "+FLAGS", "(\\Seen)")
                continue

            sender = _message_sender(original)
            if _is_own_message(original, sender):
                log.info("Skipping own/forwarded IMAP message from %s", sender or "?")
            else:
                status = route_message(original, sender)
                log.info("IMAP message %s from %s -> %s",
                         uid.decode(errors="replace"), sender or "?", status)

            if imap_cfg.get("mark_seen", True):
                conn.uid("store", uid, "+FLAGS", "(\\Seen)")
    finally:
        try:
            conn.logout()
        except Exception:
            log.exception("IMAP logout failed")


def _imap_worker() -> None:
    """Poll the configured mailbox forever; re-reads config each cycle."""
    while True:
        imap_cfg = CFG.get("imap") or {}
        if not imap_cfg.get("enabled"):
            time.sleep(5)
            continue
        try:
            _poll_imap_once(imap_cfg)
        except Exception:
            log.exception("IMAP poll failed")
            record_event("error", "IMAP poll failed")
        time.sleep(max(1, int(imap_cfg.get("poll_seconds", 30))))


# --------------------------------------------------------------------------
# SMTP server handler
# --------------------------------------------------------------------------
class SantaHandler:
    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):
        # Accept mail addressed to anything; routing is decided by subject.
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(self, server, session, envelope):
        sender = (envelope.mail_from or "").strip().lower()

        # Check the whitelist before parsing untrusted content.
        if sender not in _effective_whitelist():
            log.warning("Rejected non-whitelisted sender %s", sender)
            record_event("rejected", f"{sender} is not whitelisted")
            return "550 5.7.1 Sender not whitelisted"

        try:
            original = BytesParser(policy=policy.default).parsebytes(envelope.content)
        except Exception:
            log.exception("Could not parse message from %s", sender)
            record_event("error", f"could not parse message from {sender}")
            return "550 5.6.0 Message parse error"

        return route_message(original, sender)


def _start_dashboard() -> None:
    # Imported here, not at the top: main.py runs as __main__, and importing it
    # again as "main" would create a second module with its own copy of CFG.
    try:
        from dashboard import create_app
    except ImportError:  # e.g. started as `python -m app.main`
        from app.dashboard import create_app

    dash = CFG.get("dashboard", {})
    dash_host = dash.get("host", "0.0.0.0")
    dash_port = int(dash.get("port", 8080))
    app = create_app(cfg=CFG, save_config=save_config,
                     events=events_snapshot, normalise=normalise)
    threading.Thread(
        target=app.run,
        kwargs={"host": dash_host, "port": dash_port, "debug": False,
                "use_reloader": False, "threaded": True},
        name="dashboard", daemon=True,
    ).start()
    log.info("Dashboard listening on http://%s:%s", dash_host, dash_port)


def main():
    host = CFG.get("listen_host", "0.0.0.0")
    port = int(CFG.get("listen_port", 1025))
    controller = Controller(SantaHandler(), hostname=host, port=port)
    controller.start()
    log.info("Secret Santa router listening on %s:%s", host, port)
    log.info("Whitelisted senders: %s", ", ".join(sorted(_effective_whitelist())))
    log.info("Routable people: %s", ", ".join(sorted(CFG["people"])))
    _start_dashboard()

    imap_cfg = CFG.get("imap") or {}
    if imap_cfg.get("enabled"):
        log.info("IMAP polling enabled: %s:%s (%s)",
                 imap_cfg.get("host", "?"), imap_cfg.get("port", "?"),
                 imap_cfg.get("mailbox", "INBOX"))
    else:
        log.info("IMAP polling disabled")
    threading.Thread(target=_imap_worker, name="imap-poller", daemon=True).start()
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        controller.stop()
        log.info("Shut down")


if __name__ == "__main__":
    main()
