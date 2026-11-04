"""
End-to-end test for the Secret Santa router and its web dashboard.

Pattern (per AGENTS.md): a throwaway aiosmtpd "relay catcher" on a second
localhost port, a temp config.json pointed at it with starttls=false (that
branch skips AUTH, so no fake creds needed), the real app started as a
subprocess, mail sent with smtplib, and the captured EmailMessage inspected.
The dashboard is exercised over HTTP with urllib (login, CSRF, whitelist,
people, relay settings, persistence, live reload).

Run:  .venv/bin/python e2e_test.py
"""

import http.cookiejar
import json
import os
import re
import smtplib
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from email import policy
from email.parser import BytesParser

from aiosmtpd.controller import Controller

HERE = os.path.dirname(os.path.abspath(__file__))
SMTP_PORT = 1025
DASH_PORT = 8081
RELAY_PORT = 2026
IMAP_PORT = 2027
BASE_URL = f"http://127.0.0.1:{DASH_PORT}"

failures: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        failures.append(name)
        if extra:
            print(f"       {extra}")


class RelayCatcher:
    """Fake relay: captures every message it is handed."""

    def __init__(self) -> None:
        self.messages = []

    async def handle_DATA(self, server, session, envelope):
        msg = BytesParser(policy=policy.default).parsebytes(envelope.content)
        self.messages.append(msg)
        return "250 OK"


class FakeIMAPServer(socketserver.ThreadingTCPServer):
    """Tiny IMAP4 server: enough for LOGIN/SELECT/UID SEARCH/FETCH/STORE."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, addr) -> None:
        super().__init__(addr, FakeIMAPHandler)
        self.inbox: dict[int, bytes] = {}
        self.seen: set[int] = set()
        self.lock = threading.Lock()
        self._next_uid = 1

    def add_message(self, raw: bytes) -> int:
        with self.lock:
            uid = self._next_uid
            self._next_uid += 1
            self.inbox[uid] = raw
            return uid


class FakeIMAPHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        self.wfile.write(b"* OK fake IMAP ready\r\n")
        while True:
            line = self.rfile.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            parts = text.split(" ", 2)
            tag = parts[0]
            cmd = parts[1].upper() if len(parts) > 1 else ""
            if cmd == "CAPABILITY":
                self.wfile.write(b"* CAPABILITY IMAP4rev1\r\n")
                self._ok(tag, "CAPABILITY")
            elif cmd == "LOGIN":
                self._ok(tag, "LOGIN")
            elif cmd == "SELECT":
                self.wfile.write(b"* 1 EXISTS\r\n")
                self._ok(tag, "SELECT")
            elif cmd == "UID":
                self._uid(tag, parts[2] if len(parts) > 2 else "")
            elif cmd == "LOGOUT":
                self.wfile.write(b"* BYE\r\n")
                self._ok(tag, "LOGOUT")
                break
            else:
                self._ok(tag, cmd or "NOOP")

    def _ok(self, tag: str, what: str) -> None:
        self.wfile.write(f"{tag} OK {what} completed\r\n".encode())

    def _uid(self, tag: str, rest: str) -> None:
        sub = rest.split(" ", 1)
        command = sub[0].upper()
        args = sub[1] if len(sub) > 1 else ""
        server: FakeIMAPServer = self.server  # type: ignore[assignment]
        if command == "SEARCH":
            with server.lock:
                uids = [str(u) for u in server.inbox if u not in server.seen]
            self.wfile.write(f"* SEARCH {' '.join(uids)}\r\n".encode())
            self._ok(tag, "SEARCH")
        elif command == "FETCH":
            uid = int(args.split()[0])
            with server.lock:
                raw = server.inbox.get(uid)
            if raw is None:
                self._ok(tag, "FETCH")
                return
            self.wfile.write(f"* {uid} FETCH (RFC822 {{{len(raw)}}}\r\n".encode())
            self.wfile.write(raw)
            self.wfile.write(b")\r\n")
            self._ok(tag, "FETCH")
        elif command == "STORE":
            uid = int(args.split()[0])
            with server.lock:
                server.seen.add(uid)
            self._ok(tag, "STORE")
        else:
            self._ok(tag, command)


def wait_for_port(port: int, timeout: float = 20) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def wait_for(pred, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.1)
    return False


def build_opener(*handlers) -> urllib.request.OpenerDirector:
    # ProxyHandler({}): never route these localhost requests through a proxy,
    # even when http_proxy/HTTPS_PROXY env vars are set.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), *handlers)


class Browser:
    """Minimal HTTP client with cookies + CSRF handling for the dashboard."""

    def __init__(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def get(self, path: str) -> str:
        with self.opener.open(BASE_URL + path, timeout=10) as resp:
            return resp.read().decode()

    def csrf(self) -> str:
        m = re.search(r'name="csrf" value="([^"]+)"', self.get("/"))
        return m.group(1) if m else ""

    def post(self, path: str, fields: dict, with_csrf: bool = True):
        data = dict(fields)
        if with_csrf:
            data["csrf"] = self.csrf()
        req = urllib.request.Request(
            BASE_URL + path, data=urllib.parse.urlencode(data).encode())
        try:
            resp = self.opener.open(req, timeout=10)
        except urllib.error.HTTPError as err:
            resp = err
        with resp:
            return resp.getcode(), resp.read().decode()


def stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        out, _ = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
    print("\n--- app log ---")
    print(out.decode(errors="replace"))


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="santa-e2e-")
    cfg_path = os.path.join(tmp, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump({
            "listen_host": "127.0.0.1",
            "listen_port": SMTP_PORT,
            "relay": {
                "host": "127.0.0.1",
                "port": RELAY_PORT,
                "starttls": False,
                "username": "bot@example.com",
                "password": "original-app-password",
                "from_address": "Secret Santa <santa@example.com>",
            },
            "imap": {
                "enabled": True,
                "host": "127.0.0.1",
                "port": IMAP_PORT,
                "ssl": False,
                "username": "bot@example.com",
                "password": "original-app-password",
                "mailbox": "INBOX",
                "poll_seconds": 1,
                "mark_seen": True,
            },
            "dashboard": {"host": "127.0.0.1", "port": DASH_PORT,
                          "password": "test-dashboard-pw"},
            "whitelist": ["alice@example.com"],
            "people": {"bob": "bob.santa@example.com"},
            "not_found_recipient": "organizer@example.com",
            "subject_template": "[Secret Santa] {original_subject}",
        }, f, indent=2)

    catcher = RelayCatcher()
    relay_ctl = Controller(catcher, hostname="127.0.0.1", port=RELAY_PORT)
    relay_ctl.start()

    imap_server = FakeIMAPServer(("127.0.0.1", IMAP_PORT))
    threading.Thread(target=imap_server.serve_forever, daemon=True).start()

    env = dict(os.environ, CONFIG_PATH=cfg_path, PYTHONUNBUFFERED="1")
    proc = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "app", "main.py")],
        cwd=HERE, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def on_disk() -> dict:
        with open(cfg_path, encoding="utf-8") as f:
            return json.load(f)

    try:
        ok = wait_for_port(SMTP_PORT) and wait_for_port(DASH_PORT)
        check("app started (SMTP + dashboard up)", ok)
        if ok:
            run_checks(catcher, on_disk, imap_server)
    finally:
        stop(proc)
        imap_server.shutdown()
        imap_server.server_close()

    if failures:
        print(f"\n{len(failures)} FAILURE(S): " + "; ".join(failures))
        return 1
    print("\nALL TESTS PASSED")
    return 0


def run_checks(catcher: RelayCatcher, on_disk, imap_server: FakeIMAPServer) -> None:
    anon = build_opener()
    with anon.open(BASE_URL + "/", timeout=10) as resp:
        check("unauthenticated / redirects to login",
              resp.geturl().endswith("/login"), resp.geturl())

    c = Browser()

    status, html = c.post("/login", {"password": "nope"})
    check("wrong password rejected", "Wrong password." in html)

    status, html = c.post("/login", {"password": "test-dashboard-pw"})
    check("login lands on dashboard",
          status == 200 and "alice@example.com" in html, f"status {status}")

    status, html = c.post("/whitelist/add", {"email": "Frank@Example.com"},
                          with_csrf=False)
    check("POST without CSRF token is rejected", status == 400, str(status))

    status, html = c.post("/whitelist/add", {"email": "Frank@Example.com"})
    check("whitelist add appears in UI", "frank@example.com" in html)
    check("whitelist add persisted (lowercased)",
          "frank@example.com" in on_disk()["whitelist"])

    status, html = c.post("/whitelist/add", {"email": "alice@example.com"})
    check("duplicate whitelist add refused", "already whitelisted" in html)

    status, html = c.post("/people/add",
                          {"name": "Frank", "email": "frank.santa@example.com"})
    check("people add appears in UI", "frank.santa@example.com" in html)
    check("people add persisted (name lowercased)",
          on_disk()["people"].get("frank") == "frank.santa@example.com")

    # Live reload: the new sender + person must work without a restart.
    with smtplib.SMTP("127.0.0.1", SMTP_PORT, timeout=10) as smtp:
        smtp.sendmail(
            "frank@example.com", ["santa@example.com"],
            "From: frank@example.com\r\nTo: santa@example.com\r\n"
            "Subject: Question for Frank\r\n\r\nWhat size shirt do you wear?")
    wait_for(lambda: len(catcher.messages) >= 1)
    check("newly added sender+person routed without restart",
          len(catcher.messages) == 1, f"captured {len(catcher.messages)}")
    if catcher.messages:
        fwd = catcher.messages[0]
        check("forwarded to the new person's address",
              fwd["To"] == "frank.santa@example.com", str(fwd["To"]))
        check("From is the anonymous bot address",
              "santa@example.com" in (fwd["From"] or ""))
        check("matched name stripped from subject",
              "frank" not in (fwd["Subject"] or "").lower(), str(fwd["Subject"]))
        check("subject template applied",
              (fwd["Subject"] or "").startswith("[Secret Santa]"))
        check("body text forwarded", "What size shirt" in fwd.get_content())
        check("original sender address not leaked anywhere",
              "frank@example.com" not in fwd.as_string())

    # Fallback routing still works.
    with smtplib.SMTP("127.0.0.1", SMTP_PORT, timeout=10) as smtp:
        smtp.sendmail("alice@example.com", ["santa@example.com"],
                      "Subject: hello\r\n\r\nfallback test")
    wait_for(lambda: len(catcher.messages) >= 2)
    if len(catcher.messages) >= 2:
        check("no-match subject goes to fallback recipient",
              catcher.messages[1]["To"] == "organizer@example.com",
              str(catcher.messages[1]["To"]))
    else:
        check("no-match subject goes to fallback recipient", False,
              "fallback mail never arrived")

    # Non-whitelisted sender rejected.
    rejected = False
    try:
        with smtplib.SMTP("127.0.0.1", SMTP_PORT, timeout=10) as smtp:
            smtp.sendmail("mallory@example.com", ["santa@example.com"],
                          "Subject: hi\r\n\r\nsneaky")
    except smtplib.SMTPDataError as err:
        rejected = err.smtp_code == 550
    check("non-whitelisted sender rejected with 550", rejected)

    # IMAP receive path: inject a message into the fake mailbox and let the
    # app's poller route it through the same whitelist/subject logic.
    c.post("/whitelist/add", {"email": "grace@example.com"})
    c.post("/people/add",
           {"name": "Grace", "email": "grace.santa@example.com"})
    raw = (
        b"From: grace@example.com\r\n"
        b"To: santa@brenden.zip\r\n"
        b"Subject: Question for Grace\r\n"
        b"\r\n"
        b"What colour do you like?\r\n"
    )
    uid = imap_server.add_message(raw)
    before = len(catcher.messages)
    ok = wait_for(lambda: len(catcher.messages) > before, timeout=15)
    check("IMAP message routed to the relay", ok,
          f"captured {len(catcher.messages)}")
    if ok:
        fwd = catcher.messages[-1]
        check("IMAP forward goes to the matched person",
              fwd["To"] == "grace.santa@example.com", str(fwd["To"]))
        check("IMAP forward is anonymous",
              "grace@example.com" not in fwd.as_string())
        check("IMAP message marked seen", uid in imap_server.seen)

    status, html = c.post("/relay/test", {})
    check("relay test reports OK", "Relay test OK" in html, html[:200])

    status, html = c.post("/relay", {
        "host": "127.0.0.1", "port": str(RELAY_PORT), "starttls": "on",
        "username": "bot@example.com", "password": "",
        "from_address": "Secret Santa <santa@example.com>"})
    relay_disk = on_disk()["relay"]
    check("blank password keeps the saved app password",
          relay_disk["password"] == "original-app-password",
          str(relay_disk["password"]))
    check("starttls toggle saved", relay_disk["starttls"] is True)

    c.post("/relay", {"host": "127.0.0.1", "port": str(RELAY_PORT),
                      "starttls": "on", "username": "bot@example.com",
                      "password": "brand-new-app-password",
                      "from_address": "Secret Santa <santa@example.com>"})
    check("new app password saved",
          on_disk()["relay"]["password"] == "brand-new-app-password")

    status, html = c.post("/people/update",
                          {"original": "frank", "name": "Franklin",
                           "email": "franklin.santa@example.com"})
    people_disk = on_disk()["people"]
    check("people rename applied",
          people_disk.get("franklin") == "franklin.santa@example.com"
          and "frank" not in people_disk, str(people_disk))

    c.post("/settings", {"not_found_recipient": "chief@example.com",
                         "subject_template": "[SS] {original_subject}"})
    disk = on_disk()
    check("general settings persisted",
          disk["not_found_recipient"] == "chief@example.com"
          and disk["subject_template"] == "[SS] {original_subject}")

    c.post("/dashboard/password", {"password": "brand-new-dashboard-pw"})
    c2 = Browser()
    status, html = c2.post("/login", {"password": "test-dashboard-pw"})
    check("old dashboard password rejected after change",
          "Wrong password." in html)
    status, html = c2.post("/login", {"password": "brand-new-dashboard-pw"})
    check("new dashboard password works", "alice@example.com" in html)

    html = c.get("/")
    check("activity log shows routed mail",
          "frank@example.com → frank (frank.santa@example.com)" in html)

    c.post("/whitelist/remove", {"email": "frank@example.com"})
    check("whitelist remove persisted",
          "frank@example.com" not in on_disk()["whitelist"])
    c.post("/people/remove", {"original": "franklin", "name": "franklin"})
    check("people remove persisted", "franklin" not in on_disk()["people"])


if __name__ == "__main__":
    sys.exit(main())
