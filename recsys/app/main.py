from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from starlette.middleware.sessions import SessionMiddleware
import os, sys
_APP_HOME = (os.environ.get("APP_HOME")
             or os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _APP_HOME)

from app.database import init_db
from app.routers import plivo_hooks, menu, metrics
from app.routers import voice_ws
from app.routers import voice_web
from app.routers import checkout
from app.routers import text_chat
from app.routers import vip_web
from app.routers import admin_vip
from app.routers import srm
from app.routers import operator
from app.routers import skills
from app.routers import sms_test
from app.routers import auth
from app.routers import catalog
from app.routers import onboard
from app.routers import gmail_oauth
from app.routers import admin_ops
from app.config import get_settings

settings = get_settings()
BASE = settings.base_path  # /recsys

app = FastAPI(root_path=BASE)

# Signed session cookie (merchant login). same_site=lax so the cookie survives
# Google's OAuth redirect back to /auth/google/callback.
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key,
                   same_site="lax", https_only=False)

@app.on_event("startup")
def startup():
    init_db()
    print("[RecsYS] DB tables ready")

# Static + templates
static_dir    = os.path.join(_APP_HOME, "static")
templates_dir = os.path.join(_APP_HOME, "templates")
app.mount("/static", StaticFiles(directory=static_dir), name="static")
templates = Jinja2Templates(directory=templates_dir)

# Routers
app.include_router(plivo_hooks.router)
app.include_router(menu.router)
app.include_router(metrics.router)
app.include_router(voice_ws.router)   # WebSocket voice stream
app.include_router(voice_web.router)  # Browser voice chat API (no prefix — Apache strips /recsys/)
app.include_router(checkout.router)   # PayPal checkout (page + create/capture endpoints)
app.include_router(text_chat.router)  # Browser Text-to-Order channel (owner testing)
app.include_router(vip_web.router)    # VIP membership signup (PayPal subscription)
app.include_router(admin_vip.router)  # Admin VIP setup + card editor; consumer config
app.include_router(srm.router)        # Self-Running Marketing — weekly VIP campaigns
app.include_router(operator.router)   # Operator console: conversation log + annotation
app.include_router(skills.router)     # Self-learning agent skills (operator-taught intents)
app.include_router(sms_test.router)   # Dev-only SMS test console (/smsTest, key-gated)
app.include_router(auth.router)       # Google OAuth merchant login + tenant session
app.include_router(catalog.router)    # Photo → AI catalog upload (tenant-scoped)
app.include_router(onboard.router)    # Merchant onboarding wizard (store name, phone, raffle)
app.include_router(gmail_oauth.router) # One-time Gmail send authorization (OAuth)
app.include_router(admin_ops.router)   # Admin: authorize → buy requested numbers

# UI pages
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse(request=request, name="dashboard.html", context={"base": BASE})

@app.get("/agentstudio", response_class=HTMLResponse)
async def agentstudio_page(request: Request):
    """AI Studio console — agent launcher (Text / Phone / Kiosk / Loyalty live)."""
    return templates.TemplateResponse(request=request, name="agentstudio.html", context={
        "base": BASE,
        "plivo_number": settings.plivo_number,
    })

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    """Merchant home — sign in with Google, then manage your store."""
    return templates.TemplateResponse(request=request, name="dashboard_merchant.html", context={"base": BASE})

@app.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request):
    """Create a store (name + slug); works without Google OAuth configured."""
    return templates.TemplateResponse(request=request, name="signup.html", context={
        "base": BASE, "google_client_id": settings.google_oauth_client_id})

@app.get("/menu", response_class=HTMLResponse)
async def menu_page(request: Request):
    return templates.TemplateResponse(request=request, name="menu.html", context={"base": BASE})

@app.get("/config", response_class=HTMLResponse)
async def config_page(request: Request):
    return templates.TemplateResponse(request=request, name="config.html", context={
        "base": BASE,
        "plivo_number": settings.plivo_number,
        "voice_webhook": f"https://support.hostbuddy.io{BASE}/api/voice/inbound",
        "sms_webhook":   f"https://support.hostbuddy.io{BASE}/api/sms/inbound"})

@app.get("/metrics", response_class=HTMLResponse)
async def metrics_page(request: Request):
    return templates.TemplateResponse(request=request, name="metrics.html", context={"base": BASE})

@app.get("/offers", response_class=HTMLResponse)
async def offers_page(request: Request):
    return templates.TemplateResponse(request=request, name="offers.html", context={"base": BASE})

@app.get("/voice-test", response_class=HTMLResponse)
@app.get("/kiosk",      response_class=HTMLResponse)
async def kiosk_page(request: Request):
    """Kiosk Experience — voice ordering UI styled like an in-store kiosk."""
    return templates.TemplateResponse(request=request, name="voice_test.html", context={"base": BASE})

@app.get("/text", response_class=HTMLResponse)
async def text_page(request: Request):
    """Text-to-Order — browser SMS-like chat for restaurant-owner testing."""
    return templates.TemplateResponse(request=request, name="text_chat.html", context={"base": BASE})
