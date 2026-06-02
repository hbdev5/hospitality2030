"""
Recommender — OpenAI gpt-4o-mini with server-side tool calling.

Ported from Java UnifiedPhoneBot / OpenAIOrchestrator.
Tool set mirrors Java: search_menu, get_menu_categories, get_item_details,
get_modifier_options, add_to_cart, remove_from_cart, get_cart,
complete_order, cancel_order.

Menu is NOT in the system prompt (~80% token reduction).
Tool loop max 3 iterations (same as Java MAX_TOOL_LOOPS).
"""

import json, time, re
from openai import OpenAI
from app.config import get_settings
from app.services import menu_cache
from app.services import cart as cart_svc

settings = get_settings()

# ── Tool schema ───────────────────────────────────────────────────────────────

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_menu",
            "description": (
                "Search menu items by name, category, or keyword. Returns up to 5 matches with prices. "
                "Use when guest asks what's on the menu, asks about a specific food or drink."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Item name, category, or keyword"}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_menu_categories",
            "description": "Get all menu sections/categories. Use when guest asks 'what do you have' or wants to browse.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_item_details",
            "description": "Get full details for a specific menu item: description, price, modifiers/options.",
            "parameters": {
                "type": "object",
                "properties": {
                    "item_name": {"type": "string", "description": "Exact or approximate item name"}
                },
                "required": ["item_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_modifier_options",
            "description": (
                "Get available options/modifiers for an item (e.g. protein choice, size, sides). "
                "Call this before add_to_cart if the item has customization options."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "item_name": {"type": "string", "description": "Name of the menu item"}
                },
                "required": ["item_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_to_cart",
            "description": "Add an item to the guest's cart. Call after confirming item and any modifiers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "item_name":    {"type": "string",  "description": "Name of the menu item"},
                    "quantity":     {"type": "integer", "description": "Number of items", "default": 1},
                    "modifiers":    {"type": "array", "items": {"type": "string"},
                                    "description": "Things to ADD or substitute IN, e.g. ['Extra Cheese', 'Bacon']. NEVER put negations here."},
                    "size":         {"type": "string",  "description": "Size if applicable"},
                    "special_instructions": {"type": "string", "description": "Exclusions or special requests, e.g. 'no bacon', 'no onions', 'well done'"},
                },
                "required": ["item_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_from_cart",
            "description": "Remove an item from the guest's cart.",
            "parameters": {
                "type": "object",
                "properties": {
                    "item_name": {"type": "string", "description": "Name of the item to remove"}
                },
                "required": ["item_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_cart",
            "description": "Get the current cart contents and total. Use when guest asks 'what's in my order' or 'what's my total'.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "complete_order",
            "description": "Finalize and place the order. Call when guest confirms they are ready to order.",
            "parameters": {
                "type": "object",
                "properties": {
                    "confirmation": {"type": "string", "description": "Guest confirmation phrase"}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_order",
            "description": "Cancel the current order and clear the cart.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "subscribe_vip",
            "description": (
                "Describe the restaurant's VIP membership and give the guest a link to join. "
                "Call this whenever the guest mentions VIP, membership, loyalty, subscribing, "
                "or asks about member perks/benefits. Returns the plan pitch and a signup link."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "email": {"type": "string", "description": "Guest email if they volunteered one (optional)"}
                },
            },
        },
    },
]


# ── Speech cleanup ─────────────────────────────────────────────────────────────

def clean_for_speech(text: str) -> str:
    text = re.sub(r'\*{1,3}(.*?)\*{1,3}', r'\1', text)
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*[-•*]\s+', '', text, flags=re.MULTILINE)
    text = text.replace('|', ',').replace('`', '')
    text = re.sub(r'_(.*?)_', r'\1', text)
    text = re.sub(r'\n+', ' ', text)
    text = re.sub(r'  +', ' ', text)
    return text.strip()


# ── Price lookup from items_json (fallback when raw_text has no $) ────────────
# raw_text is the source of truth for menu_cache, but PDFs like Denny's lack price text.
# items_json (set during menu upload or admin edit) provides a name→price map.

_price_cache: dict = {}   # restaurant_id → {lowercase_name: price_float}

def _norm_name(s: str) -> str:
    """Lowercase + treat & and 'and' as equivalent for menu lookups."""
    return s.strip().lower().replace("&", " and ").replace("  ", " ")


def _load_prices(restaurant_id: int) -> dict:
    cached = _price_cache.get(restaurant_id)
    if cached is not None:
        return cached
    try:
        from app.database import Menu, SessionLocal
        db = SessionLocal()
        try:
            m = (db.query(Menu)
                   .filter(Menu.restaurant_id == restaurant_id)
                   .order_by(Menu.id.desc()).first())
        finally:
            db.close()
        if not m or not m.items_json:
            _price_cache[restaurant_id] = {}
            return {}
        data = json.loads(m.items_json)
        items = data if isinstance(data, list) else data.get("items", [])
        pmap = {}
        for it in items:
            if not isinstance(it, dict): continue
            name  = (it.get("name") or "").strip()
            price = it.get("price")
            if name and price:
                pmap[_norm_name(name)] = float(price)
        _price_cache[restaurant_id] = pmap
        print(f"[price_cache] loaded {len(pmap)} priced items for restaurant {restaurant_id}")
        return pmap
    except Exception as e:
        print(f"[price_cache] load error: {e}")
        return {}


def invalidate_prices(restaurant_id: int = None):
    """Call after menu upload / price edit."""
    if restaurant_id is None:
        _price_cache.clear()
    else:
        _price_cache.pop(restaurant_id, None)


def _lookup_price(restaurant_id: int, item_name: str) -> float:
    # 1. Try structured menu first (Java parity — real prices, real items)
    try:
        cents = menu_cache.lookup_price_cents(restaurant_id, item_name)
        if cents > 0:
            return cents / 100.0
    except Exception as e:
        print(f"[price] structured lookup error: {e}")

    # 2. Fall back to items_json from the old upload flow
    pmap = _load_prices(restaurant_id)
    key = _norm_name(item_name)
    if pmap:
        if key in pmap: return pmap[key]
        # Substring fallback — pick the longest matching item name (most specific)
        matches = [(name, price) for name, price in pmap.items() if key in name or name in key]
        if matches:
            matches.sort(key=lambda x: -len(x[0]))
            return matches[0][1]
    # No DB match — fall back to category-based defaults so the order total
    # never shows $0. (Demo broke when "premium coffee" + "seasonal fruit" came
    # back as $0 → user saw $13.49 instead of ~$17 total.)
    BEVERAGE_KW = ("coffee", "tea", "juice", "milk", "soda", "lemonade",
                   "smoothie", "shake", "latte", "espresso", "mocha", "drink",
                   "cold brew", "water")
    SIDE_KW     = ("fruit", "hash brown", "fries", "salad", "side",
                   "bread", "biscuit", "muffin", "yogurt", "oatmeal", "grits",
                   "egg", "bacon", "sausage", "ham", "toast")
    DESSERT_KW  = ("ice cream", "cake", "pie", "dessert", "cookie", "brownie")
    k = key
    if any(w in k for w in BEVERAGE_KW): return 3.49
    if any(w in k for w in DESSERT_KW):  return 5.99
    if any(w in k for w in SIDE_KW):     return 2.99
    # Fully unknown item — small placeholder rather than $0 so the order isn't
    # totally free. Real systems would block this; for demo we keep it moving.
    return 4.99


# ── Tool dispatch ──────────────────────────────────────────────────────────────
# Returns (result_text, checkout_url_or_None).  All non-order branches return ("...", None).

def _dispatch(fn: str, args: dict, restaurant_id: int, menu_text: str,
              session_id: str, db=None) -> tuple:
    cart = cart_svc.get_or_create(session_id, restaurant_id)

    if fn == "search_menu":
        return menu_cache.search_menu(restaurant_id, menu_text, args.get("query", "")), None

    if fn == "get_menu_categories":
        return menu_cache.get_categories(restaurant_id, menu_text), None

    if fn == "get_item_details":
        return menu_cache.get_item_details(restaurant_id, menu_text, args.get("item_name", "")), None

    if fn == "get_modifier_options":
        return menu_cache.get_modifier_options(restaurant_id, menu_text, args.get("item_name", "")), None

    if fn == "add_to_cart":
        req_name = (args.get("item_name") or "").strip()
        # ── Java findItem parity: never add an item that isn't on the menu ──
        # A misheard transcript ("Miller Light" → garble) was being added as a
        # fabricated line ("Alaska Middle Of Light $17.99"). Validate first; if
        # the restaurant has a structured menu and nothing matches, refuse and
        # ask the guest to repeat. When matched, add the CANONICAL item (real
        # name + price + photo), not the raw transcript.
        matched = menu_cache.match_item(restaurant_id, req_name)
        if matched is None and menu_cache.has_structured_menu(restaurant_id):
            return (f"Hmm, I couldn't find \"{req_name}\" on our menu — "
                    f"could you say that again?"), None

        item_name   = (menu_cache.base_name(matched) or req_name) if matched else req_name
        item_image  = (matched.image_url if matched else "") or menu_cache.image_for(restaurant_id, req_name)
        price = (matched.price_cents or 0) / 100.0 if matched else 0.0
        # Fallback price for menus without structured prices (raw_text / items_json).
        if price == 0.0:
            detail = menu_cache.get_item_details(restaurant_id, menu_text, req_name)
            m = re.search(r'\$(\d+(?:\.\d{1,2})?)', detail)
            if m:
                price = float(m.group(1))
        if price == 0.0:
            price = _lookup_price(restaurant_id, req_name)

        # Safety net: GPT sometimes echoes negated words as positive modifiers
        # ("no bacon" → modifiers=["bacon"]). Pull out anything that looks like
        # an exclusion and move it to special_instructions. Also normalize casing
        # + dedupe so we don't end up with ["bacon", "Bacon"].
        raw_mods = args.get("modifiers", []) or []
        special  = (args.get("special_instructions") or "").strip()
        cleaned, seen = [], set()
        for m_str in raw_mods:
            if not isinstance(m_str, str) or not m_str.strip():
                continue
            ml = m_str.strip().lower()
            if any(ml.startswith(p) for p in ("no ", "without ", "hold ", "minus ", "skip ")):
                special = f"{special}, {m_str.strip()}".strip(", ").strip()
                continue
            norm = m_str.strip().title()
            if norm.lower() not in seen:
                seen.add(norm.lower())
                cleaned.append(norm)

        add_msg = cart.add(
            name=item_name,
            qty=args.get("quantity", 1),
            price=price,
            modifiers=cleaned,
            size=args.get("size", ""),
            special_instructions=special,
            image_url=item_image,
        )
        # Java pattern: return cart summary so GPT sees what's actually in the cart
        # after the add — prevents duplicate items and helps with state tracking.
        return f"{add_msg} Current cart: {cart.summary()}", None

    if fn == "remove_from_cart":
        return cart.remove(args.get("item_name", "")), None

    if fn == "get_cart":
        return cart.summary(), None

    if fn == "subscribe_vip":
        from app.services import vip as vip_svc
        program = vip_svc.get_program(restaurant_id)
        if not program:
            return ("We don't have a VIP program set up right now. "
                    "Please check back soon!"), None
        identifier = cart.caller_phone or session_id
        existing   = vip_svc.is_subscriber(identifier, restaurant_id)
        if existing:
            return (f"You're already a {program.program_name} member — "
                    f"your benefits apply automatically every visit. "), None
        # Note: membership only activates after PayPal approval on the signup
        # page (see vip_web.vip_capture) — we never grant benefits here.
        link = vip_svc.subscribe_link(session_id)
        return f"{vip_svc.pitch(program)} Join here: {link}", None

    if fn == "complete_order":
        if not cart.items:
            return "Cart is empty — nothing to order.", None

        # Required-modifier guard (Java BrowserBotTest item-config rule): every
        # item must have a choice for each REQUIRED modifier group before the
        # order can be placed. If something's missing, remind the guest instead
        # of completing — even if the model forgot to ask.
        for it in cart.items:
            if it.name.lower().startswith("vip perk"):
                continue
            missing = menu_cache.unsatisfied_required_groups(restaurant_id, it.name, it.modifiers)
            if missing:
                g    = missing[0]
                opts = ", ".join(g["options"][:3])
                # Re-focus the configuring state on this item so the guest's next
                # answer routes straight to it via the modifier fast-path.
                cart.current_item_id   = it.id
                cart.current_item_name = it.name
                return (f"Before I place that — your {it.name} needs a {g['name'].lower()}. "
                        f"Would you like {opts}?"), None

        # ── VIP per-visit benefits (honest + condition-aware) ────────────────
        # Active subscribers get: (1) their recurring free perk, and (2) a visit
        # discount ONLY IF it applies today (e.g. weekdays-only). Both appear on
        # the receipt as explicit line items so the math is transparent.
        vip_discount_pct = 0.0
        vip_discount_amt = 0.0
        vip_cond         = "always"
        vip_applies      = False
        try:
            from app.services import vip as vip_svc
            program    = vip_svc.get_program(restaurant_id)
            identifier = cart.caller_phone or session_id
            sub        = vip_svc.is_subscriber(identifier, restaurant_id)
            if program and sub:
                benefit = (program.recurring_benefit or "").strip()
                if benefit and not any("vip perk" in i.name.lower() for i in cart.items):
                    cart.add(name=f"VIP Perk: {benefit}", qty=1, price=0.0)
                vip_discount_pct = program.visit_discount_percent or 0.0
                vip_cond         = program.discount_condition or "always"
                vip_applies      = vip_discount_pct > 0 and vip_svc.discount_applies_today(vip_cond)
                print(f"[vip] benefit for {identifier}: perk='{benefit}' "
                      f"discount={vip_discount_pct}% cond={vip_cond} applies_today={vip_applies}")
        except Exception as vip_ex:
            print(f"[vip] per-visit benefit error: {vip_ex}")

        summary     = cart.summary()
        subtotal    = cart.total()
        order_items = cart.to_dict()["items"]
        if vip_applies:
            vip_discount_amt = round(subtotal * vip_discount_pct / 100.0, 2)
            order_items = order_items + [{
                "name": f"VIP {int(vip_discount_pct)}% off ({vip_cond})",
                "quantity": 1, "price": -vip_discount_amt,
                "modifiers": [], "size": "", "special_instructions": "",
            }]
        total = round(subtotal - vip_discount_amt, 2)
        order_id = None
        # Persist to DB — use caller-supplied session if present, else open a short-lived one
        from app.database import Order, SessionLocal
        import json as _json
        owns_db = db is None
        local_db = db or SessionLocal()
        try:
            order = Order(
                restaurant_id=restaurant_id,
                session_id=session_id,
                caller_number=cart.caller_phone or None,
                status="confirmed",
                items_json=_json.dumps(order_items),
                total_cents=int(round(total * 100)),
            )
            local_db.add(order)
            local_db.commit()
            order_id = order.id
            print(f"[cart] order saved id={order.id}")
        except Exception as e:
            print(f"[cart] order save error: {e}")
        finally:
            if owns_db:
                local_db.close()

        # SMS link points to our checkout page so the caller sees the order line
        # items (Java pattern — receipt-first, payment-second). The page hosts
        # PayPal Smart Buttons and shows sandbox test credentials.
        # The "Order #X confirmed. Total $X. Complete order: URL" wording got past
        # Plivo's spam filter where the previous "Pay with PayPal: URL" did not.
        checkout_url = f"{settings.public_base_url}/checkout/{order_id}" if order_id and total > 0 else None

        # SMS the caller a checkout link — voice/web sessions only.
        # SMS-driven sessions ("sms-<phone>") get the link inline in the reply,
        # so we skip the auto-SMS to avoid double-messaging the caller.
        is_sms_session = (session_id or "").startswith("sms-")
        if checkout_url and cart.caller_phone and not is_sms_session:
            try:
                import plivo as _plivo
                amount_str = f"${int(total)}" if total == int(total) else f"${total:.2f}"
                sms_text = f"Order #{order_id} confirmed. Total {amount_str}. Complete order: {checkout_url}"
                # Plivo requires E.164 — webhook often delivers `From` as bare digits.
                dst = cart.caller_phone.strip()
                if dst and not dst.startswith("+"):
                    digits = "".join(c for c in dst if c.isdigit())
                    dst = f"+{digits}" if len(digits) >= 11 else f"+1{digits}"
                client = _plivo.RestClient(settings.plivo_auth_id, settings.plivo_auth_token)
                resp = client.messages.create(
                    src=settings.plivo_number,
                    dst=dst,
                    text=sms_text,
                )
                # plivo SDK returns a MessageCreateResponse with .message_uuid (list) or .api_id
                msg_uuid = getattr(resp, "message_uuid", None)
                api_id   = getattr(resp, "api_id", None)
                print(f"[cart] checkout sms to {dst} api_id={api_id} msg_uuid={msg_uuid} text={sms_text!r}")
            except Exception as e:
                print(f"[cart] sms send failed: {e}")

        cart.clear()
        spoken_amount = f"{int(total)} dollars" if total == int(total) else f"{total:.2f} dollars"
        vip_note = (f" Your VIP {int(vip_discount_pct)}% {vip_cond} discount saved you ${vip_discount_amt:.2f}."
                    if vip_applies and vip_discount_amt > 0 else "")
        if checkout_url and cart.caller_phone:
            spoken = f"Order placed! Total {spoken_amount}.{vip_note} I've texted you a PayPal checkout link."
        elif checkout_url:
            spoken = f"Order placed! Total {spoken_amount}.{vip_note} Click the Pay Now button to complete your payment."
        else:
            spoken = f"Order placed!{vip_note} {summary}"
        return spoken, checkout_url

    if fn == "cancel_order":
        cart.clear()
        return "Order cancelled. Cart cleared.", None

    return f"Unknown tool: {fn}", None


# Public wrapper — voice_ws closing branch calls this when the caller says "I'm done"
# with items still in their cart. Returns the spoken confirmation string.
def complete_order(restaurant_id: int, session_id: str) -> str:
    text, _ = _dispatch("complete_order", {}, restaurant_id, "", session_id, db=None)
    return text


# ── Main entry point ───────────────────────────────────────────────────────────

def get_recommendation(
    menu_text:     str,
    query:         str,
    state:         str = "browsing",
    language:      str = "en",
    restaurant_id: int = 1,
    session_id:    str = "default",
    db=None,
    history:       list = None,
) -> dict:

    # ── Server-side modifier fast-path (Java parity) ──────────────────────
    # If the guest is currently configuring an item (current_item_id set),
    # and their utterance cleanly matches one or more modifier options of
    # that item, we route directly to add_to_cart — NO LLM round-trip. This
    # is what made the Java agent feel fast: an "and a side of fries" turn
    # is a 50ms operation instead of a 4-second LLM call.
    cart = cart_svc.get(session_id)
    if cart and cart.current_item_id is not None and cart.current_item_name:
        q_low   = query.lower().strip()
        # Stop the auto-route on intent-to-move-on phrases.
        STOP_ROUTING = (
            "anything else", "something else", "different",
            "next item", "another item", "i'm done", "i am done", "that's all",
            "that is all", "place my order", "place the order",
            "checkout", "complete my order", "cancel",
        )
        if not any(p in q_low for p in STOP_ROUTING):
            try:
                m = menu_cache.match_utterance_to_modifiers(
                    restaurant_id, cart.current_item_name, query
                )
                positive = m.get("positive", [])
                negated  = m.get("negated", [])
            except Exception as e:
                print(f"[fastpath] match error: {e}")
                positive, negated = [], []

            if positive or negated:
                t0 = time.time()
                special = ", ".join(f"no {n.lower()}" for n in negated) if negated else ""
                msg = cart.add_modifiers_to_current(positive, special_instructions=special)
                latency_ms = round((time.time() - t0) * 1000, 1)
                # Build a natural confirmation that covers BOTH positive and negation
                parts = []
                if positive:
                    parts.append(", ".join(p.lower() for p in positive))
                if negated:
                    parts.append("no " + ", no ".join(n.lower() for n in negated))
                confirm = " and ".join(parts) if parts else "ok"
                spoken = f"Got it, {confirm}. Anything else?"
                print(f"[fastpath] {latency_ms}ms: '{query}' → +{positive} -{negated} on {cart.current_item_name}")
                # Include cart snapshot so browser UI / SMS can render the
                # updated receipt without a follow-up roundtrip.
                fp_out = {
                    "recommendation":    spoken,
                    "claude_latency_ms": latency_ms,
                    "noise_flag":        False,
                }
                try:
                    fp_out["total"]      = f"{cart.total():.2f}"
                    fp_out["cart_items"] = cart.to_dict()["items"]
                    img = menu_cache.image_for(restaurant_id, cart.current_item_name)
                    if img:
                        fp_out["feature_image"] = img
                except Exception:
                    pass
                return fp_out

    client = OpenAI(api_key=settings.openai_api_key)

    # Language
    lang_note = ""
    if language and language.startswith("es"):
        lang_note = "The guest is speaking Spanish — respond ENTIRELY in Spanish. "

    # State hints
    state_hints = {
        "browsing":      "Guest is exploring — mention 2-3 items with prices if known, keep it conversational.",
        "item_selected": "Guest chose something — confirm briefly, then ask about modifiers or add-ons.",
        "upsell":        "Guest is considering an add-on — keep it under 10 words.",
        "confirmation":  "Summarise the order briefly, end with 'Anything else?'",
        "greeting":      "Guest just arrived — ask what they'd like in 12 words.",
    }
    state_hint = state_hints.get(state, state_hints["browsing"])

    # Java pattern: cart summary + current-item-being-configured are part of
    # the system prompt so the model sees state on EVERY turn (no get_cart call).
    cart_summary_for_prompt = "empty"
    configuring_block       = ""
    try:
        _cart_now = cart_svc.get_or_create(session_id, restaurant_id)
        if _cart_now.items:
            cart_summary_for_prompt = _cart_now.summary()
        # If the guest is mid-configuration, tell GPT EXACTLY what modifier
        # groups apply and instruct it to update that item, not add new lines.
        # This is the Java OpenAIOrchestrator approach (line 248: setCurrentInventoryId).
        if _cart_now.current_item_id and _cart_now.current_item_name:
            groups = menu_cache.get_item_modifier_groups(restaurant_id, _cart_now.current_item_name)
            if groups:
                required = [g for g in groups if g.get("required")]
                lines = [f"\n--- CURRENTLY CONFIGURING: {_cart_now.current_item_name} ---"]
                lines.append("Modifier groups for THIS item ONLY:")
                for g in groups:
                    tag = " [REQUIRED — guest must pick one]" if g.get("required") else " [optional]"
                    lines.append(f"  {g['name']}{tag}: {', '.join(g['options'])}")
                if required:
                    req_names = ", ".join(g["name"] for g in required)
                    lines.append(
                        f"REQUIRED CONFIG: this item needs a choice for EACH required group ({req_names}). "
                        "Prompt the guest for any missing required group, ONE at a time, offering up to 3 options. "
                        "Do NOT call complete_order until every required group has a choice."
                    )
                lines.append(
                    "RULE: any food/option the guest mentions next is a modifier of THIS item. "
                    "Call add_to_cart with item_name='" + _cart_now.current_item_name + "' and the "
                    "FULL CUMULATIVE modifiers list. Do NOT call add_to_cart with a different "
                    "item_name (like 'Sweet Potato Fries') unless the guest says 'different item' "
                    "or 'something else'. Modifiers must come from the lists above."
                )
                configuring_block = "\n".join(lines)
    except Exception as e:
        print(f"[recommender] prompt-state error: {e}")

    system = (
        "You are Alex, a friendly voice ordering assistant at a diner. "
        + lang_note
        + "CRITICAL RULES (phone call — SHORT, natural):\n"
        + "- HARD LIMIT: 20 words MAX per response. Phone callers can't process more.\n"
        + "- ONE sentence preferred. Two only if absolutely needed.\n"
        + "- Never list more than 3 items at once. Lead with featured/popular items.\n"
        + "- Confirm naturally: 'Got it, Tuscan chicken sandwich.' NOT 'I have added...'.\n"
        + "- Never guess prices or items. Always use the tools to look them up.\n"
        + "- When user says 'I'm done', 'place my order', 'checkout', or similar: CALL complete_order IMMEDIATELY. Do NOT say 'I'll place' or 'finalizing' without calling the tool.\n"
        + "ITEM PRESENTATION (when user asks about or orders an item):\n"
        + "- When the guest asks about a CATEGORY (drinks, beers, coffee, etc.), NAME 2-3 specific menu items (e.g. 'We have Miller Lite, Bud Light, or a Plain Latte') — not just the category — so the kiosk can show their photos.\n"
        + "- A PHOTO of the item is shown to the guest, so be VIVID & APPETIZING: add one short sensory detail to entice (e.g. 'a velvety oat-milk latte', 'a juicy char-grilled burger', 'crisp golden fries'). Keep it within the word limit.\n"
        + "- DO NOT read the raw description verbatim (e.g. don't say 'featuring double patty, cheese and bacon').\n"
        + "- Instead, frame around what's CONFIGURABLE. Use the 'Customizable:' line from get_item_details.\n"
        + "- Example: 'The Slam Burger is $10.50 — comes with sides and a choice of protein. Would you like one?'\n"
        + "- After adding the main item: PROACTIVELY mention sides. Example: 'Got it. It comes with sides — would you like fries, onion rings, or sweet potato fries?'\n"
        + "- After they pick a side, confirm + ask 'anything else?'\n"
        + "TOOLS — prefer ONE tool per turn for speed, but you MAY call add_to_cart right after a lookup in the SAME turn.\n"
        + "ORDER INTENT (critical): when the guest NAMES an item to order ('I'll take a slam burger', 'I want…', 'give me…', 'add…'), you MUST call add_to_cart THIS turn. Saying 'Got it' WITHOUT calling add_to_cart loses the order — never do that.\n"
        + "DON'T DUPLICATE: if that item is ALREADY in the cart (see CART STATE below), do NOT add it again or bump its quantity unless the guest explicitly says 'another', 'one more', or a number. Replying 'yes' to your own offer does NOT mean add again.\n"
        + "FINISH WORDS: if the guest says 'that's it', 'that's all', \"that's everything\", 'I'm done', 'nothing else', 'place my order', or 'checkout' — call complete_order and NEVER add_to_cart on that turn.\n"
        + "- search_menu / get_item_details / get_menu_categories — discover items.\n"
        + "- add_to_cart — add the named item NOW. You do NOT need get_modifier_options first; add it (with any variant the guest named), THEN ask about sides/options. Can pass multiple items.\n"
        + "- get_modifier_options — only when the guest explicitly asks what the choices are.\n"
        + "- get_cart / complete_order / cancel_order — order management.\n"
        + "- subscribe_vip — call when the guest mentions VIP, membership, loyalty, or member perks. It returns the plan and a signup link; just read it back.\n"
        + "MODIFIERS:\n"
        + "- `modifiers` is ONLY for additions or substitutions IN (e.g. 'Extra Cheese', 'Bacon').\n"
        + "- For 'no X' / 'without X' / 'hold the X' → use `special_instructions: \"no X\"`.\n"
        + "  NEVER put a negated word into `modifiers`.\n"
        + "ADDING MODIFIERS TO EXISTING CART ITEM:\n"
        + "- If the guest is configuring an item already in the cart (e.g. 'add bacon to it', 'with fruit', 'also sausage'), call add_to_cart with the SAME item_name and the FULL CUMULATIVE list of modifiers — the cart auto-merges. Do NOT call add_to_cart multiple times for the same item.\n"
        + "- See CART STATE below — if the item is there, you're MODIFYING, not adding a second one.\n\n"
        + "--- CART STATE ---\n"
        + f"Cart: {cart_summary_for_prompt}\n"
        + configuring_block + "\n"
        + f"Tone: {state_hint}"
    )

    messages = [{"role": "system", "content": system}]
    # Multi-turn context — voice_ws / voice_web pass prior {role, content} pairs
    if history:
        for h in history[-12:]:   # cap to last 12 messages (~6 turns)
            r = h.get("role")
            c = (h.get("content") or "").strip()
            if r in ("user", "assistant") and c:
                messages.append({"role": r, "content": c})
    messages.append({"role": "user", "content": query})

    t0 = time.time()
    final_text  = ""
    checkout_url = None   # set if complete_order fires during this turn
    complete_order_called = False  # track whether GPT actually finalized

    # Tool loop max 2 iterations (was 3). On the broken call we saw GPT thrash:
    # get_menu_categories → search_menu → final = 3 round-trips × 3s = 14s
    # latency. 2 is enough: 1 tool call, then final answer.
    MAX_TOOL_LOOPS = 3
    for _ in range(MAX_TOOL_LOOPS):
        try:
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                tools=_TOOLS,
                tool_choice="auto",
                max_tokens=120,
                temperature=0.5,
                parallel_tool_calls=True,
            )
        except Exception as e:
            # API outage (e.g. OpenAI quota/429) — degrade gracefully instead of 500.
            print(f"[recommender] LLM error: {e}")
            final_text = "Sorry, I'm having a brief hiccup — could you say that again in a moment?"
            break

        choice = response.choices[0]

        if choice.finish_reason == "stop":
            final_text = choice.message.content or ""
            break

        if choice.finish_reason == "tool_calls":
            messages.append(choice.message)
            for tc in choice.message.tool_calls:
                fn   = tc.function.name
                args = json.loads(tc.function.arguments)
                result, url = _dispatch(fn, args, restaurant_id, menu_text, session_id, db)
                if url:
                    checkout_url = url
                if fn == "complete_order":
                    complete_order_called = True
                print(f"[tool] {fn}({args}) → {str(result)[:80]}")
                messages.append({
                    "role":         "tool",
                    "tool_call_id": tc.id,
                    "content":      result,
                })
        else:
            final_text = (choice.message.content or "").strip()
            break

    # ── Hallucination guard: GPT says "I'll place your order" but didn't actually
    # call complete_order. We saw this fire twice in one demo call → caller hung
    # up because nothing happened. If the model "narrates" placing/finalizing,
    # FORCE the tool call so the order really gets saved + SMSed.
    PLACE_PATTERNS = (
        "i'll place", "i will place", "placing your order", "placing the order",
        "i'll finalize", "i will finalize", "finalizing your order",
        "i'll submit", "i will submit", "submitting your order",
        "i'll complete", "i will complete", "completing your order",
        "order placed", "order is placed", "your order is being placed",
    )
    ft_lower = (final_text or "").lower()
    if not complete_order_called and any(p in ft_lower for p in PLACE_PATTERNS):
        print(f"[recommender] hallucination guard: GPT said it placed the order without calling complete_order — forcing it now")
        forced_text, forced_url = _dispatch("complete_order", {}, restaurant_id, menu_text, session_id, db)
        if forced_url:
            checkout_url = forced_url
        complete_order_called = True
        # Replace the narration with the real confirmation that includes the link
        final_text = forced_text

    latency_ms = round((time.time() - t0) * 1000, 1)

    # Guard: GPT sometimes returns empty content after tool calls (hit loop limit or confused)
    if not final_text or not final_text.strip():
        final_text = "Sorry, I couldn't find that. What else can I get for you?"

    text = clean_for_speech(final_text)

    # Word cap — phone callers can't process long responses. 18 words ≈ 3 sec audio.
    limit = 18
    words = text.split()
    if len(words) > limit:
        chunk = " ".join(words[:limit])
        for punct in ".!?":
            idx = chunk.rfind(punct)
            if idx > 10:
                text = chunk[:idx + 1]
                break
        else:
            text = chunk + "."

    noise = len(text) < 10
    print(f"[recommender] {latency_ms}ms → {text[:80]}")
    out = {"recommendation": text, "claude_latency_ms": latency_ms, "noise_flag": noise}
    if checkout_url:
        out["checkout_url"] = checkout_url
        # Expose total + cart line items so SMS can format inline + browser UI
        # can render a receipt without an extra fetch.
        try:
            _cart_for_out = cart_svc.get(session_id)
            if _cart_for_out:
                out["total"]      = f"{_cart_for_out.total():.2f}"
                out["cart_items"] = _cart_for_out.to_dict()["items"]
        except Exception:
            pass

    # Feature image — the dish being ordered/configured this turn, so the kiosk
    # can show an appetizing photo alongside the spoken reply.
    try:
        _c = cart_svc.get(session_id)
        if _c and _c.current_item_name:
            fimg = menu_cache.image_for(restaurant_id, _c.current_item_name)
            if fimg:
                out["feature_image"] = fimg
    except Exception:
        pass

    # Options carousel — items the agent NAMED in its reply (e.g. "Miller Lite
    # and Bud Light") so the kiosk can show 2-3 photo cards to preview choices.
    try:
        opts = menu_cache.items_mentioned(restaurant_id, text, 3)
        if opts:
            out["options"] = opts
    except Exception:
        pass
    return out
