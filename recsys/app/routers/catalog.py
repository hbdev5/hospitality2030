"""
Catalog upload — photo → AI extract → review → save. Tenant-scoped BY URL:
everything lives under /r/{slug}/… and is ownership-checked (only the logged-in
owner of that store can read/edit its catalog).

  GET  /r/{slug}/catalog              merchant catalog page (owner-only)
  POST /r/{slug}/api/catalog/extract  multipart photo → suggested items (+ photo url)
  POST /r/{slug}/api/catalog/save     {items, photo_url} → write to this store
  GET  /r/{slug}/api/catalog/items    this store's catalog
"""
from fastapi import APIRouter, Request, UploadFile, File
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.services import catalog as catalog_svc
from app.services import tenant as tenant_svc
from app.config import get_settings
from app.paths import home

router    = APIRouter()
settings  = get_settings()
templates = Jinja2Templates(directory=home("templates"))


def _owned(request, slug):
    return tenant_svc.owned_restaurant_id(request, slug)


def _forbidden():
    return JSONResponse({"ok": False, "error": "Not authorized for this store. Sign in as its owner."},
                        status_code=403)


@router.get("/r/{slug}/catalog", response_class=HTMLResponse)
def catalog_page(request: Request, slug: str):
    rid = _owned(request, slug)
    if not rid:
        # Not signed in as this store's owner → send to the dashboard to sign in.
        return RedirectResponse(settings.base_path + "/dashboard")
    summary = tenant_svc.restaurant_summary(rid) or {}
    return templates.TemplateResponse(request=request, name="catalog.html", context={
        "base": settings.base_path, "slug": slug, "store_name": summary.get("name", "")})


@router.post("/r/{slug}/api/catalog/extract")
async def extract(request: Request, slug: str, photo: UploadFile = File(...)):
    rid = _owned(request, slug)
    if not rid:
        return _forbidden()
    data = await photo.read()
    if not data:
        return {"ok": False, "error": "Empty photo."}
    res = catalog_svc.extract_items_from_image(data, photo.content_type or "image/jpeg")
    try:
        res["photo_url"] = catalog_svc.store_photo(rid, data, photo.filename or "photo.jpg")
    except Exception as e:
        print(f"[catalog] store photo err: {e}")
        res["photo_url"] = None
    return res


@router.post("/r/{slug}/api/catalog/save")
async def save(request: Request, slug: str):
    rid = _owned(request, slug)
    if not rid:
        return _forbidden()
    b = await request.json()
    created = catalog_svc.save_items(rid, b.get("items") or [], image_url=b.get("photo_url"))
    return {"ok": True, "created": created, "total": len(catalog_svc.list_items(rid))}


@router.get("/r/{slug}/api/catalog/items")
def items(request: Request, slug: str):
    rid = _owned(request, slug)
    if not rid:
        return _forbidden()
    its = catalog_svc.list_items(rid)
    return {"ok": True, "slug": slug, "count": len(its), "items": its}
