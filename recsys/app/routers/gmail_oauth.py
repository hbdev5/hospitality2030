"""
One-time Gmail send authorization (admin-only).

  GET /auth/gmail/connect?key=<admin>   → Google consent (gmail.send scope)
  GET /auth/gmail/callback              → exchange code → save send-only refresh token

The refresh token is written to <APP_HOME>/.gmail_token (not .env), is send-only,
and is revocable from the Google account. Gated by the smsTest/admin key.
"""
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, HTMLResponse

from app.config import get_settings
from app.services import emailer

router   = APIRouter()
settings = get_settings()

SCOPE = "https://www.googleapis.com/auth/gmail.send"


def _redirect_uri():
    return settings.public_base_url.rstrip('/') + "/auth/gmail/callback"


def _admin_ok(key):
    expected = settings.smstest_key
    return (not expected) or (key == expected)


@router.get("/auth/gmail/connect", response_class=HTMLResponse)
def gmail_connect(request: Request, key: str = ""):
    if not _admin_ok(key):
        return HTMLResponse("<h3>Not authorized.</h3>", status_code=403)
    if not (settings.gmail_client_id and settings.gmail_client_secret):
        return HTMLResponse("<h3>Set GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET in .env first.</h3>", status_code=503)
    params = {
        "client_id":     settings.gmail_client_id,
        "redirect_uri":  _redirect_uri(),
        "response_type": "code",
        "scope":         SCOPE,
        "access_type":   "offline",
        "prompt":        "select_account consent",   # always show the account chooser + force refresh token
        "include_granted_scopes": "true",
        "login_hint":    settings.gmail_sender,
    }
    return RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params))


@router.get("/auth/gmail/callback", response_class=HTMLResponse)
async def gmail_callback(request: Request, code: str = "", error: str = ""):
    if error:
        return HTMLResponse(f"<h3>Consent cancelled.</h3><p>{error}</p>", status_code=400)
    if not code:
        return HTMLResponse("<h3>Missing code.</h3>", status_code=400)
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post("https://oauth2.googleapis.com/token", data={
            "code":          code,
            "client_id":     settings.gmail_client_id,
            "client_secret": settings.gmail_client_secret,
            "redirect_uri":  _redirect_uri(),
            "grant_type":    "authorization_code",
        })
    if r.status_code != 200:
        return HTMLResponse(f"<h3>Token exchange failed.</h3><pre>{r.text[:400]}</pre>", status_code=400)
    refresh = r.json().get("refresh_token")
    if not refresh:
        return HTMLResponse("<h3>No refresh token returned.</h3>"
                            "<p>Re-run with prompt=consent (this flow does). If it persists, remove the app's "
                            "access at myaccount.google.com → Security → Third-party access, then retry.</p>",
                            status_code=400)
    emailer.save_gmail_refresh_token(refresh)
    return HTMLResponse(
        "<div style='font-family:sans-serif;max-width:480px;margin:40px auto'>"
        "<h2>✅ Gmail connected</h2><p>A <b>send-only</b> token is saved. "
        f"Email now sends as <b>{settings.gmail_sender}</b> — no password stored. "
        "You can revoke it anytime at myaccount.google.com → Security → Third-party access.</p></div>")
