"""
In-memory cart management — mirrors Java's CallSession.CartItem pattern.

One Cart per session (web: browser UUID, phone: call_sid).
Carts expire after 30 minutes of inactivity.
"""

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

CART_TTL = 1800  # 30 min


_cart_item_seq = 0   # process-local id counter for cart line items

def _next_cart_item_id() -> int:
    global _cart_item_seq
    _cart_item_seq += 1
    return _cart_item_seq


@dataclass
class CartItem:
    name:                 str
    quantity:             int   = 1
    price:                float = 0.0   # unit price, 0 if unknown
    modifiers:            List[str] = field(default_factory=list)
    size:                 str  = ""
    special_instructions: str  = ""
    image_url:            str  = ""
    id:                   int   = field(default_factory=_next_cart_item_id)

    def display(self) -> str:
        parts = [f"{self.quantity}x {self.name}"]
        if self.modifiers:
            parts.append(f"({', '.join(self.modifiers)})")
        if self.size:
            parts.append(f"[{self.size}]")
        if self.price:
            parts.append(f"${self.price * self.quantity:.2f}")
        return " ".join(parts)


@dataclass
class Cart:
    session_id:    str
    restaurant_id: int
    items:         List[CartItem] = field(default_factory=list)
    last_active:   float = field(default_factory=time.time)
    caller_phone:  str   = ""   # E.164, set by voice_ws on Plivo start event
    # Java parity: track the item the guest is currently configuring.
    # While set, subsequent modifier utterances ("sweet potato fries", "no bacon",
    # "with avocado") are routed to UPDATE this item — not added as new lines.
    # Cleared when: complete_order, cancel_order, or guest says "anything else /
    # I'm done with that / something else / different item".
    current_item_id: Optional[int] = None
    current_item_name: str = ""
    phone_prompted: bool = False   # asked once for a receipt phone (kiosk/web)

    def touch(self):
        self.last_active = time.time()

    def add(self, name: str, qty: int = 1, price: float = 0.0,
            modifiers: List[str] = None, size: str = "",
            special_instructions: str = "", image_url: str = "") -> str:
        self.touch()
        modifiers = modifiers or []

        # 0. CONFIGURING the current item → always merge into that one line.
        # While a guest is building an item (current_item_id set), any add of the
        # SAME item name updates it — even if the model sends only the new modifier
        # this turn (not the cumulative list). This is the decisive fix for "added
        # Slam Burger twice": non-cumulative modifier adds no longer spawn dupes.
        # (Java parity: all configuration routes to currentInventoryId.)
        if qty <= 1 and self.current_item_id is not None:
            cur = self.find_item_by_id(self.current_item_id)
            if cur and cur.name.lower() == name.lower() and cur.size.lower() == size.lower():
                seen = {m.lower(): m for m in cur.modifiers}
                added = []
                for m in modifiers:
                    if m and m.lower() not in seen:
                        seen[m.lower()] = m
                        cur.modifiers.append(m)
                        added.append(m)
                if special_instructions and special_instructions not in (cur.special_instructions or ""):
                    cur.special_instructions = (
                        f"{cur.special_instructions}, {special_instructions}".strip(", ")
                        if cur.special_instructions else special_instructions)
                if image_url and not cur.image_url:
                    cur.image_url = image_url
                self.current_item_name = cur.name
                if added:
                    return f"Updated {cur.name} — now with {', '.join(cur.modifiers)}."
                return f"{cur.name} is already in your order."

        # Exact match (name + modifiers + size) → bump quantity
        key = (name.lower(), tuple(sorted(modifiers)), size.lower())
        for item in self.items:
            if (item.name.lower(), tuple(sorted(item.modifiers)), item.size.lower()) == key:
                # Idempotent re-add guard: if the model re-calls add_to_cart for the
                # SAME item the guest is already configuring (e.g. after a bare
                # "yes"), do NOT bump quantity — that caused phantom doubles. Only
                # a different item, or an explicit quantity>1, increases the count.
                # (Java's CallSession appends and relies on currentInventoryId; this
                # is the equivalent guard for our merge-style cart.)
                if qty <= 1 and item.id == self.current_item_id:
                    self.current_item_name = item.name
                    return f"{item.name} is already in your order."
                item.quantity += qty
                self.current_item_id   = item.id
                self.current_item_name = item.name
                return f"Updated {item.name} to {item.quantity} in cart."

        # Same-name match with overlapping modifiers → MERGE modifiers instead
        # of creating a new line. This fixes the user-reported bug where
        # "Lumberjack Slam" + "add bacon" + "add sausage" + "add fruit" became
        # THREE separate Lumberjack Slams in the cart. The natural intent is
        # ONE Lumberjack that gets modifications added over multiple turns.
        # We merge only when qty == 1 and the new add is "augmenting" — i.e.
        # all existing modifiers are subset/superset of new ones, OR existing
        # item has fewer modifiers (still being configured).
        if qty == 1:
            for item in self.items:
                if item.name.lower() != name.lower() or item.size.lower() != size.lower():
                    continue
                existing_set = {m.lower() for m in item.modifiers}
                new_set      = {m.lower() for m in modifiers}
                # "Augmenting": new is superset of existing, or vice versa,
                # OR one is empty (e.g. user said "Lumberjack" then "with bacon")
                if new_set.issuperset(existing_set) or existing_set.issuperset(new_set):
                    # Union of modifiers, dedupe case-insensitive but keep title case
                    seen = {}
                    for m in item.modifiers + modifiers:
                        ml = m.lower()
                        if ml not in seen:
                            seen[ml] = m
                    item.modifiers = list(seen.values())
                    # Special instructions: concat if new, replace if empty
                    if special_instructions and special_instructions not in (item.special_instructions or ""):
                        item.special_instructions = (
                            f"{item.special_instructions}, {special_instructions}".strip(", ")
                            if item.special_instructions else special_instructions
                        )
                    self.current_item_id   = item.id
                    self.current_item_name = item.name
                    return (f"Updated {item.name} — now with "
                            f"{', '.join(item.modifiers) if item.modifiers else 'no modifiers'}.")

        new_item = CartItem(
            name=name, quantity=qty, price=price,
            modifiers=modifiers, size=size,
            special_instructions=special_instructions, image_url=image_url,
        )
        self.items.append(new_item)
        self.current_item_id   = new_item.id
        self.current_item_name = new_item.name
        return f"Added {qty}x {name} to cart."

    def find_item_by_id(self, item_id: int) -> Optional["CartItem"]:
        for item in self.items:
            if item.id == item_id:
                return item
        return None

    def clear_current(self):
        """Guest moved on to a different item or finished — stop auto-routing."""
        self.current_item_id   = None
        self.current_item_name = ""

    def add_modifiers_to_current(self, modifiers: List[str], special_instructions: str = "") -> str:
        """Server-side fast path used by the modifier-name auto-router. Returns
        a status string."""
        if self.current_item_id is None:
            return ""
        item = self.find_item_by_id(self.current_item_id)
        if item is None:
            self.current_item_id = None
            return ""
        # Append new modifiers (dedup, case-insensitive)
        seen = {m.lower(): m for m in item.modifiers}
        added = []
        for m in modifiers or []:
            if m and m.lower() not in seen:
                seen[m.lower()] = m
                item.modifiers.append(m)
                added.append(m)
        if special_instructions and special_instructions not in (item.special_instructions or ""):
            item.special_instructions = (
                f"{item.special_instructions}, {special_instructions}".strip(", ")
                if item.special_instructions else special_instructions
            )
        self.touch()
        if added:
            return f"Updated {item.name} with {', '.join(added)}."
        return f"{item.name} already has those."

    def remove(self, name: str) -> str:
        self.touch()
        before = len(self.items)
        self.items = [i for i in self.items if name.lower() not in i.name.lower()]
        if len(self.items) < before:
            return f"Removed {name} from cart."
        return f"{name} not found in cart."

    def clear(self):
        self.items = []
        self.current_item_id   = None
        self.current_item_name = ""
        self.phone_prompted    = False
        self.touch()

    def summary(self) -> str:
        if not self.items:
            return "Your cart is empty."
        lines = [i.display() for i in self.items]
        total = self.total()
        if total:
            lines.append(f"Total: ${total:.2f}")
        return " | ".join(lines)

    def total(self) -> float:
        """Includes modifier prices, looked up from the structured menu."""
        from app.services import menu_cache
        grand = 0.0
        for item in self.items:
            base = item.price * item.quantity
            mod_total = 0.0
            if item.modifiers:
                # Build a lowercase price lookup of all modifier options for THIS
                # item's modifier groups. Cents → dollars.
                try:
                    groups = menu_cache.get_item_modifier_groups(self.restaurant_id, item.name)
                    price_map = {}
                    for g in groups:
                        for name_lc, cents in g.get("prices", {}).items():
                            price_map[name_lc] = (cents or 0) / 100.0
                except Exception:
                    price_map = {}
                for m in item.modifiers:
                    mod_total += price_map.get(m.lower(), 0.0)
                mod_total *= item.quantity
            grand += base + mod_total
        return grand

    def to_dict(self) -> dict:
        return {
            "session_id":    self.session_id,
            "restaurant_id": self.restaurant_id,
            "items": [
                {
                    "name":     i.name,
                    "quantity": i.quantity,
                    "price":    i.price,
                    "modifiers": i.modifiers,
                    "size":     i.size,
                    "special_instructions": i.special_instructions,
                    "image_url": i.image_url,
                }
                for i in self.items
            ],
            "total": self.total(),
        }


# ── Global cart store ─────────────────────────────────────────────────────────

_carts: Dict[str, Cart] = {}


def get_or_create(session_id: str, restaurant_id: int) -> Cart:
    _evict_stale()
    if session_id not in _carts:
        _carts[session_id] = Cart(session_id=session_id, restaurant_id=restaurant_id)
    return _carts[session_id]


def get(session_id: str) -> Optional[Cart]:
    return _carts.get(session_id)


def delete(session_id: str):
    _carts.pop(session_id, None)


def _evict_stale():
    now = time.time()
    stale = [sid for sid, c in _carts.items() if now - c.last_active > CART_TTL]
    for sid in stale:
        del _carts[sid]
