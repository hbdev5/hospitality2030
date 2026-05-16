from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
import os, sys
sys.path.insert(0, os.path.expanduser('~/work/recsys'))

from app.database import init_db
from app.routers import plivo_hooks, menu, metrics
from app.routers import voice_ws
from app.config import get_settings

settings = get_settings()
BASE = settings.base_path  # /recsys

app = FastAPI(root_path=BASE)

@app.on_event("startup")
def startup():
    init_db()
    print("[RecsYS] DB tables ready")

# Static + templates
static_dir    = os.path.expanduser("~/work/recsys/static")
templates_dir = os.path.expanduser("~/work/recsys/templates")
app.mount("/static", StaticFiles(directory=static_dir), name="static")
templates = Jinja2Templates(directory=templates_dir)

# Routers
app.include_router(plivo_hooks.router)
app.include_router(menu.router)
app.include_router(metrics.router)
app.include_router(voice_ws.router)   # WebSocket voice stream

# UI pages
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse(request=request, name="dashboard.html", context={"base": BASE})

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
