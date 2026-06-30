"""
Google OAuth merchant login (manual Authorization Code flow via httpx).

  GET /auth/google/login     → redirect to Google's consent screen
  GET /auth/google/callback  → exchange code, upsert Owner + their store, set session
  GET /auth/logout           → clear session
  GET /api/me                → current owner + store (drives the dashboard)

Open self-serve: first login creates an Owner and a fresh store (one per owner).
Credentials live in settings (GOOGLE_OAUTH_CLIENT_ID/SECRET); if unset, login
returns a friendly "not configured" page instead of erroring.
"""
import secrets
from urllib.parse import urlencode
from datetime import datetime

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse

from app.config import get_settings
from app.database import SessionLocal, Owner, Restaurant
from app.services import tenant as tenant_svc

router   = APIRouter()
settings = get_settings()

GOOGLE_AUTH     = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN    = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO = "https://openidconnect.googleapis.com/v1/userinfo"


def _redirect_uri() -> str:
    return settings.public_base_url.rstrip('/') + "/auth/google/callback"


def _configured() -> bool:
    return bool(settings.google_oauth_client_id and settings.google_oauth_client_secret)


@router.get("/auth/google/login")
def google_login(request: Request):
    if not _configured():
        return HTMLResponse(
            "<h3>Google login isn't configured yet.</h3>"
            "<p>Set <code>GOOGLE_OAUTH_CLIENT_ID</code> / <code>GOOGLE_OAUTH_CLIENT_SECRET</code> in .env.</p>",
            status_code=503)
    state = secrets.token_urlsafe(24)
    request.session['oauth_state'] = state
    params = {
        "client_id":     settings.google_oauth_client_id,
        "redirect_uri":  _redirect_uri(),
        "response_type": "code",
        "scope":         "openid email profile",
        "state":         state,
        "access_type":   "online",
        "prompt":        "select_account",
    }
    return RedirectResponse(f"{GOOGLE_AUTH}?{urlencode(params)}")


@router.get("/auth/google/callback")
async def google_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error:
        return HTMLResponse(f"<h3>Login cancelled.</h3><p>{error}</p>", status_code=400)
    if not code or not state or state != request.session.get('oauth_state'):
        return HTMLResponse("<h3>Login failed (state mismatch). Please try again.</h3>", status_code=400)
    request.session.pop('oauth_state', None)

    async with httpx.AsyncClient(timeout=20) as client:
        tok = await client.post(GOOGLE_TOKEN, data={
            "code":          code,
            "client_id":     settings.google_oauth_client_id,
            "client_secret": settings.google_oauth_client_secret,
            "redirect_uri":  _redirect_uri(),
            "grant_type":    "authorization_code",
        })
        if tok.status_code != 200:
            return HTMLResponse(f"<h3>Token exchange failed.</h3><pre>{tok.text[:300]}</pre>", status_code=400)
        access = tok.json().get("access_token")
        ui = await client.get(GOOGLE_USERINFO, headers={"Authorization": f"Bearer {access}"})
        profile = ui.json()

    owner_id, rid, slug = _upsert_owner_and_store(profile)
    request.session['owner_id']      = owner_id
    request.session['restaurant_id'] = rid
    request.session['email']         = profile.get("email")
    return RedirectResponse(settings.base_path + "/dashboard")


def _upsert_owner_and_store(profile: dict):
    sub   = profile.get("sub")
    email = profile.get("email")
    db = SessionLocal()
    try:
        owner = None
        if sub:
            owner = db.query(Owner).filter(Owner.google_sub == sub).first()
        if not owner and email:
            owner = db.query(Owner).filter(Owner.email == email).first()
        if not owner:
            owner = Owner(google_sub=sub, email=email, name=profile.get("name"),
                          picture=profile.get("picture"), last_login=datetime.utcnow())
            db.add(owner)
            db.commit()
            db.refresh(owner)
        else:
            owner.last_login = datetime.utcnow()
            owner.google_sub = owner.google_sub or sub
            owner.picture    = profile.get("picture") or owner.picture
            db.commit()

        rest = db.query(Restaurant).filter(Restaurant.owner_id == owner.id).first()
        if not rest:
            base = (profile.get("name") or (email or "store").split("@")[0])
            slug = tenant_svc.unique_slug(db, base)
            rest = Restaurant(name=(profile.get("name") or "My Store") + "'s Store",
                              owner_id=owner.id, slug=slug)
            db.add(rest)
            db.commit()
            db.refresh(rest)
        return owner.id, rest.id, rest.slug
    finally:
        db.close()


@router.get("/api/slug-check")
def slug_check(slug: str = ""):
    """Is this slug available? Returns a free suggestion if taken."""
    s = tenant_svc.slugify(slug)
    db = SessionLocal()
    try:
        taken = db.query(Restaurant).filter(Restaurant.slug == s).first() is not None
        return {"slug": s, "available": (not taken),
                "suggestion": tenant_svc.unique_slug(db, s) if taken else s}
    finally:
        db.close()


@router.post("/api/signup")
async def signup(request: Request):
    """Create a store (name + secured slug) without Google. Email is optional but
    links the Owner so a later Google sign-in with the same email connects."""
    b = await request.json()
    name  = (b.get("name") or "").strip()
    email = (b.get("email") or "").strip() or None
    slug  = tenant_svc.slugify(b.get("slug") or name)
    if not name:
        return {"ok": False, "error": "Store name is required."}
    if not slug:
        return {"ok": False, "error": "Please choose a slug."}
    db = SessionLocal()
    try:
        if db.query(Restaurant).filter(Restaurant.slug == slug).first():
            return {"ok": False, "error": f"The slug “{slug}” is taken — pick another."}
        owner = db.query(Owner).filter(Owner.email == email).first() if email else None
        if not owner:
            owner = Owner(email=email, name=name, last_login=datetime.utcnow())
            db.add(owner)
            db.commit()
            db.refresh(owner)
        rest = Restaurant(name=name, slug=slug, owner_id=owner.id)
        db.add(rest)
        db.commit()
        db.refresh(rest)
        request.session['owner_id']      = owner.id
        request.session['restaurant_id'] = rest.id
        if email:
            request.session['email'] = email
        return {"ok": True, "slug": slug, "redirect": settings.base_path + "/agentstudio"}
    except Exception as e:
        db.rollback()
        return {"ok": False, "error": "Could not create the store — the slug may have just been taken."}
    finally:
        db.close()


@router.post("/auth/google/onetap")
async def google_onetap(request: Request):
    """Google Identity Services sign-in (secretless). The browser button posts a
    Google ID token (JWT); we verify it via Google's tokeninfo endpoint, check the
    audience matches our client id, then upsert the owner + store and log in."""
    body  = await request.json()
    token = (body.get("credential") or "").strip()
    if not token:
        return JSONResponse({"ok": False, "error": "missing credential"}, status_code=400)
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get("https://oauth2.googleapis.com/tokeninfo", params={"id_token": token})
    if r.status_code != 200:
        return JSONResponse({"ok": False, "error": "invalid token"}, status_code=401)
    claims = r.json()
    expected_aud = settings.google_oauth_client_id
    if expected_aud and claims.get("aud") != expected_aud:
        return JSONResponse({"ok": False, "error": "audience mismatch"}, status_code=401)
    if claims.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
        return JSONResponse({"ok": False, "error": "bad issuer"}, status_code=401)

    profile = {"sub": claims.get("sub"), "email": claims.get("email"),
               "name": claims.get("name"), "picture": claims.get("picture")}
    owner_id, rid, slug = _upsert_owner_and_store(profile)
    request.session['owner_id']      = owner_id
    request.session['restaurant_id'] = rid
    request.session['email']         = profile.get("email")
    return {"ok": True, "redirect": settings.base_path + "/agentstudio", "slug": slug}


@router.get("/auth/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(settings.base_path + "/dashboard")


@router.get("/api/me")
def me(request: Request):
    oid = request.session.get('owner_id')
    if not oid:
        return {"authenticated": False}
    db = SessionLocal()
    try:
        owner = db.query(Owner).filter(Owner.id == oid).first()
        rest = db.query(Restaurant).filter(Restaurant.id == request.session.get('restaurant_id')).first()
        return {
            "authenticated": True,
            "owner": {"email": owner.email, "name": owner.name, "picture": owner.picture} if owner else None,
            "store": {"id": rest.id, "name": rest.name, "slug": rest.slug,
                      "onboarded": bool(rest.onboarded), "number": rest.plivo_number} if rest else None,
        }
    finally:
        db.close()
