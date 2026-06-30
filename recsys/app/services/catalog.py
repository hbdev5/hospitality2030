"""
Photo → AI catalog for no-POS merchants (farmers markets).

Snap a photo of the stand/products → a vision model lists items with a suggested
price → the merchant reviews/edits → save into the tenant's structured catalog
(menu_categories + menu_items). Tenant-scoped by restaurant_id.
"""
import os, re, time, json, base64

from collections import defaultdict

from app.config import get_settings
from app.paths import home
from app.database import SessionLocal, MenuCategory, MenuItem, Menu
from app.services import menu_cache


_VISION_PROMPT = (
    "You are cataloging products for a small shop or farmers-market stand from a "
    "photo. Identify each DISTINCT sellable item visible. For each, give: a short "
    "retail name; a reasonable US retail price as a number (estimate if unsure); a "
    "category (Produce, Fruit, Vegetables, Bakery, Dairy, Deli, Drinks, Pantry, "
    "Flowers, Other); and a unit (each, per lb, bunch, dozen, pint, jar). Ignore "
    "people, signage, bags, and non-products. Return STRICT JSON: "
    '{"items":[{"name":"","price":0,"category":"","unit":""}]}'
)


def extract_items_from_image(image_bytes: bytes, mime: str = "image/jpeg") -> dict:
    """Run the photo through the vision model. Returns {ok, items[], error?}."""
    s = get_settings()
    try:
        from openai import OpenAI
        client = OpenAI(api_key=s.openai_api_key)
        b64 = base64.b64encode(image_bytes).decode()
        r = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": [
                {"type": "text", "text": _VISION_PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ]}],
            response_format={"type": "json_object"},
            max_tokens=900, temperature=0.2,
        )
        data = json.loads(r.choices[0].message.content)
    except Exception as e:
        print(f"[catalog] vision err: {e}")
        return {"ok": False, "error": str(e), "items": []}

    out = []
    for it in (data.get("items") or []):
        name = (it.get("name") or "").strip()
        if not name:
            continue
        try:
            price = round(float(it.get("price") or 0), 2)
        except Exception:
            price = 0.0
        out.append({
            "name": name[:120], "price": price,
            "category": (it.get("category") or "Other").strip()[:64] or "Other",
            "unit": (it.get("unit") or "each").strip()[:32] or "each",
        })
    return {"ok": True, "items": out}


def store_photo(restaurant_id: int, data: bytes, filename: str = "photo.jpg") -> str:
    """Save the uploaded photo under the tenant's static dir; return its public URL."""
    safe = re.sub(r'[^a-zA-Z0-9._-]', '_', filename or 'photo.jpg')[-60:]
    d = home("static", "menu_images", str(restaurant_id))
    os.makedirs(d, exist_ok=True)
    fn = f"{int(time.time())}_{safe}"
    with open(os.path.join(d, fn), "wb") as f:
        f.write(data)
    base = get_settings().public_base_url.rstrip("/")
    return f"{base}/static/menu_images/{restaurant_id}/{fn}"


def save_items(restaurant_id: int, items: list, image_url: str = None) -> int:
    """Write reviewed items into the tenant's structured catalog. Creates
    categories on demand, dedupes by name, and busts the menu cache."""
    db = SessionLocal()
    created = 0
    try:
        cat_ids = {c.name.lower(): c.id for c in
                   db.query(MenuCategory).filter(MenuCategory.restaurant_id == restaurant_id).all()}
        for it in (items or []):
            name = (it.get("name") or "").strip()
            if not name:
                continue
            cat_name = (it.get("category") or "Other").strip() or "Other"
            cid = cat_ids.get(cat_name.lower())
            if not cid:
                c = MenuCategory(restaurant_id=restaurant_id, name=cat_name, sort_order=len(cat_ids))
                db.add(c); db.commit(); db.refresh(c)
                cid = c.id; cat_ids[cat_name.lower()] = cid
            if db.query(MenuItem).filter(MenuItem.restaurant_id == restaurant_id,
                                         MenuItem.name == name).first():
                continue  # dedupe by name
            try:
                cents = int(round(float(it.get("price") or 0) * 100))
            except Exception:
                cents = 0
            unit = (it.get("unit") or "").strip() or None
            db.add(MenuItem(restaurant_id=restaurant_id, category_id=cid, name=name,
                            display_name=name, price_cents=cents, unit_name=unit,
                            image_url=image_url, flag_show=1))
            created += 1
        db.commit()
    finally:
        db.close()
    rebuild_menu_text(restaurant_id)
    menu_cache.invalidate(restaurant_id)
    return created


def rebuild_menu_text(restaurant_id: int):
    """Build/refresh the Menu raw_text summary from the structured catalog so the
    ordering pipeline (which gates on a menu being present) works for catalog-only
    stores. Upserts a single Menu row per restaurant."""
    db = SessionLocal()
    try:
        cats = {c.id: c.name for c in
                db.query(MenuCategory).filter(MenuCategory.restaurant_id == restaurant_id).all()}
        items = (db.query(MenuItem)
                   .filter(MenuItem.restaurant_id == restaurant_id).all())
        items = [it for it in items if not it.flag_delete]
        if not items:
            return
        groups = defaultdict(list)
        for it in items:
            line = f"{it.name} - ${(it.price_cents or 0)/100:.2f}"
            if it.unit_name:
                line += f" ({it.unit_name})"
            groups[cats.get(it.category_id, "Other")].append(line)
        parts = []
        for cat, lines in groups.items():
            parts.append(cat.upper())
            parts.extend("  " + l for l in lines)
        raw = "\n".join(parts)
        menu = (db.query(Menu).filter(Menu.restaurant_id == restaurant_id)
                  .order_by(Menu.id.desc()).first())
        if menu:
            menu.raw_text = raw
        else:
            db.add(Menu(restaurant_id=restaurant_id, filename="catalog", raw_text=raw))
        db.commit()
    finally:
        db.close()


def list_items(restaurant_id: int) -> list:
    db = SessionLocal()
    try:
        rows = (db.query(MenuItem).filter(MenuItem.restaurant_id == restaurant_id)
                  .order_by(MenuItem.id.desc()).all())
        return [{"id": m.id, "name": m.name, "price": (m.price_cents or 0) / 100,
                 "image_url": m.image_url or ""} for m in rows]
    finally:
        db.close()
