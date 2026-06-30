"""
Merchant onboarding wizard (runs right after Google sign-in, until completed).

  GET  /onboard                      mobile-first setup wizard (owner-gated)
  POST /api/onboard/store-name       set the store name (+ refresh slug)
  GET  /api/onboard/numbers          search available voice numbers by area code
  POST /api/onboard/provision        BUY a number (voice) + assign to this store
  POST /api/onboard/finish           enable default raffle, mark onboarded → console

Every step is scoped to the logged-in owner's own store.
"""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.paths import home
from app.database import SessionLocal, Restaurant, Owner
from app.services import tenant as tenant_svc
from app.services import provisioning as prov
from app.services import skills as skills_svc
from app.services import onboarding_email as ob_email

router    = APIRouter()
settings  = get_settings()
templates = Jinja2Templates(directory=home("templates"))


def _my_store(request):
    """The logged-in owner's own store id, or None."""
    try:
        oid = request.session.get('owner_id')
        rid = request.session.get('restaurant_id')
    except Exception:
        return None
    if not (oid and rid):
        return None
    db = SessionLocal()
    try:
        r = db.query(Restaurant).filter(Restaurant.id == rid).first()
        return rid if (r and r.owner_id == oid) else None
    finally:
        db.close()


def _unauth():
    return JSONResponse({"ok": False, "error": "Please sign in."}, status_code=403)


@router.get("/onboard", response_class=HTMLResponse)
def onboard_page(request: Request):
    rid = _my_store(request)
    if not rid:
        return RedirectResponse(settings.base_path + "/dashboard")
    s = tenant_svc.restaurant_summary(rid) or {}
    return templates.TemplateResponse(request=request, name="onboard.html", context={
        "base": settings.base_path, "store_name": s.get("name", ""), "slug": s.get("slug", "")})


@router.post("/api/onboard/store-name")
async def set_store_name(request: Request):
    rid = _my_store(request)
    if not rid:
        return _unauth()
    b = await request.json()
    name = (b.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "Store name is required."}
    db = SessionLocal()
    try:
        r = db.query(Restaurant).filter(Restaurant.id == rid).first()
        r.name = name[:255]
        desired = tenant_svc.slugify(name)
        if desired != r.slug:
            slug, n = desired, 2
            while db.query(Restaurant).filter(Restaurant.slug == slug, Restaurant.id != rid).first():
                slug, n = f"{desired}-{n}", n + 1
            r.slug = slug
        db.commit()
        return {"ok": True, "name": r.name, "slug": r.slug}
    finally:
        db.close()


@router.get("/api/onboard/numbers")
def numbers(request: Request, area_code: str = ""):
    if not _my_store(request):
        return _unauth()
    return prov.search_numbers(area_code)


@router.post("/api/onboard/request-number")
async def request_number(request: Request):
    """Operator requests a number — we DON'T buy it. Record it and email the
    admin (sagar@hostbuddy.io) to authorize; provisioning happens on approval."""
    rid = _my_store(request)
    if not rid:
        return _unauth()
    b = await request.json()
    number = (b.get("number") or "").strip()
    if not number:
        return {"ok": False, "error": "No number selected."}
    db = SessionLocal()
    try:
        r = db.query(Restaurant).filter(Restaurant.id == rid).first()
        r.requested_number = number
        store_name, slug = r.name, r.slug
        owner = db.query(Owner).filter(Owner.id == r.owner_id).first()
        owner_email = owner.email if owner else (request.session.get('email') or "")
        db.commit()
    finally:
        db.close()
    ob_email.send_number_request(store_name, owner_email, number, number[1:4], slug)
    return {"ok": True, "pending": True, "number": number}


@router.post("/api/onboard/provision")
async def provision(request: Request):
    """Actual purchase — gated to admin authorization (not the operator UI)."""
    rid = _my_store(request)
    if not rid:
        return _unauth()
    b = await request.json()
    return prov.provision_number(rid, b.get("number"))


@router.post("/api/onboard/finish")
async def finish(request: Request):
    rid = _my_store(request)
    if not rid:
        return _unauth()
    # Every store gets the raffle capability by default.
    if not any(s["action"] == "raffle_entry" for s in skills_svc.list_skills(rid)):
        skills_svc.create_skill(rid, "Raffle",
                                ["raffle", "add me to raffle", "enter raffle", "giveaway"],
                                "You've been added to the raffle! Good luck! 🎉", "raffle_entry")
    db = SessionLocal()
    try:
        r = db.query(Restaurant).filter(Restaurant.id == rid).first()
        r.onboarded = True
        db.commit()
        store_name, slug = r.name, r.slug
        owner = db.query(Owner).filter(Owner.id == r.owner_id).first()
        owner_email = owner.email if owner else (request.session.get('email') or "")
    finally:
        db.close()
    # Spam-safe welcome (email, not SMS) with the $1 activation + next steps.
    if owner_email:
        ob_email.send_operator_welcome(owner_email, store_name, slug)
    return {"ok": True, "redirect": settings.base_path + "/agentstudio", "slug": slug}
