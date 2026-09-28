"""James mail access for the Lampy web console (stdlib only).

IMAP  : 127.0.0.1:1143 (plain, inside the lampy WSL distro)
SMTP  : 127.0.0.1:2587 (submission; STARTTLS when advertised)

Credentials come from the Mail tab form and live only in the console
process's in-memory vault (console.py), keyed by a random token — never
in the Flask session cookie and never on disk. Any failure is reported
honestly with the server's error — never fake mailbox data. If James has
no provisioned user yet, the IMAP login fails and the UI shows the setup
hint instead.
"""

import email
import email.policy
import imaplib
import os
import smtplib
from email.message import EmailMessage
from email.utils import parsedate_to_datetime

MAIL_HOST = os.environ.get("CONSOLE_MAIL_HOST", "127.0.0.1")
IMAP_PORT = int(os.environ.get("CONSOLE_IMAP_PORT", "1143"))
SMTP_PORT = int(os.environ.get("CONSOLE_SMTP_PORT", "2587"))
TIMEOUT = float(os.environ.get("CONSOLE_PROBE_TIMEOUT", "5"))

imaplib.IMAP4._timeout = TIMEOUT  # noqa: SLF001 — single-process console


def _decode(hdr):
    if not hdr:
        return ""
    parts = email.header.decode_header(hdr)
    out = ""
    for text, enc in parts:
        if isinstance(text, bytes):
            out += text.decode(enc or "utf-8", "replace")
        else:
            out += text
    return out


def imap_connect(user, password):
    """Return a logged-in, INBOX-selected IMAP4. Raises with the cause."""
    try:
        m = imaplib.IMAP4(MAIL_HOST, IMAP_PORT)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError("IMAP connect failed: %s: %s"
                           % (type(e).__name__, e))
    try:
        m.login(user, password)
    except Exception as e:  # noqa: BLE001
        try:
            m.logout()
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError("IMAP login failed: %s: %s"
                           % (type(e).__name__, e))
    typ, _ = m.select("INBOX", readonly=True)
    if typ != "OK":
        m.logout()
        raise RuntimeError("IMAP select INBOX failed")
    return m


def list_messages(user, password, limit=25):
    """Newest-first list of {num, from, subject, date}. Raises on failure."""
    m = imap_connect(user, password)
    try:
        typ, data = m.search(None, "ALL")
        if typ != "OK":
            raise RuntimeError("IMAP search failed")
        nums = data[0].split()
        msgs = []
        for num in reversed(nums[-limit:]):
            typ, data = m.fetch(num, "(BODY.PEEK[HEADER.FIELDS "
                                     "(FROM SUBJECT DATE)])")
            if typ != "OK" or not data or not data[0]:
                continue
            msg = email.message_from_bytes(data[0][1],
                                           policy=email.policy.default)
            try:
                dt = parsedate_to_datetime(msg["Date"])
                date = dt.strftime("%Y-%m-%d %H:%M") if dt else msg["Date"]
            except Exception:  # noqa: BLE001
                date = msg["Date"] or ""
            msgs.append({"num": num.decode(),
                         "from": _decode(msg["From"]),
                         "subject": _decode(msg["Subject"]) or "(no subject)",
                         "date": date})
        return msgs
    finally:
        try:
            m.logout()
        except Exception:  # noqa: BLE001
            pass


def fetch_message(user, password, num):
    """Full message {from, to, subject, date, body}. Raises on failure."""
    m = imap_connect(user, password)
    try:
        typ, data = m.fetch(num.encode() if isinstance(num, str) else num,
                            "(BODY.PEEK[])")
        if typ != "OK" or not data or not data[0]:
            raise RuntimeError("message not found")
        msg = email.message_from_bytes(data[0][1],
                                       policy=email.policy.default)
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain" \
                        and not part.get_filename():
                    try:
                        body = part.get_content()
                    except Exception:  # noqa: BLE001
                        body = ""
                    break
        else:
            try:
                body = msg.get_content()
            except Exception:  # noqa: BLE001
                body = ""
        return {"from": _decode(msg["From"]), "to": _decode(msg["To"]),
                "subject": _decode(msg["Subject"]) or "(no subject)",
                "date": msg["Date"] or "", "body": body or ""}
    finally:
        try:
            m.logout()
        except Exception:  # noqa: BLE001
            pass


def send_message(user, password, to_addr, subject, body):
    """Send via James submission. Raises with the server's error."""
    msg = EmailMessage()
    msg["From"] = user if "@" in user else user
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        s = smtplib.SMTP(MAIL_HOST, SMTP_PORT, timeout=TIMEOUT)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError("SMTP connect failed: %s: %s"
                           % (type(e).__name__, e))
    try:
        s.ehlo()
        if s.has_extn("starttls"):
            s.starttls()
            s.ehlo()
        try:
            s.login(user, password)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError("SMTP login failed: %s: %s"
                               % (type(e).__name__, e))
        try:
            s.send_message(msg)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError("SMTP send failed: %s: %s"
                               % (type(e).__name__, e))
    finally:
        try:
            s.quit()
        except Exception:  # noqa: BLE001
            pass
