"""
MenuCache v3 — structured menu, Java parity.

Replaces the previous regex-on-raw-text version. Reads from the structured
menu_categories / menu_items / menu_modifier_groups / menu_modifier_options /
menu_item_modifier_groups tables.

Tools (called by OpenAI orchestrator):
  - search_menu(query)       → fuzzy item search with prices
  - get_menu_categories()    → category list
  - get_item_details(name)   → item + price + description + modifier groups
  - get_modifier_options(item_name) → grouped modifier options with prices

Falls back to raw_text search for restaurants without structured menus.
"""

import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# ── Cached menu structure ─────────────────────────────────────────────────────

@dataclass
class CachedModifierOption:
    name:        str
    price_cents: int = 0

@dataclass
class CachedModifierGroup:
    id:             int
    name:           str
    selection_type: str = "multi"
    min_select:     int = 0      # >=1 means this group is REQUIRED (Java COMBOITEMS MINMAX)
    max_select:     int = 99
    options:        List[CachedModifierOption] = field(default_factory=list)

    @property
    def required(self) -> bool:
        return (self.min_select or 0) >= 1

@dataclass
class CachedItem:
    id:           int
    name:         str
    price_cents:  int = 0
    description:  str = ""
    category:     str = "General"
    modifier_group_ids: List[int] = field(default_factory=list)

    @property
    def price_dollars(self) -> float:
        return self.price_cents / 100.0

@dataclass
class CachedMenu:
    items:           List[CachedItem]                = field(default_factory=list)
    categories:      List[str]                       = field(default_factory=list)
    modifier_groups: Dict[int, CachedModifierGroup]  = field(default_factory=dict)
    loaded_at:       float                           = 0.0


# ── Cache + loader ────────────────────────────────────────────────────────────

_cache: Dict[int, CachedMenu] = {}
TTL_SEC = 300   # 5-minute TTL


def _load_structured(restaurant_id: int) -> Optional[CachedMenu]:
    """Load the structured menu from DB. Returns None if no items exist."""
    try:
        from app.database import (
            SessionLocal, MenuCategory, MenuItem,
            MenuModifierGroup, MenuModifierOption, MenuItemModifierGroup,
        )
    except Exception as e:
        print(f"[menu_cache] structured tables not available: {e}")
        return None

    db = SessionLocal()
    try:
        # Categories
        cats = db.query(MenuCategory).filter(
            MenuCategory.restaurant_id == restaurant_id
        ).order_by(MenuCategory.sort_order).all()
        cat_name_by_id = {c.id: c.name for c in cats}

        # Items
        items_q = db.query(MenuItem).filter(
            MenuItem.restaurant_id == restaurant_id
        ).order_by(MenuItem.name).all()
        if not items_q:
            return None

        # Item ↔ ModifierGroup links
        links_q = db.query(MenuItemModifierGroup).join(
            MenuItem, MenuItem.id == MenuItemModifierGroup.item_id,
        ).filter(MenuItem.restaurant_id == restaurant_id).all()
        groups_per_item: Dict[int, List[int]] = {}
        for link in links_q:
            groups_per_item.setdefault(link.item_id, []).append(link.group_id)

        # Modifier groups + options (one round trip each thanks to lazy='joined')
        groups_q = db.query(MenuModifierGroup).filter(
            MenuModifierGroup.restaurant_id == restaurant_id
        ).all()
        cached_groups: Dict[int, CachedModifierGroup] = {}
        for g in groups_q:
            cached_groups[g.id] = CachedModifierGroup(
                id             = g.id,
                name           = g.name,
                selection_type = g.selection_type or 'multi',
                min_select     = int(g.min_select or 0),
                max_select     = int(g.max_select if g.max_select is not None else 99),
                options        = [
                    CachedModifierOption(name=o.name, price_cents=int(o.price_cents or 0))
                    for o in g.options
                ],
            )

        cached_items: List[CachedItem] = []
        for it in items_q:
            cached_items.append(CachedItem(
                id          = it.id,
                name        = it.name,
                price_cents = int(it.price_cents or 0),
                description = it.description or "",
                category    = cat_name_by_id.get(it.category_id, "General"),
                modifier_group_ids = groups_per_item.get(it.id, []),
            ))

        menu = CachedMenu(
            items           = cached_items,
            categories      = [c.name for c in cats],
            modifier_groups = cached_groups,
            loaded_at       = time.time(),
        )
        print(f"[menu_cache] loaded structured menu for restaurant {restaurant_id}: "
              f"{len(cached_items)} items, {len(cats)} categories, {len(cached_groups)} mod groups")
        return menu
    finally:
        db.close()


def _get(restaurant_id: int, raw_text: str = "") -> CachedMenu:
    """Return cached menu, reloading if stale or missing."""
    cached = _cache.get(restaurant_id)
    if cached and (time.time() - cached.loaded_at) < TTL_SEC:
        return cached
    menu = _load_structured(restaurant_id)
    if menu is None:
        # Fallback for restaurants without structured menus — return empty;
        # raw_text search functions handle this below.
        menu = CachedMenu(loaded_at=time.time())
        print(f"[menu_cache] no structured menu for restaurant {restaurant_id} — raw_text only")
    _cache[restaurant_id] = menu
    return menu


def invalidate(restaurant_id: int):
    _cache.pop(restaurant_id, None)


# ── Spanish keyword aliases for cross-language search ────────────────────────

_ES_ALIASES = {
    "cafe": "coffee", "café": "coffee", "cafè": "coffee",
    "frio": "cold",   "frío": "cold",   "caliente": "hot",
    "leche": "milk",  "té": "tea",      "te": "tea",
    "jugo": "juice",  "agua": "water",  "pan": "bread",
    "pizza": "pizza", "ensalada": "salad", "carne": "steak",
    "pollo": "chicken", "bebida": "drink",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _strip_paren_qualifier(name: str) -> str:
    """'Sourdough (Slam burger)' → 'Slam burger'. The DB stores variants as
    'Variant (Base name)' — for matching against guest speech we want the base."""
    m = re.match(r'^\s*([^(]+?)\s*\(([^)]+)\)\s*$', name)
    if m:
        return m.group(2).strip()
    return name


def _name_haystack(item: CachedItem) -> str:
    """All searchable text for an item, lowercase."""
    return " ".join([
        item.name.lower(),
        _strip_paren_qualifier(item.name).lower(),
        item.category.lower(),
        (item.description or "").lower(),
    ])


def _format_price(cents: int) -> str:
    if cents <= 0:
        return ""
    return f"${cents / 100:.2f}"


# ── Tool: search_menu ────────────────────────────────────────────────────────

def search_menu(restaurant_id: int, raw_text: str, query: str) -> str:
    menu = _get(restaurant_id, raw_text)
    q = (query or "").lower().strip()
    if not q:
        return "No items found."

    # Whole-word replacement only — a naive substring replace corrupted English
    # words containing a Spanish token (e.g. "te"->"tea" turned "latte" into
    # "lattea", so lattes were never found).
    for es, en in _ES_ALIASES.items():
        q = re.sub(rf'\b{re.escape(es)}\b', en, q)

    # Score-based search: words matched * weight
    q_words = [w for w in q.split() if len(w) > 1]

    scored = []
    for item in menu.items:
        hay = _name_haystack(item)
        # Full phrase match → highest score
        score = 0
        if q in hay:
            score += 10
        for w in q_words:
            if w in hay:
                score += 2
        if score > 0:
            scored.append((score, item))

    scored.sort(key=lambda x: -x[0])
    matches = [it for _, it in scored[:5]]

    if not matches:
        # Fall back to raw_text if structured menu had nothing
        if raw_text and not menu.items:
            return _raw_text_search(raw_text, q)
        return f"No items found matching '{query}'."

    lines = []
    for it in matches:
        base = _strip_paren_qualifier(it.name)
        # Show base name + variant qualifier (if any)
        if base != it.name:
            label = f"{base} ({it.name.split('(')[0].strip()})"
        else:
            label = it.name
        price = _format_price(it.price_cents)
        line  = f"- {label}"
        if price:
            line += f": {price}"
        if it.description:
            line += f" — {it.description}"
        lines.append(line)
    return "From menu:\n" + "\n".join(lines)


# ── Tool: get_menu_categories ────────────────────────────────────────────────

def get_categories(restaurant_id: int, raw_text: str) -> str:
    menu = _get(restaurant_id, raw_text)
    if menu.categories:
        return "Available sections: " + ", ".join(menu.categories)
    # Raw text fallback for legacy restaurants
    if raw_text:
        headers = []
        for line in raw_text.replace('\\n', '\n').splitlines():
            line = line.strip()
            if line and line.isupper() and 3 < len(line) < 50 and not any(c.isdigit() for c in line):
                if line not in headers:
                    headers.append(line.title())
            if len(headers) >= 15:
                break
        if headers:
            return "Available sections: " + ", ".join(headers)
    return "Menu sections not available — try asking about specific items."


# ── Item finder (used by add_to_cart price lookup too) ───────────────────────

def find_item(menu: CachedMenu, item_name: str) -> Optional[CachedItem]:
    """
    Best-effort match for guest-spoken item name → cached item. Scores all
    items by word overlap (including the parenthesized variant qualifier),
    then returns the highest-scoring item. Without scoring, the first fuzzy
    match wins — and "Sourdough Slam burger" was returning the French Toast
    variant because "slam" matched first.
    """
    n = item_name.lower().strip()
    if not n:
        return None

    # Exact match on full or base name
    for it in menu.items:
        if it.name.lower() == n or _strip_paren_qualifier(it.name).lower() == n:
            return it

    query_words = [w for w in n.split() if len(w) > 1]
    if not query_words:
        return None

    best, best_score = None, 0
    for it in menu.items:
        full_lower = it.name.lower()
        # Include variant qualifier (e.g. "Sourdough" from "Sourdough (Slam burger)")
        # by replacing parens with spaces so they become matchable words.
        haystack = full_lower.replace('(', ' ').replace(')', ' ')
        score = sum(1 for w in query_words if w in haystack)
        # Bonus when ALL query words match
        if score == len(query_words):
            score += 2
        # Bonus when the query is a contiguous substring of the haystack
        if n in haystack:
            score += 1
        if score > best_score:
            best, best_score = it, score
    return best if best_score > 0 else None


# ── Tool: get_item_details ───────────────────────────────────────────────────

def get_item_details(restaurant_id: int, raw_text: str, item_name: str) -> str:
    menu = _get(restaurant_id, raw_text)
    found = find_item(menu, item_name)

    if not found:
        if raw_text and not menu.items:
            r = _raw_text_search(raw_text, item_name)
            if not r.startswith("No items"):
                return r
        return f"'{item_name}' not found. Try search_menu to find it."

    # Strip restaurant-set descriptions that just list default ingredients
    # ("with double patty, cheese and bacon"). GPT was reading these verbatim
    # which produced "Slam Burger is $10.50, featuring double patty, cheese
    # and bacon". User wants generic configurable framing instead.
    desc = (found.description or "").strip()
    if desc.lower().startswith(("with ", "comes with ", "served with ", "includes ")):
        desc = ""  # drop — modifier groups below will replace this

    lines = [f"{found.name}"]
    if found.price_cents > 0:
        lines.append(f"Price: {_format_price(found.price_cents)}")
    if desc:
        lines.append(f"Description: {desc}")
    lines.append(f"Category: {found.category}")

    if found.modifier_group_ids:
        # Short, voice-friendly summary of WHAT THE GUEST CAN CONFIGURE.
        # Tells GPT: "this item has these configurable groups; offer them".
        group_names = []
        for gid in found.modifier_group_ids:
            grp = menu.modifier_groups.get(gid)
            if grp and grp.options:
                group_names.append(grp.name)
        if group_names:
            lines.append("Customizable: " + ", ".join(group_names))
        lines.append("Available options:")
        for gid in found.modifier_group_ids:
            grp = menu.modifier_groups.get(gid)
            if not grp or not grp.options:
                continue
            opts_str = ", ".join(
                f"{o.name}" + (f" (+{_format_price(o.price_cents)})" if o.price_cents > 0 else "")
                for o in grp.options
            )
            lines.append(f"  {grp.name}: {opts_str}")
    return "\n".join(lines)


# ── Tool: get_modifier_options ───────────────────────────────────────────────

def get_modifier_options(restaurant_id: int, raw_text: str, item_name: str) -> str:
    """
    Returns ALL modifier groups for an item with their options + prices.
    THIS is what the LLM uses when the guest asks "what sides do you have"
    or "what protein options". The old regex version returned empty for
    most items — this returns the real grouped data.
    """
    menu = _get(restaurant_id, raw_text)
    found = find_item(menu, item_name)
    if not found:
        return f"No modifier options found for '{item_name}'."

    if not found.modifier_group_ids:
        return f"'{found.name}' has no customization options."

    lines = [f"Options for {found.name}:"]
    for gid in found.modifier_group_ids:
        grp = menu.modifier_groups.get(gid)
        if not grp or not grp.options:
            continue
        opts_str = ", ".join(
            f"{o.name}" + (f" (+{_format_price(o.price_cents)})" if o.price_cents > 0 else "")
            for o in grp.options
        )
        lines.append(f"  {grp.name}: {opts_str}")
    if len(lines) == 1:
        return f"'{found.name}' has no customization options configured."
    return "\n".join(lines)


# ── Price lookup (used by add_to_cart) ───────────────────────────────────────

def lookup_price_cents(restaurant_id: int, item_name: str) -> int:
    """Returns price in cents, 0 if not found."""
    menu = _get(restaurant_id)
    found = find_item(menu, item_name)
    return found.price_cents if found else 0


# ── Helpers for the configuration-state-machine ──────────────────────────────

def get_item_modifier_groups(restaurant_id: int, item_name: str) -> list:
    """
    Returns list of dicts describing the modifier groups for an item:
      [{"name": "Sides", "options": ["fries", "onion rings", "sweet potato fries"]},
       {"name": "protein", "options": ["Bacon", "Chicken", "avocado", "sausage"]}, ...]

    Used by recommender.py to inject "currently configuring X with these
    modifier groups" into the system prompt — and by the server-side
    auto-router that intercepts modifier utterances without invoking GPT.
    """
    menu  = _get(restaurant_id)
    found = find_item(menu, item_name)
    if not found:
        return []
    out = []
    for gid in found.modifier_group_ids:
        grp = menu.modifier_groups.get(gid)
        if not grp:
            continue
        out.append({
            "name":     grp.name,
            "options":  [o.name for o in grp.options],
            "prices":   {o.name.lower(): o.price_cents for o in grp.options},
            "required": grp.required,
            "min":      grp.min_select,
            "max":      grp.max_select,
        })
    return out


def unsatisfied_required_groups(restaurant_id: int, item_name: str,
                                chosen_modifiers: list) -> list:
    """Java parity (BrowserBotTest item-configuration rule): a required group
    (min_select >= 1) must have at least one chosen option before the order can
    be placed. Returns the list of required group dicts that are NOT yet
    satisfied by `chosen_modifiers`. Empty list = item fully configured."""
    groups = get_item_modifier_groups(restaurant_id, item_name)
    chosen = {m.strip().lower() for m in (chosen_modifiers or [])}
    missing = []
    for g in groups:
        if not g.get("required"):
            continue
        opts = {o.lower() for o in g["options"]}
        if not (chosen & opts):
            missing.append(g)
    return missing


def match_utterance_to_modifiers(restaurant_id: int, item_name: str, utterance: str) -> dict:
    """
    Given the item the guest is currently configuring and their raw utterance,
    return:
      {"positive": [opt, ...], "negated": [opt, ...]}
    `positive` are options to ADD; `negated` are options the guest said no/without to.
    Returns {"positive": [], "negated": []} if no clean match.

    Handles two important edge cases:
      - Longest-match-wins. "sweet potato fries" should NOT also yield "fries".
      - Negation aware. "no bacon, and chicken" → positive=['Chicken'], negated=['Bacon'].
    """
    groups = get_item_modifier_groups(restaurant_id, item_name)
    if not groups:
        return {"positive": [], "negated": []}

    u = (utterance or "").lower()
    if not u:
        return {"positive": [], "negated": []}

    # 1. Find ALL option matches with their positions.
    all_options = []  # [(opt_name, opt_lower, start_idx)]
    for grp in groups:
        for opt in grp["options"]:
            ol = opt.lower()
            # Multi-word: substring; single-word: whole word boundary
            if " " in ol:
                idx = u.find(ol)
                if idx >= 0:
                    all_options.append((opt, ol, idx, idx + len(ol)))
            else:
                for m in re.finditer(rf'\b{re.escape(ol)}\b', u):
                    all_options.append((opt, ol, m.start(), m.end()))

    if not all_options:
        return {"positive": [], "negated": []}

    # 2. Longest-match-wins: when two matches overlap, drop the shorter one.
    all_options.sort(key=lambda x: (x[2], -(x[3] - x[2])))  # by start, then by -length
    final = []
    for opt, ol, start, end in all_options:
        overlaps = False
        for kept in final:
            ks, ke = kept[2], kept[3]
            # any character overlap
            if not (end <= ks or start >= ke):
                # Keep the longer one
                if (ke - ks) >= (end - start):
                    overlaps = True
                    break
                else:
                    final.remove(kept)
                    break
        if not overlaps:
            final.append((opt, ol, start, end))

    # 3. Negation detection. Walk backwards from each match through the prefix,
    # stopping at the nearest clause boundary (comma / "and" / "or" / "but" /
    # "with" / sentence break). Only words BETWEEN the match and that boundary
    # count. This way "no bacon, and chicken" → only Bacon is negated.
    NEGATION_WORDS = {"no", "without", "hold", "skip", "minus", "not", "exclude"}
    BOUNDARY_WORDS = {"and", "or", "but", "plus", "with"}
    positive, negated = [], []
    for opt, ol, start, end in final:
        prefix = u[max(0, start - 50):start]
        # Tokenize: words AND commas (commas matter as boundaries)
        tokens = re.findall(r'[,.;]|\b\w+\b', prefix)
        is_negated = False
        # Walk backwards through tokens until we hit a boundary
        for tok in reversed(tokens):
            if tok in (",", ".", ";"):
                break
            if tok in BOUNDARY_WORDS:
                break
            if tok in NEGATION_WORDS:
                is_negated = True
                break
        # Also handle "X isn't / X is not" — suffix check
        suffix = u[end:end + 20]
        if re.match(r'\s*(is\s+not|isn\'?t|are\s+not|aren\'?t)', suffix):
            is_negated = True
        if is_negated:
            negated.append(opt)
        else:
            positive.append(opt)

    # Dedup while preserving order
    def _dedup(seq):
        seen, out = set(), []
        for s in seq:
            if s.lower() not in seen:
                seen.add(s.lower())
                out.append(s)
        return out

    return {"positive": _dedup(positive), "negated": _dedup(negated)}


# ── Phrase hints for Google STT ──────────────────────────────────────────────

def get_phrase_hints(restaurant_id: int, raw_text: str = "") -> list:
    """All item + modifier names — fed to Google STT as speech_contexts so
    brand vocabulary ('Slamburger', 'Lumberjack', 'Falafel') transcribes
    correctly on first occurrence."""
    menu = _get(restaurant_id, raw_text)
    hints = set()
    for it in menu.items:
        hints.add(it.name)
        base = _strip_paren_qualifier(it.name)
        if base and base != it.name:
            hints.add(base)
        if it.category:
            hints.add(it.category)
    for grp in menu.modifier_groups.values():
        hints.add(grp.name)
        for opt in grp.options:
            hints.add(opt.name)
    # Common ordering verbs
    hints.update([
        "place my order", "complete my order", "I'd like to order", "I want",
        "add to cart", "I'm done", "checkout",
        "no bacon", "no onions", "no cheese", "extra cheese",
    ])
    return [h for h in hints if h and 2 <= len(h) <= 50]


# ── Raw-text fallback (legacy menus without structured data) ─────────────────

def _raw_text_search(raw_text: str, query: str) -> str:
    """Score-based search across raw PDF-extracted text."""
    lines = (raw_text or "").replace('\\n', '\n').splitlines()
    q_words = [w for w in query.lower().split() if len(w) > 2]
    if not q_words:
        return f"No items found matching '{query}'."
    scored = []
    for idx, line in enumerate(lines):
        line_l = line.lower()
        score = sum(1 for w in q_words if w in line_l)
        if score > 0:
            block = " ".join(lines[idx:idx + 3]).strip()
            if block and len(block) > 5:
                scored.append((score, idx, block))
    if not scored:
        return f"No items found matching '{query}'."
    scored.sort(key=lambda x: -x[0])
    seen, results = set(), []
    for _, _, block in scored:
        key = block[:40]
        if key not in seen:
            seen.add(key)
            results.append(block)
        if len(results) >= 5:
            break
    return "From menu:\n" + "\n".join(f"- {r}" for r in results)
