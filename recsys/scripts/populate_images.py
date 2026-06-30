"""
Populate menu_items.image_url with online (Unsplash) dish photos, keyed by
item-name keywords. Idempotent — re-run anytime; only fills/refreshes URLs.

Usage (on VM):  cd /home/azureuser/work/recsys && python3 scripts/populate_images.py
"""
import os, sys, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.database import SessionLocal, MenuItem

_U = "https://images.unsplash.com/"
def _img(pid, w=600):
    return f"{_U}{pid}?auto=format&fit=crop&w={w}&q=80"

# Category → verified-rendering Unsplash photo id
CAT = {
    "COFFEE": "photo-1541167760496-1628856ab772",
    "BEER":   "photo-1608270586620-248524c67de9",
    "PIZZA":  "photo-1513104890138-7c749659a591",
    "STEAK":  "photo-1544025162-d76694265947",
    "BURGER": "photo-1568901346375-23c9450c58cd",
    "TACO":   "photo-1565299624946-b28f40a0ae38",
    "SALAD":  "photo-1512621776951-a57141f2eefd",
    "BAGEL":  "photo-1585445490387-f47934b73b54",
}
# keyword (lowercase) → category. Matched leading-word so plurals work
# ("tacos" → "taco") and "platter" does NOT match "latte".
KEYWORD_IMG = [
    ("latte","COFFEE"),("macchiato","COFFEE"),("mocha","COFFEE"),("americano","COFFEE"),
    ("espresso","COFFEE"),("cappuccino","COFFEE"),("coffee","COFFEE"),
    ("miller","BEER"),("bud","BEER"),("lager","BEER"),("beer","BEER"),
    ("pizza","PIZZA"),
    ("t-bone","STEAK"),("prime rib","STEAK"),("flank","STEAK"),("rib","STEAK"),("steak","STEAK"),
    ("slam","BURGER"),("burger","BURGER"),
    ("taco","TACO"),
    ("salad","SALAD"),
    ("bagel","BAGEL"),
]
_DEFAULT = "photo-1414235077428-338989a2e8c0"   # plated restaurant dish


def pick(name: str) -> str:
    n = (name or "").lower()
    for kw, cat in KEYWORD_IMG:
        # leading word-boundary only → matches plurals, skips substrings like "platter"
        if re.search(rf"(?<!\w){re.escape(kw)}", n):
            return _img(CAT[cat])
    return _img(_DEFAULT)


def run(restaurant_id: int = 1):
    db = SessionLocal()
    try:
        items = db.query(MenuItem).filter(MenuItem.restaurant_id == restaurant_id).all()
        n = 0
        for it in items:
            it.image_url = pick(it.name)
            n += 1
        db.commit()
        print(f"[populate_images] set image_url for {n} items (restaurant {restaurant_id})")
    finally:
        db.close()


if __name__ == "__main__":
    run(1)
