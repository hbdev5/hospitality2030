"""
Tenant resolution — the single seam every endpoint will use to scope data to a
restaurant. Today most callers still pass restaurant_id=1; Phase 2 routes them
through here instead.

Resolution order:
  1. logged-in merchant   → session['restaurant_id']
  2. public path slug      → /r/{slug}/…  → restaurant_by_slug()
  3. voice/SMS             → Plivo To-number (handled in plivo_hooks already)
"""
import re
from app.database import SessionLocal, Restaurant


def slugify(name: str) -> str:
    s = re.sub(r'[^a-z0-9]+', '-', (name or '').lower()).strip('-')
    return s or 'store'


def unique_slug(db, base: str) -> str:
    base = slugify(base)
    slug, n = base, 2
    while db.query(Restaurant).filter(Restaurant.slug == slug).first():
        slug, n = f"{base}-{n}", n + 1
    return slug


def current_restaurant_id(request):
    """The logged-in merchant's tenant, or None."""
    try:
        return request.session.get('restaurant_id')
    except Exception:
        return None


def restaurant_by_slug(slug: str):
    db = SessionLocal()
    try:
        r = db.query(Restaurant).filter(Restaurant.slug == slug).first()
        return r.id if r else None
    finally:
        db.close()


def owned_restaurant_id(request, slug: str):
    """restaurant_id for {slug} IF the logged-in owner owns it, else None.
    Guards merchant management pages against editing another tenant's data."""
    try:
        owner_id = request.session.get('owner_id')
    except Exception:
        owner_id = None
    if not owner_id:
        return None
    db = SessionLocal()
    try:
        r = db.query(Restaurant).filter(Restaurant.slug == slug).first()
        return r.id if (r and r.owner_id == owner_id) else None
    finally:
        db.close()


def restaurant_summary(restaurant_id: int):
    db = SessionLocal()
    try:
        r = db.query(Restaurant).filter(Restaurant.id == restaurant_id).first()
        if not r:
            return None
        return {"id": r.id, "name": r.name, "slug": r.slug, "plivo_number": r.plivo_number}
    finally:
        db.close()
