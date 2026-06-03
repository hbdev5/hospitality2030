"""
Minimal SMTP emailer (stdlib only) — provider-agnostic.

Configure via .env: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM.
Works with any free SMTP: Gmail (app password), Brevo, SendGrid SMTP, etc.
If not configured, callers fall back to simulate.
"""

import smtplib, ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr
from app.config import get_settings

settings = get_settings()


def configured() -> bool:
    return bool(settings.smtp_host and settings.smtp_user and settings.smtp_pass)


def send_html(recipients: list, subject: str, html: str, from_name: str = "") -> dict:
    """Send one HTML email to each recipient over a single connection.
    Returns {sent, failed, info}."""
    if not configured():
        return {"sent": 0, "failed": len(recipients), "info": "smtp not configured"}
    recipients = [r for r in (recipients or []) if r]
    if not recipients:
        return {"sent": 0, "failed": 0, "info": "no recipients"}

    sender = settings.smtp_from or settings.smtp_user
    from_hdr = formataddr((from_name or "Curry Bliss VIP", sender))
    sent = failed = 0
    try:
        if int(settings.smtp_port) == 465:
            srv = smtplib.SMTP_SSL(settings.smtp_host, 465, timeout=20)
        else:
            srv = smtplib.SMTP(settings.smtp_host, int(settings.smtp_port), timeout=20)
            srv.starttls(context=ssl.create_default_context())
        srv.login(settings.smtp_user, settings.smtp_pass)
        for addr in recipients:
            try:
                msg = MIMEMultipart("alternative")
                msg["Subject"] = subject
                msg["From"]    = from_hdr
                msg["To"]      = addr
                msg.attach(MIMEText(html, "html"))
                srv.sendmail(sender, [addr], msg.as_string())
                sent += 1
            except Exception as e:
                failed += 1
                print(f"[email] send fail {addr}: {e}")
        srv.quit()
    except Exception as e:
        return {"sent": sent, "failed": len(recipients) - sent, "info": f"smtp error: {e}"}
    return {"sent": sent, "failed": failed, "info": "ok"}
