"""
Secret Santa Email Router
=========================
Receives email on a local SMTP port, checks the sender against a whitelist,
finds a person's name in the subject line, and anonymously forwards the
email to that person's address.

Configuration lives in config.json (mounted into the container at /app/config.json)
and can be edited from the bundled web dashboard (dashboard.py, default :8080),
which mutates the live config in place — routing changes need no restart.
"""

import asyncio
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


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    # Normalise whitelist emails to lowercase for case-insensitive matching
    cfg["whitelist"] = {addr.strip().lower() for addr in cfg.get("whitelist", [])}
    cfg["people"] = {
        name.strip().lower(): addr.strip()
        for name, addr in cfg.get("people", {}).items()
    }
    return cfg


def save_config() -> None:
    """Atomically write the live config back to disk (whitelist stored as a list)."""
    data = dict(CFG)
    data["whitelist"] = sorted(CFG["whitelist"])
    tmp = f"{CONFIG_PATH}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, CONFIG_PATH)


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
    use_tls = relay.get("starttls", True)
    if use_tls:
        with smtplib.SMTP(relay["host"], relay["port"], timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(relay["username"], relay["password"])
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(relay["host"], relay["port"], timeout=30) as smtp:
            smtp.send_message(msg)


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
        log.info("Received mail from %s", sender)

        if sender not in CFG["whitelist"]:
            log.warning("Rejected non-whitelisted sender %s", sender)
            record_event("rejected", f"{sender} is not whitelisted")
            return "550 5.7.1 Sender not whitelisted"

        try:
            original = BytesParser(policy=policy.default).parsebytes(envelope.content)
        except Exception:
            log.exception("Could not parse message from %s", sender)
            record_event("error", f"could not parse message from {sender}")
            return "550 5.6.0 Message parse error"

        subject = str(original.get("Subject", ""))
        matched_name, recipient = find_person(subject)

        if not matched_name:
            fallback = CFG.get("not_found_recipient")
            if not fallback:
                log.warning("No name matched subject %r and no fallback set", subject)
                record_event("rejected",
                             f"no name in subject {subject!r} (from {sender})")
                return "550 5.1.1 No valid recipient name in subject"
            log.info("No match for subject %r, forwarding to fallback %s",
                     subject, fallback)
            recipient = fallback
            matched_name = None

        log.info("Matched %r -> forwarding to %s", matched_name or "(fallback)", recipient)
        record_event("routed", f"{sender} → {matched_name or 'fallback'} ({recipient})")
        try:
            fwd = build_forward(original, recipient, matched_name)
            send_via_relay(fwd)
        except Exception:
            log.exception("Relay send failed for mail from %s", sender)
            record_event("error", f"relay send failed for mail from {sender}")
            return "451 4.3.0 Relay error, try again later"

        return "250 Message accepted and routed"


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
    log.info("Whitelisted senders: %s", ", ".join(sorted(CFG["whitelist"])))
    log.info("Routable people: %s", ", ".join(sorted(CFG["people"])))
    _start_dashboard()
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
