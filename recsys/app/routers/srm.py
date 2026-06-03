"""
Self-Running Marketing router — owner studio + consumer email preview.

  POST /api/srm/generate   → draft this week's menu-driven campaign
  GET  /api/srm/current    → latest campaign + status + subscriber count
  POST /api/srm/approve    → approve + (simulated) send to VIP subscriber emails
  GET  /srm                → owner review/approve studio page
  GET  /srm/email          → the exact consumer email preview
"""

import os
from typing import Optional
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.services import srm as srm_svc

router    = APIRouter()
settings  = get_settings()
templates = Jinja2Templates(directory=os.path.expanduser("~/work/recsys/templates"))


@router.get("/api/srm/menu-items")
async def srm_menu_items():
    return JSONResponse({"items": srm_svc.menu_items(1)})


@router.post("/api/srm/generate")
async def srm_generate(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    data = srm_svc.generate(1, (body or {}).get("featured_item") or None)
    if data:
        srm_svc.enrich_for_email(data)
    data["subscriber_count"] = srm_svc.subscriber_count(1)
    return JSONResponse(data)


@router.get("/api/srm/current")
async def srm_current():
    data = srm_svc.current(1) or {}
    if data:
        srm_svc.enrich_for_email(data)
    data["subscriber_count"] = srm_svc.subscriber_count(1)
    return JSONResponse(data)


@router.post("/api/srm/update")
async def srm_update(request: Request):
    body = await request.json()
    data = srm_svc.update(1, body.get("featured"), body.get("secondary"), body.get("style")) or {}
    data["subscriber_count"] = srm_svc.subscriber_count(1)
    return JSONResponse(data)


@router.post("/api/srm/approve")
async def srm_approve():
    return JSONResponse(srm_svc.approve(1))


@router.get("/srm", response_class=HTMLResponse)
async def srm_studio(request: Request):
    return templates.TemplateResponse(request=request, name="srm_studio.html",
                                      context={"base": settings.base_path})


@router.get("/srm/email", response_class=HTMLResponse)
async def srm_email(request: Request, style: Optional[int] = None):
    data = srm_svc.current(1) or {}
    if data:
        srm_svc.enrich_for_email(data)
    st = style or data.get("style") or 1
    st = st if st in (1, 2, 3) else 1
    return templates.TemplateResponse(request=request, name=f"srm_email_{st}.html",
                                      context={"base": settings.base_path, "c": data})
