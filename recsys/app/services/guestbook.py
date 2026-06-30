"""
Hospitality guest CRM.

DELIBERATELY SEPARATE from the Java app's apppod_beta.GUESTBOOK — raffle and
web signups land in the hospitality (recsys) DB's own `guestbook` table,
tenant-scoped by restaurant_id. Deduped by email or phone: a returning guest's
visit count is bumped instead of creating a duplicate row.
"""
from datetime import datetime
from app.database import SessionLocal, GuestBook


def add_entry(*, restaurant_id, name=None, email=None, phone=None, source="raffle", notes=None):
    """Add or update a guest. Returns the guestbook row id, or None on failure."""
    email = (email or "").strip() or None
    phone = (phone or "").strip() or None
    db = SessionLocal()
    try:
        existing = None
        if email:
            existing = (db.query(GuestBook)
                          .filter(GuestBook.restaurant_id == restaurant_id,
                                  GuestBook.email == email).first())
        if not existing and phone:
            existing = (db.query(GuestBook)
                          .filter(GuestBook.restaurant_id == restaurant_id,
                                  GuestBook.phone == phone).first())
        now = datetime.utcnow()
        if existing:
            existing.visits = (existing.visits or 0) + 1
            existing.last_visit = now
            if name and not existing.name:   existing.name = name
            if email and not existing.email: existing.email = email
            if phone and not existing.phone: existing.phone = phone
            db.commit()
            return existing.id
        g = GuestBook(restaurant_id=restaurant_id, name=name, email=email, phone=phone,
                      source=source, visits=1, last_visit=now, notes=notes)
        db.add(g)
        db.commit()
        db.refresh(g)
        return g.id
    except Exception as e:
        db.rollback()
        print(f"[guestbook] add err: {e}")
        return None
    finally:
        db.close()


def list_entries(restaurant_id, limit=200):
    db = SessionLocal()
    try:
        rows = (db.query(GuestBook)
                  .filter(GuestBook.restaurant_id == restaurant_id)
                  .order_by(GuestBook.created_at.desc()).limit(limit).all())
        return [{
            "id": g.id, "name": g.name or "", "email": g.email or "",
            "phone": g.phone or "", "source": g.source or "", "visits": g.visits or 0,
            "created_at": g.created_at.strftime("%Y-%m-%d %H:%M") if g.created_at else "",
        } for g in rows]
    finally:
        db.close()
