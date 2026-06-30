"""
Browser Text-to-Order channel.

Same idea as the SMS path (plivo_hooks._process_sms_background) but text-in /
text-out over HTTP, for the restaurant owner to test from the AI Studio UI.
Routes through the SAME unified pipeline: VIP keyword fast-path, structured
menu, modifier fast-path, per-session cart + history, complete_order.

  POST /api/text/chat   {message, session_id}  →  {reply, cart_items, cart_total, checkout_url?}
"""

import time
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.database import SessionLocal, Restaurant, Menu
from app.services.recommender import get_recommendation
from app.services import cart as cart_svc
from app.services import vip as vip_svc
from app.services import skills as skills_svc
from app.services.convo_log import log_turn

router = APIRouter()

# Per-session conversation history, mirroring voice_web / SMS.
_history: dict = {}   # session_id → list[{role,content}]


def _load_menu():
    db = SessionLocal()
    try:
        rest = db.query(Restaurant).first()
        if not rest:
            return None, None
        menu = (db.query(Menu)
                  .filter(Menu.restaurant_id == rest.id)
                  .order_by(Menu.id.desc()).first())
        return rest, (menu.raw_text if menu else None)
    finally:
        db.close()


@router.post("/api/text/chat")
async def text_chat(request: Request):
    t_start = time.time()
    body       = await request.json()
    message    = (body.get("message") or "").strip()
    session_id = (body.get("session_id") or "text-default").strip()

    if not message:
        return JSONResponse({"reply": "Type a message to get started.", "cart_items": [], "cart_total": "0.00"})

    rest, menu_text = _load_menu()
    rid = rest.id if rest else 1

    checkout_url = None
    claude_ms    = 0.0
    noise        = False

    # Mid-collection follow-up (e.g. raffle name+email) takes top priority.
    pending = skills_svc.pending_reply(message, restaurant_id=rid, session_id=session_id, caller=None, channel="text")
    # VIP keyword fast-path (headline demo flow) — deterministic, no LLM.
    vip_reply = None if pending else vip_svc.keyword_intent(message, session_id, identifier=session_id, restaurant_id=rid)
    # Operator-taught skills (self-learning) — run before the LLM, like VIP.
    skill_reply = None if (pending or vip_reply) else skills_svc.match(
        message, restaurant_id=rid, session_id=session_id, caller=None, channel="text")
    if pending:
        reply = pending
    elif vip_reply:
        reply = vip_reply
    elif skill_reply:
        reply = skill_reply
    elif not menu_text:
        reply = "Our menu isn't available right now. Please try again soon."
    else:
        hist = _history.setdefault(session_id, [])
        result = get_recommendation(
            menu_text, message,
            state         = "browsing",
            language      = "en",
            restaurant_id = rid,
            session_id    = session_id,
            history       = hist,
        )
        reply        = result["recommendation"]
        checkout_url = result.get("checkout_url")
        claude_ms    = result.get("claude_latency_ms", 0.0)
        noise        = result.get("noise_flag", False)
        hist.append({"role": "user",      "content": message})
        hist.append({"role": "assistant", "content": reply})
        if len(hist) > 12:
            _history[session_id] = hist[-12:]

    # Live cart snapshot so the UI can render a receipt (parity with voice_web).
    cart_items, cart_total = [], "0.00"
    try:
        c = cart_svc.get(session_id)
        if c and c.items:
            cart_items = c.to_dict()["items"]
            cart_total = f"{c.total():.2f}"
    except Exception as e:
        print(f"[text] cart snapshot err: {e}")

    total_ms = round((time.time() - t_start) * 1000)
    print(f"[text] {total_ms}ms | {message!r} -> {reply[:60]!r}")

    # Persist the turn so the operator console sees browser-text conversations too.
    log_turn("text", message, reply,
             restaurant_id=rid, session_id=session_id,
             claude_latency_ms=claude_ms, total_latency_ms=total_ms, noise_flag=noise)

    resp = {"reply": reply, "cart_items": cart_items, "cart_total": cart_total, "ms": total_ms}
    if checkout_url:
        resp["checkout_url"] = checkout_url
    return JSONResponse(resp)
