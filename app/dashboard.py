"""
Web dashboard for the Secret Santa router.

A small Flask app run in a daemon thread inside the SMTP process. It edits the
live CFG dict in place and persists regular settings to config.json and passwords
to the ignored .env file, so changes take effect immediately — no restart needed.

main.py injects the config dict and helpers into create_app() instead of this
module importing them: main.py runs as __main__, so `import main` from here
would re-execute the file as a second module with its own copy of CFG.
"""

import imaplib
import logging
import re
import secrets
import smtplib
import time

from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

log = logging.getLogger("santa-router.dashboard")


def create_app(cfg: dict, save_config, events, normalise) -> Flask:
    """
    cfg:         the live config dict shared with the SMTP handler (edited in place)
    save_config: callable persisting cfg to disk
    events:      callable returning a snapshot of recent routing events
    normalise:   the router's name normalisation (lowercase + collapse whitespace)
    """
    app = Flask(__name__)
    app.secret_key = secrets.token_hex(32)  # per-process: logins end on restart
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    _ensure_password(cfg, save_config)

    # ------------------------------------------------------------------ auth
    def _password() -> str:
        return (cfg.get("dashboard") or {}).get("password") or ""

    @app.before_request
    def gate():
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(16)
        if request.method == "POST" and request.form.get("csrf") != session["csrf"]:
            abort(400, description="Bad or missing CSRF token")
        if request.endpoint != "login" and not session.get("authed"):
            return redirect(url_for("login"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if session.get("authed"):
            return redirect(url_for("index"))
        if request.method == "POST":
            guess = request.form.get("password", "").encode()
            if secrets.compare_digest(guess, _password().encode()):
                session["authed"] = True
                return redirect(url_for("index"))
            time.sleep(1)  # blunt brute-force brake
            flash("Wrong password.", "error")
        return render_template("login.html")

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # -------------------------------------------------------------- helpers
    def valid_email(addr: str) -> bool:
        return re.fullmatch(r"[^@\s]+@[^@\s]+", addr) is not None

    def save() -> bool:
        try:
            save_config()
            return True
        except OSError as exc:
            flash(f"Applied for this run, but could not persist settings: {exc}",
                  "error")
            return False

    # ------------------------------------------------------------- dashboard
    @app.route("/")
    def index():
        relay = cfg.get("relay", {})
        return render_template(
            "index.html",
            whitelist=sorted(cfg.get("whitelist", set())),
            people=sorted(cfg.get("people", {}).items()),
            relay=relay,
            relay_has_password=bool(relay.get("password")),
            imap=cfg.get("imap", {}),
            not_found_recipient=cfg.get("not_found_recipient", ""),
            subject_template=cfg.get("subject_template", ""),
            listen_host=cfg.get("listen_host", "?"),
            listen_port=cfg.get("listen_port", "?"),
            events=events(),
        )

    # ------------------------------------------------------------- whitelist
    @app.post("/whitelist/add")
    def whitelist_add():
        addr = request.form.get("email", "").strip().lower()
        if not valid_email(addr):
            flash("That doesn't look like an email address.", "error")
        elif addr in cfg["whitelist"]:
            flash(f"{addr} is already whitelisted.", "error")
        else:
            cfg["whitelist"].add(addr)
            if save():
                flash(f"Added {addr} to the whitelist.")
        return redirect(url_for("index"))

    @app.post("/whitelist/remove")
    def whitelist_remove():
        addr = request.form.get("email", "").strip().lower()
        if addr in cfg["whitelist"]:
            cfg["whitelist"].discard(addr)
            if save():
                flash(f"Removed {addr} from the whitelist.")
        return redirect(url_for("index"))

    # ---------------------------------------------------------------- people
    @app.post("/people/add")
    def people_add():
        name = normalise(request.form.get("name", ""))
        email = request.form.get("email", "").strip()
        if not name:
            flash("Give the person a name — it goes in the email subject.", "error")
        elif not valid_email(email):
            flash("That doesn't look like an email address.", "error")
        elif name in cfg["people"]:
            flash(f"{name!r} already exists — edit it instead.", "error")
        else:
            cfg["people"][name] = email
            if save():
                flash(f"Added {name} → {email}.")
        return redirect(url_for("index"))

    @app.post("/people/update")
    def people_update():
        old = normalise(request.form.get("original", ""))
        name = normalise(request.form.get("name", ""))
        email = request.form.get("email", "").strip()
        if not name or not valid_email(email):
            flash("A name and a valid email are required.", "error")
        elif name != old and name in cfg["people"]:
            flash(f"{name!r} already exists.", "error")
        else:
            cfg["people"].pop(old, None)
            cfg["people"][name] = email
            if save():
                flash(f"Updated {name} → {email}.")
        return redirect(url_for("index"))

    @app.post("/people/remove")
    def people_remove():
        name = normalise(request.form.get("original") or request.form.get("name") or "")
        if cfg["people"].pop(name, None) is not None and save():
            flash(f"Removed {name}.")
        return redirect(url_for("index"))

    # ----------------------------------------------------------------- relay
    @app.post("/relay")
    def relay_save():
        form = request.form
        try:
            port = int(form.get("port", ""))
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            flash("Relay port must be a number between 1 and 65535.", "error")
            return redirect(url_for("index"))
        host = form.get("host", "").strip()
        from_address = form.get("from_address", "").strip()
        if not host or not from_address:
            flash("Relay host and the From address are required.", "error")
            return redirect(url_for("index"))
        relay = cfg.setdefault("relay", {})
        relay["host"] = host
        relay["port"] = port
        relay["starttls"] = bool(form.get("starttls"))
        relay["ssl"] = bool(form.get("ssl"))
        relay["username"] = form.get("username", "").strip()
        password = form.get("password", "")
        if password:  # write-only field: blank means "keep the saved one"
            relay["password"] = password
        relay["from_address"] = from_address
        if save():
            flash("Relay settings saved." if password
                  else "Relay settings saved (password unchanged).")
        return redirect(url_for("index"))

    @app.post("/relay/test")
    def relay_test():
        relay = cfg.get("relay", {})
        try:
            host = relay["host"]
            port = int(relay.get("port", 587))
            if relay.get("ssl"):
                with smtplib.SMTP_SSL(host, port, timeout=15) as smtp:
                    smtp.ehlo()
                    smtp.login(relay.get("username", ""), relay.get("password", ""))
                    note = "connected over SSL, logged in"
            else:
                with smtplib.SMTP(host, port, timeout=15) as smtp:
                    smtp.ehlo()
                    if relay.get("starttls", True):
                        smtp.starttls()
                        smtp.ehlo()
                        smtp.login(relay.get("username", ""), relay.get("password", ""))
                        note = "connected, TLS up, logged in"
                    else:
                        note = "connected (STARTTLS off, no login attempted)"
            flash(f"Relay test OK — {note} ({host}:{port}).")
        except KeyError:
            flash("No relay host configured.", "error")
        except Exception as exc:
            flash(f"Relay test failed: {exc}", "error")
        return redirect(url_for("index"))

    # ------------------------------------------------------------------ imap
    @app.post("/imap")
    def imap_save():
        form = request.form
        try:
            port = int(form.get("port", ""))
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            flash("IMAP port must be a number between 1 and 65535.", "error")
            return redirect(url_for("index"))
        try:
            poll = int(form.get("poll_seconds", "30"))
            if poll < 5:
                raise ValueError
        except ValueError:
            flash("IMAP poll interval must be at least 5 seconds.", "error")
            return redirect(url_for("index"))
        host = form.get("host", "").strip()
        if not host:
            flash("IMAP host is required.", "error")
            return redirect(url_for("index"))
        imap = cfg.setdefault("imap", {})
        imap["enabled"] = bool(form.get("enabled"))
        imap["host"] = host
        imap["port"] = port
        imap["ssl"] = bool(form.get("ssl"))
        imap["mailbox"] = form.get("mailbox", "").strip() or "INBOX"
        imap["poll_seconds"] = poll
        imap["mark_seen"] = bool(form.get("mark_seen"))
        if save():
            flash("IMAP settings saved.")
        return redirect(url_for("index"))

    @app.post("/imap/test")
    def imap_test():
        imap = cfg.get("imap", {})
        relay = cfg.get("relay", {})
        username = imap.get("username") or relay.get("username", "")
        password = imap.get("password") or relay.get("password", "")
        try:
            host = imap["host"]
            port = int(imap.get("port", 993))
            mailbox = imap.get("mailbox", "INBOX")
            if imap.get("ssl", True):
                conn = imaplib.IMAP4_SSL(host, port, timeout=15)
            else:
                conn = imaplib.IMAP4(host, port, timeout=15)
            try:
                conn.login(username, password)
                typ, _ = conn.select(mailbox, readonly=True)
                if typ != "OK":
                    raise RuntimeError(f"SELECT {mailbox} failed")
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass
            flash(f"IMAP test OK — logged in and opened {mailbox} ({host}:{port}).")
        except KeyError:
            flash("No IMAP host configured.", "error")
        except Exception as exc:
            flash(f"IMAP test failed: {exc}", "error")
        return redirect(url_for("index"))

    # ------------------------------------------------------- general settings
    @app.post("/settings")
    def settings_save():
        not_found = request.form.get("not_found_recipient", "").strip()
        template = request.form.get("subject_template", "").strip()
        if not_found and not valid_email(not_found):
            flash("The fallback address doesn't look like an email.", "error")
        elif "{original_subject}" not in template:
            flash("Subject template must contain {original_subject}.", "error")
        else:
            cfg["not_found_recipient"] = not_found
            cfg["subject_template"] = template
            if save():
                flash("General settings saved.")
        return redirect(url_for("index"))

    # ---------------------------------------------------- dashboard password
    @app.post("/dashboard/password")
    def dashboard_password():
        new = request.form.get("password", "")
        if len(new) < 8:
            flash("New dashboard password must be at least 8 characters.", "error")
        else:
            cfg.setdefault("dashboard", {})["password"] = new
            if save():
                flash("Dashboard password updated.")
        return redirect(url_for("index"))

    return app


def _ensure_password(cfg: dict, save_config) -> None:
    """First run: generate a dashboard password and persist it in .env."""
    dash = cfg.setdefault("dashboard", {})
    if dash.get("password"):
        return
    password = secrets.token_urlsafe(12)
    dash["password"] = password
    try:
        save_config()
        log.info("Generated dashboard password and saved it to .env")
    except OSError:
        log.warning("Generated dashboard password but could not save it to .env")
