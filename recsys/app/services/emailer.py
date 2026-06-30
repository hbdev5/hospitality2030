"""
Email sender — prefers the Gmail API (OAuth, send-only) so no password is stored.

Priority: Gmail API (gmail.send) → SMTP → simulate.

Gmail path: a one-time consent (auth/gmail/connect) mints a send-only refresh
token saved to <APP_HOME>/.gmail_token (NOT in .env). At send time we exchange it
for a short-lived access token and POST the message to the Gmail API. The token
is send-only and revocable from your Google account — a leaked .env cannot read
your inbox.
"""
import os, base64, smtplib, ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr

import httpx

from app.config import get_settings
from app.paths import home

settings = get_settings()


# ── Gmail API (preferred) ────────────────────────────────────────────────────

def gmail_token_path() -> str:
    return home(".gmail_token")


def _gmail_refresh_token():
    p = gmail_token_path()
    try:
        if os.path.exists(p):
            with open(p) as f:
                return f.read().strip() or None
    except Exception:
        pass
    return None


def gmail_configured() -> bool:
    s = get_settings()
    return bool(s.gmail_client_id and s.gmail_client_secret and _gmail_refresh_token())


def save_gmail_refresh_token(token: str):
    p = gmail_token_path()
    with open(p, "w") as f:
        f.write(token.strip())
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass


def _gmail_access_token():
    s = get_settings()
    rt = _gmail_refresh_token()
    if not (s.gmail_client_id and s.gmail_client_secret and rt):
        return None
    try:
        r = httpx.post("https://oauth2.googleapis.com/token", timeout=15, data={
            "client_id": s.gmail_client_id, "client_secret": s.gmail_client_secret,
            "refresh_token": rt, "grant_type": "refresh_token"})
        if r.status_code == 200:
            return r.json().get("access_token")
        print(f"[gmail] token error: {r.status_code} {r.text[:160]}")
    except Exception as e:
        print(f"[gmail] token exc: {e}")
    return None


def _raw(to_addr, subject, html, from_name, sender):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = formataddr((from_name or "HostBuddy", sender))
    msg["To"]      = to_addr
    msg.attach(MIMEText(html, "html"))
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def _send_via_gmail(recipients, subject, html, from_name):
    s = get_settings()
    token = _gmail_access_token()
    if not token:
        return {"sent": 0, "failed": len(recipients), "info": "gmail auth failed"}
    sender = s.gmail_sender or s.admin_email
    sent = failed = 0
    with httpx.Client(timeout=20) as c:
        for addr in recipients:
            try:
                r = c.post("https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                           headers={"Authorization": f"Bearer {token}"},
                           json={"raw": _raw(addr, subject, html, from_name, sender)})
                if r.status_code == 200:
                    sent += 1
                else:
                    failed += 1
                    print(f"[gmail] send fail {addr}: {r.status_code} {r.text[:160]}")
            except Exception as e:
                failed += 1
                print(f"[gmail] send exc {addr}: {e}")
    return {"sent": sent, "failed": failed, "info": "ok" if sent else "gmail send failed"}


# ── SMTP (fallback) ──────────────────────────────────────────────────────────

def _smtp_configured() -> bool:
    s = get_settings()
    return bool(s.smtp_host and s.smtp_user and s.smtp_pass)


def _send_via_smtp(recipients, subject, html, from_name):
    s = get_settings()
    sender = s.smtp_from or s.smtp_user
    from_hdr = formataddr((from_name or "HostBuddy", sender))
    sent = failed = 0
    try:
        if int(s.smtp_port) == 465:
            srv = smtplib.SMTP_SSL(s.smtp_host, 465, timeout=20)
        else:
            srv = smtplib.SMTP(s.smtp_host, int(s.smtp_port), timeout=20)
            srv.starttls(context=ssl.create_default_context())
        srv.login(s.smtp_user, s.smtp_pass)
        for addr in recipients:
            try:
                msg = MIMEMultipart("alternative")
                msg["Subject"] = subject; msg["From"] = from_hdr; msg["To"] = addr
                msg.attach(MIMEText(html, "html"))
                srv.sendmail(sender, [addr], msg.as_string())
                sent += 1
            except Exception as e:
                failed += 1
                print(f"[email] smtp fail {addr}: {e}")
        srv.quit()
    except Exception as e:
        return {"sent": sent, "failed": len(recipients) - sent, "info": f"smtp error: {e}"}
    return {"sent": sent, "failed": failed, "info": "ok"}


# ── public ───────────────────────────────────────────────────────────────────

def configured() -> bool:
    return gmail_configured() or _smtp_configured()


def send_html(recipients: list, subject: str, html: str, from_name: str = "") -> dict:
    """Send one HTML email to each recipient. Gmail API → SMTP → simulate."""
    recipients = [r for r in (recipients or []) if r]
    if not recipients:
        return {"sent": 0, "failed": 0, "info": "no recipients"}
    if gmail_configured():
        return _send_via_gmail(recipients, subject, html, from_name)
    if _smtp_configured():
        return _send_via_smtp(recipients, subject, html, from_name)
    return {"sent": 0, "failed": len(recipients), "info": "email not configured"}
