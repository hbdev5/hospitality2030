"""
Self-Running Marketing (SRM) — weekly, menu-driven VIP email campaigns.

The agent reads the restaurant's structured menu (items + drinks) and drafts a
weekly campaign: 1 featured offer + 5 secondary offers, biased toward drinks and
lesser-known items so customers of a complex menu actually discover things. The
owner reviews/approves in the SRM studio; on approval we "send" to the VIP
subscriber emails (simulated for the demo — recorded with a real email preview;
a real SMTP/SendGrid send drops into _send() later).

Stored in the existing Playbook table (campaigns_json + status DRAFT/SENT).
"""

import os, json, re, random
from datetime import date, datetime
from openai import OpenAI

from app.config import get_settings
from app.paths import home
from app.database import SessionLocal, Playbook, VipSubscriber
from app.services import menu_cache
from app.services import vip as vip_svc

settings = get_settings()


# ── email enrichment (images, price parsing, badges) ──────────────────────────

_U = "https://images.unsplash.com/"
_IMG = {
    "cocktail": "photo-1514362545857-3bc16c4c7d1b", "strawberry": "photo-1514362545857-3bc16c4c7d1b",
    "drink": "photo-1514362545857-3bc16c4c7d1b", "margarita": "photo-1514362545857-3bc16c4c7d1b",
    "prime rib": "photo-1544025162-d76694265947", "steak": "photo-1544025162-d76694265947",
    "rib": "photo-1544025162-d76694265947", "beef": "photo-1544025162-d76694265947",
    "pizza": "photo-1513104890138-7c749659a591",
    "latte": "photo-1541167760496-1628856ab772", "coffee": "photo-1541167760496-1628856ab772",
    "espresso": "photo-1541167760496-1628856ab772", "mocha": "photo-1541167760496-1628856ab772",
    "cappuccino": "photo-1541167760496-1628856ab772", "americano": "photo-1541167760496-1628856ab772",
    "burger": "photo-1568901346375-23c9450c58cd", "slam": "photo-1568901346375-23c9450c58cd",
    "taco": "photo-1565299624946-b28f40a0ae38",
    "salad": "photo-1512621776951-a57141f2eefd",
    "beer": "photo-1608270586620-248524c67de9",
    "dessert": "photo-1565958011703-44f9829ba187", "cake": "photo-1565958011703-44f9829ba187",
    "bagel": "photo-1585445490387-f47934b73b54",
}
_IMG_DEFAULT = "photo-1517248135467-4c7edcad34c4"

def _img_for(text: str, w: int = 600) -> str:
    t = (text or "").lower()
    pid = next((u for k, u in _IMG.items() if k in t), _IMG_DEFAULT)
    return f"{_U}{pid}?auto=format&fit=crop&w={w}&q=80"


_MENU_IMG_DIR = home("static", "menu_images")

def _inventory_image(restaurant_id: int, item_name: str):
    """Real product photo for an item if the owner has uploaded one to
    static/menu_images/ (r<rid>-<slug>.<ext>); else None → keyword fallback.
    Curry Bliss inventory has no photos yet, so this returns None today."""
    if not item_name:
        return None
    slug = re.sub(r"[^a-z0-9]+", "-", item_name.lower()).strip("-")
    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        fname = f"r{restaurant_id}-{slug}{ext}"
        if os.path.exists(os.path.join(_MENU_IMG_DIR, fname)):
            return f"{settings.public_base_url}/static/menu_images/{fname}"
    return None

def _parse_prices(offer: str):
    """Return (was, now) when the offer states 'from → to' (e.g. '$16 → $11')."""
    m = re.search(r"\$\s?(\d+(?:\.\d{1,2})?)\s*(?:→|->|to)\s*\$\s?(\d+(?:\.\d{1,2})?)", offer or "")
    if m:
        return m.group(1), m.group(2)
    return None, None

def _badge(offer: str) -> str:
    o = (offer or "").strip()
    if not o:
        return ""
    if "free" in o.lower() and "$" not in o:
        return "FREE"
    m = re.search(r"\$\s?(\d+(?:\.\d{1,2})?)\s*off", o, re.I)
    if m:
        return f"${m.group(1)} OFF"
    m = re.search(r"(\d+)\s*%\s*off", o, re.I)
    if m:
        return f"{m.group(1)}% OFF"
    return o[:16]

def enrich_for_email(c: dict) -> dict:
    """Attach images, parsed featured price, and short badges for templates."""
    f = c.get("featured", {}) or {}
    # Prefer a real inventory photo for the item; fall back to keyword lookup.
    # (Curry Bliss inventory has no product photos yet → keyword lookup is used.)
    f["img"] = (_inventory_image(c.get("restaurant_id", 1), f.get("item") or f.get("title"))
                or _img_for(f"{f.get('category','')} {f.get('item','')} {f.get('title','')}", 1000))
    # Prefer the structured numeric price points; fall back to parsing the offer text.
    if f.get("was") and f.get("now"):
        f["was"], f["now"] = _money(f["was"]), _money(f["now"])
    else:
        f["was"], f["now"] = _parse_prices(f.get("offer", ""))
    c["featured"] = f
    for s in (c.get("secondary") or []):
        s["img"] = (_inventory_image(c.get("restaurant_id", 1), s.get("title"))
                    or _img_for(f"{s.get('title','')} {s.get('offer','')}", 300))
        s["badge"] = _badge(s.get("offer", ""))
    return c


def _menu_summary(restaurant_id: int) -> str:
    """Compact category → items WITH prices for the LLM (drinks included).
    Prices matter: offers must be scaled to item value (no free $16 cocktails)."""
    menu = menu_cache._get(restaurant_id, "")
    by_cat = {}
    for it in menu.items:
        base = menu_cache._strip_paren_qualifier(it.name)
        d = by_cat.setdefault(it.category or "Other", {})
        if base not in d:
            d[base] = it.price_cents or 0
    lines = []
    for cat, items in by_cat.items():
        parts = [f"{n} (${c/100:.2f})" if c else n for n, c in list(items.items())[:8]]
        lines.append(f"{cat}: {', '.join(parts)}")
    return "\n".join(lines)


def _money(x) -> str:
    try:
        x = float(x)
        return str(int(x)) if x == int(x) else f"{x:.2f}"
    except Exception:
        return str(x)


_EMOJI = {"steak": "🥩", "rib": "🥩", "pizza": "🍕", "coffee": "☕", "latte": "☕",
          "mocha": "☕", "macchiato": "☕", "americano": "☕", "espresso": "☕",
          "beer": "🍺", "taco": "🌮", "burger": "🍔", "slam": "🍔", "salad": "🥗",
          "bagel": "🥯", "gyros": "🥙", "falafel": "🥙", "fish": "🐟"}

def _emoji_for(name: str) -> str:
    t = (name or "").lower()
    return next((e for k, e in _EMOJI.items() if k in t), "🍽️")

def _tier_offer(price: float) -> str:
    if not price or price <= 0: return "Members special"
    if price < 5:   return "Free upgrade"
    if price <= 10: return "$3 off"
    if price < 20:  return "$5 off"
    return "20% off"

def _tier_now(price: float) -> float:
    if not price or price <= 0: return 0
    if price < 5:   return round(price - 0.75, 2)
    if price <= 10: return round(price - 2, 2)
    if price < 20:  return round(price - 5, 2)
    return round(price * 0.8, 2)


def _template_campaign(name: str, hero: str, hero_price: float, price_map: dict) -> dict:
    """Deterministic, menu-grounded campaign used when the LLM is unavailable
    (e.g. OpenAI quota). Built around the chosen hero so Regenerate still varies."""
    hero = hero or "Chef's Selection"
    was  = hero_price or 14
    now  = _tier_now(was)
    others = [n for n in price_map.keys() if n.lower() != hero.lower()]
    random.shuffle(others)
    secondary = [{"emoji": _emoji_for(n), "title": n, "offer": _tier_offer(price_map.get(n, 0))}
                 for n in others[:5]]
    return {
        "featured": {"emoji": _emoji_for(hero), "title": hero, "item": hero, "category": "",
                     "description": f"This week, savor our {hero} — a members-only favorite at a special VIP price.",
                     "was": was, "now": now, "offer": f"VIP ${_money(now)} (reg ${_money(was)})"},
        "secondary": secondary,
    }


def menu_items(restaurant_id: int = 1) -> list:
    """Distinct real menu items (name, price, category) — powers the owner's
    featured-item picker."""
    menu = menu_cache._get(restaurant_id, "")
    seen, out = set(), []
    for it in menu.items:
        base = menu_cache._strip_paren_qualifier(it.name)
        if base.lower() in seen:
            continue
        seen.add(base.lower())
        out.append({"name": base, "price": round((it.price_cents or 0) / 100, 2),
                    "category": it.category or ""})
    out.sort(key=lambda x: (x["category"], -x["price"]))
    return out


def generate(restaurant_id: int = 1, featured_item: str = None) -> dict:
    program = vip_svc.get_program(restaurant_id)
    name    = (program.card_title or program.program_name) if program else "Curry Bliss"
    summary = _menu_summary(restaurant_id)

    # Rotate the hero across the restaurant's REAL menu sections (no invented items).
    menu = menu_cache._get(restaurant_id, "")
    cats, price_map = {}, {}
    for it in menu.items:
        base = menu_cache._strip_paren_qualifier(it.name)
        cats.setdefault(it.category or "Other", []).append(base)
        price_map.setdefault(base, round((it.price_cents or 0) / 100, 2))
    cat_names = list(cats.keys())
    db0 = SessionLocal()
    try:
        count = db0.query(Playbook).filter(Playbook.restaurant_id == restaurant_id).count()
    finally:
        db0.close()
    focus_cat   = cat_names[count % len(cat_names)] if cat_names else "menu"
    focus_items = list(dict.fromkeys(cats.get(focus_cat, [])))[:8]
    seed = random.randint(1000, 9999)

    # The hero is ALWAYS a concrete real item — owner-picked, or (Auto/AI-suggested)
    # a random item from the rotating section. Picking it ourselves guarantees
    # variety on every Regenerate (the model otherwise defaults to the priciest item).
    if not featured_item:
        pool = focus_items or [menu_cache._strip_paren_qualifier(it.name) for it in menu.items]
        featured_item = random.choice(pool) if pool else None

    chosen_price = None
    if featured_item:
        fi = featured_item.strip().lower()
        for it in menu.items:
            if menu_cache._strip_paren_qualifier(it.name).lower() == fi:
                chosen_price = round((it.price_cents or 0) / 100, 2)
                break
    hero_line = (f"The HERO this week MUST be exactly '{featured_item}'"
                 + (f" (menu price ${_money(chosen_price)})" if chosen_price else "")
                 + ". Build the entire campaign around it.") if featured_item else \
                (f"Feature an item from the '{focus_cat}' section this week.")

    sys = (
        "You are a creative restaurant marketing director writing this week's VIP email. "
        "Craft the campaign around ONE hero item.\n"
        "CRITICAL: use ONLY items that appear in the MENU below — NEVER invent a dish or drink. "
        "The hero 'title' and 'item' MUST be an EXACT item name from the menu (you may add an "
        "elegant tagline only inside 'description'). Return STRICT JSON:\n"
        '{"featured":{"emoji":"","title":"<real menu item name>","item":"<same real item>",'
        '"category":"<one of: coffee, steak, pizza, taco, beer, burger, bagel, salad, dessert>",'
        '"description":"<elegant 1-2 sentence story about that real item>","was":<real menu price>,"now":<VIP price>},'
        '"secondary":[{"emoji":"","title":"<real menu item>","offer":""}]}\n'
        "Hero 'was' = that item's real menu price; 'now' = VIP price a few dollars less. Members pay "
        "a monthly fee, so keep it BALANCED — never free over $10.\n"
        "Exactly 5 secondary offers, all REAL menu items, balanced discounts (under $5 may be "
        "free/BOGO; $5–$10 up to $3 off; over $10 a few $ off or 20–30%). Concrete, elegant, fresh."
    )
    user = (f"Restaurant: {name}\n{hero_line}\n"
            f"FULL MENU (names + prices):\n{summary}\n"
            f"Creativity seed: {seed} — vary the angle, but stay strictly within the menu above.")

    data = None
    try:
        client = OpenAI(api_key=settings.openai_api_key)
        r = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": sys}, {"role": "user", "content": user}],
            response_format={"type": "json_object"},
            temperature=0.95, max_tokens=650,
        )
        data = json.loads(r.choices[0].message.content)
    except Exception as e:
        print(f"[srm] generation error: {e}")
    if not data or "featured" not in data:
        # LLM unavailable (e.g. OpenAI quota) → menu-grounded template around the hero.
        data = _template_campaign(name, featured_item, chosen_price, price_map)

    # Normalize featured pricing → always show a price point.
    f = data.get("featured", {}) or {}
    try:    was = float(f.get("was") or 0)
    except Exception: was = 0
    try:    now = float(f.get("now") or 0)
    except Exception: now = 0
    if chosen_price:                 # owner-picked item → use its real menu price
        was = chosen_price
    if not (0 < now < was):          # sanity fallback
        was, now = was or 14, (now if 0 < now < (was or 14) else round((was or 14) * 0.78, 2))
    f["was"], f["now"] = round(was, 2), round(now, 2)
    f["offer"] = f"VIP ${_money(now)} (reg ${_money(was)})"
    data["featured"] = f

    # Normalize to exactly 5 secondary (pad from the menu-grounded template if short)
    sec = data.get("secondary") or []
    if len(sec) < 5:
        sec = sec + _template_campaign(name, featured_item, chosen_price, price_map)["secondary"]
    data["secondary"] = sec[:5]

    wk = date.today().strftime("%b %d, %Y")
    data["week_of"] = wk
    data["restaurant"] = name

    db = SessionLocal()
    try:
        pb = Playbook(restaurant_id=restaurant_id, week_of=wk,
                      campaigns_json=json.dumps(data), status="DRAFT")
        db.add(pb); db.commit(); db.refresh(pb)
        data["id"] = pb.id
        data["status"] = "DRAFT"
        print(f"[srm] generated campaign #{pb.id} for {name} ({wk})")
    finally:
        db.close()
    return data


def _latest(db, restaurant_id):
    return (db.query(Playbook)
              .filter(Playbook.restaurant_id == restaurant_id)
              .order_by(Playbook.id.desc()).first())


def current(restaurant_id: int = 1) -> dict:
    db = SessionLocal()
    try:
        pb = _latest(db, restaurant_id)
        if not pb:
            return None
        d = json.loads(pb.campaigns_json or "{}")
        d["id"] = pb.id; d["status"] = pb.status; d["week_of"] = pb.week_of
        return d
    finally:
        db.close()


def update(restaurant_id: int, featured: dict = None, secondary: list = None,
           style: int = None) -> dict:
    """Persist owner edits (offers and/or chosen email style) and reset to DRAFT."""
    db = SessionLocal()
    try:
        pb = _latest(db, restaurant_id)
        if not pb:
            return None
        d = json.loads(pb.campaigns_json or "{}")
        if isinstance(featured, dict):
            d["featured"] = {**d.get("featured", {}), **featured}
        if isinstance(secondary, list):
            d["secondary"] = secondary[:5]
        if style in (1, 2, 3):
            d["style"] = style
        for k in ("sent_count", "email_count", "sent_at", "send_result"):
            d.pop(k, None)
        pb.campaigns_json = json.dumps(d)
        pb.status = "DRAFT"
        db.commit()
        d["id"] = pb.id; d["status"] = "DRAFT"; d["week_of"] = pb.week_of
        return d
    finally:
        db.close()


def subscriber_count(restaurant_id: int = 1) -> int:
    db = SessionLocal()
    try:
        return (db.query(VipSubscriber)
                  .filter(VipSubscriber.restaurant_id == restaurant_id,
                          VipSubscriber.status == "active").count())
    finally:
        db.close()


def approve(restaurant_id: int = 1) -> dict:
    """Approve the latest draft and 'send' to active VIP subscribers.
    Demo: records recipients + count and marks SENT (no real email). A real
    provider plugs into _send()."""
    db = SessionLocal()
    try:
        pb = _latest(db, restaurant_id)
        if not pb:
            return {"status": "none"}
        subs = (db.query(VipSubscriber)
                  .filter(VipSubscriber.restaurant_id == restaurant_id,
                          VipSubscriber.status == "active").all())
        emails = [s.email for s in subs if s.email]
        d = json.loads(pb.campaigns_json or "{}")
        d["sent_count"]   = len(subs)
        d["email_count"]  = len(emails)
        d["sent_at"]      = datetime.utcnow().isoformat()
        pb.campaigns_json = json.dumps(d)
        pb.status = "SENT"
        db.commit()
        _send(d, emails)
        return {"status": "SENT", "sent_count": len(subs), "email_count": len(emails)}
    finally:
        db.close()


def _email_html(c: dict) -> str:
    rest = c.get("restaurant", "Curry Bliss")
    f = c.get("featured", {}) or {}
    rows = "".join(
        f'<tr><td style="padding:9px 0;border-bottom:1px solid #eee;font-size:15px">'
        f'{s.get("emoji","•")} <b>{s.get("title","")}</b></td>'
        f'<td style="padding:9px 0;border-bottom:1px solid #eee;text-align:right;color:#C5A03C;font-weight:700;font-size:14px">{s.get("offer","")}</td></tr>'
        for s in (c.get("secondary") or [])
    )
    return f"""\
<div style="max-width:600px;margin:0 auto;font-family:Arial,Helvetica,sans-serif;color:#2a2a2a">
  <div style="background:#1f2430;color:#fff;padding:22px;text-align:center">
    <div style="font-family:Georgia,serif;font-size:26px;color:#C5A03C;font-weight:700">{rest}</div>
    <div style="font-size:12px;color:#b9c0cc;letter-spacing:1px">VIP REWARDS · sent weekly</div>
  </div>
  <div style="padding:24px 24px 8px"><h1 style="font-size:22px;margin:0">Your Weekly VIP Picks 🎁</h1>
    <div style="color:#8a93a6;font-size:13px">Week of {c.get('week_of','')}</div></div>
  <div style="margin:16px 24px;border:2px solid #f0c869;border-radius:14px;padding:22px;background:#fff7e6;text-align:center">
    <div style="font-size:42px">{f.get('emoji','⭐')}</div>
    <h2 style="font-size:23px;margin:8px 0 6px">{f.get('title','')}</h2>
    <div style="color:#5a6172;font-size:15px;margin-bottom:14px">{f.get('description','')}</div>
    <span style="display:inline-block;background:#1f2430;color:#fff;font-weight:700;padding:11px 20px;border-radius:9px">{f.get('offer','')}</span>
  </div>
  <div style="padding:4px 24px">
    <h3 style="font-size:13px;letter-spacing:1.5px;color:#8a93a6;text-transform:uppercase">More this week</h3>
    <table style="width:100%;border-collapse:collapse">{rows}</table>
  </div>
  <div style="background:#f6f3ee;padding:18px 24px;text-align:center;color:#8a93a6;font-size:12px;margin-top:14px">
    You're receiving this as a {rest} VIP member · Show this email or your member card to redeem.
  </div>
</div>"""


def _send(campaign: dict, emails: list):
    """Send the campaign to subscribers if SMTP is configured; else simulate."""
    from app.services import emailer
    title = campaign.get("featured", {}).get("title", "VIP picks")
    if emailer.configured() and emails:
        subject = f"Your {campaign.get('restaurant','Curry Bliss')} VIP picks — week of {campaign.get('week_of','')}"
        res = emailer.send_html(emails, subject, _email_html(campaign),
                                from_name=f"{campaign.get('restaurant','Curry Bliss')} VIP")
        campaign["send_result"] = res
        print(f"[srm] REAL send '{title}' → {res}")
    else:
        print(f"[srm] SEND simulated (no SMTP): '{title}' to {len(emails)} emails "
              f"{emails[:5]}{'…' if len(emails) > 5 else ''}")
