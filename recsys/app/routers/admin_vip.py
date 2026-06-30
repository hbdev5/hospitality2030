"""
Admin VIP setup — merchant configures the plan + card look & feel.

Studio "Admin" mode talks to these endpoints; "User" mode uses /api/vip/config
to pop the PayPal subscription buttons.

  GET  /api/vip/config            → current plan + card + paypal plan_id/client_id
  POST /api/admin/vip/setup       → natural-language plan setup (LLM parse) + render card
  POST /api/admin/vip/card        → visual-editor field updates + render card
  POST /api/admin/vip/logo        → logo upload, store under /static, re-render card
"""

import os, json, time
from fastapi import APIRouter, Request, UploadFile, File, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from openai import OpenAI

from app.database import get_db, VipProgram
from app.config   import get_settings
from app.paths    import home
from app.services import vip as vip_svc
from app.services import vip_card
from app.services import paypal as paypal_svc

router   = APIRouter()
settings = get_settings()

_STATIC_DIR = home("static")
_LOGO_DIR   = os.path.join(_STATIC_DIR, "vip_logos")

_THEME_HEX = {
    "gold": "#C5A03C", "emerald": "#1F7A4D", "green": "#1F7A4D",
    "royal": "#2E4B8F", "blue": "#2E4BB0", "navy": "#1B2A5B",
    "crimson": "#9E2A2B", "red": "#9E2A2B", "purple": "#5B2A86",
    "black": "#C5A03C", "silver": "#9AA1A8",
}


def _config_dict(p: VipProgram) -> dict:
    return {
        "program_name":           p.program_name,
        "monthly_fee_cents":      p.monthly_fee_cents,
        "fee":                    vip_svc.monthly_fee_str(p),
        "recurring_benefit":      p.recurring_benefit,
        "benefit2":               p.benefit2 or "",
        "benefit3":               p.benefit3 or "",
        "visit_discount_percent": p.visit_discount_percent or 0,
        "card_title":             p.card_title or p.program_name,
        "card_subtitle":          p.card_subtitle or "Something new for every visit",
        "accent_color":           p.accent_color or "#C5A03C",
        "bg_color":               p.bg_color or "#121212",
        "logo_url":               p.logo_url or "",
        "card_website":           p.card_website or "",
        "card_url":               p.card_url or "",
    }


def _render(p: VipProgram, db: Session) -> str:
    url = vip_card.render(p, key=f"r{p.restaurant_id}-preview")
    p.card_url = url
    db.commit()
    # cache-bust so the browser reloads the regenerated image
    return f"{url}?t={int(time.time())}"


def _ensure_program(db: Session) -> VipProgram:
    p = vip_svc.get_program(1, db=db)
    if p is None:
        p = VipProgram(restaurant_id=1)
        db.add(p)
        db.commit()
        db.refresh(p)
    return p


@router.get("/api/vip/config")
async def vip_config(db: Session = Depends(get_db)):
    p = _ensure_program(db)
    cfg = _config_dict(p)
    if not p.card_url:
        cfg["card_url"] = _render(p, db)
    # plan_id for consumer-mode PayPal buttons (best-effort)
    plan_id = ""
    try:
        plan_id = paypal_svc.ensure_subscription_plan(p, db)
    except Exception as e:
        print(f"[admin_vip] plan ensure failed: {e}")
    cfg["plan_id"]          = plan_id
    cfg["paypal_client_id"] = settings.paypal_client_id
    cfg["join_url"]         = f"{settings.public_base_url}/vip/join"
    cfg["preview_url"]      = f"{settings.public_base_url}/vip/preview"
    return JSONResponse(cfg)


def _parse_plan(message: str) -> dict:
    """Use gpt-4o-mini to extract structured plan fields from the operator's
    natural-language setup sentence."""
    client = OpenAI(api_key=settings.openai_api_key)
    sys = (
        "Extract the VIP membership plan from the merchant's instruction. "
        "Capture each benefit EXACTLY as the merchant stated it — VERBATIM. Keep their "
        "exact words and conditions (e.g. keep 'on weekdays'; NEVER change it to "
        "'every visit', never reword or generalize, never change numbers). "
        "Return STRICT JSON: {\"program_name\": string (default 'VIP'), "
        "\"monthly_fee_cents\": integer (dollars*100, 0 if not stated), "
        "\"benefits\": [up to 3 benefit strings, each VERBATIM as said, in order], "
        "\"visit_discount_percent\": number (the % off ONLY if a benefit is a percentage "
        "discount, else 0), \"discount_condition\": one of 'always','weekdays','weekends' "
        "(based on when the discount applies — 'weekdays' if they say weekdays/Mon-Fri, "
        "'weekends' for Sat-Sun, else 'always'), "
        "\"accent_color\": hex like '#1F7A4D' ONLY if a color/theme is named, else null}. "
        "Do NOT invent benefits not stated; do NOT alter wording."
    )
    try:
        r = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": sys},
                      {"role": "user", "content": message}],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=200,
        )
        data = json.loads(r.choices[0].message.content)
    except Exception as e:
        print(f"[admin_vip] parse error: {e}")
        data = {}
    # Map any named theme word to hex if the model didn't return one
    if not data.get("accent_color"):
        for word, hx in _THEME_HEX.items():
            if word in message.lower():
                data["accent_color"] = hx
                break
    # Derive discount condition from keywords if the model didn't classify it.
    ml = message.lower()
    if not data.get("discount_condition"):
        data["discount_condition"] = ("weekdays" if "weekday" in ml
                                      else "weekends" if "weekend" in ml else "always")
    return data


@router.post("/api/admin/vip/setup")
async def vip_setup(request: Request, db: Session = Depends(get_db)):
    body    = await request.json()
    message = (body.get("message") or "").strip()
    if not message:
        return JSONResponse({"error": "empty message"}, status_code=400)

    ml = message.lower()

    # Marketing command — open the weekly campaign studio.
    if any(w in ml for w in ("marketing", "campaign", "email blast", "weekly email", "promotion email")):
        srm_url = f"{settings.public_base_url}/srm"
        return JSONResponse({"reply": ("Open your Marketing Studio to generate & approve this "
                                       f"week's menu-driven VIP campaign: {srm_url}")})

    # Preview command — operator just wants the shareable test link, not a plan edit.
    preview_url = f"{settings.public_base_url}/vip/preview"
    is_preview = ("preview" in ml) or ("card" in ml and any(
        w in ml for w in ("show", "see", "view", "test", "look")))
    if is_preview:
        p = _ensure_program(db)
        return JSONResponse({
            "reply": ("Here's your shareable card preview — open it to test the look "
                      f"& feel before going live: {preview_url}"),
            "card_url": _render(p, db),
            "config":   _config_dict(p),
            "preview_url": preview_url,
        })

    p = _ensure_program(db)
    parsed = _parse_plan(message)

    if parsed.get("program_name"):       p.program_name      = str(parsed["program_name"])[:64]
    if parsed.get("monthly_fee_cents"):  p.monthly_fee_cents = int(parsed["monthly_fee_cents"])

    # Benefits captured VERBATIM and replace ALL card lines (so the preview matches
    # exactly what the operator said — including conditions like 'on weekdays').
    benefits = [str(b).strip() for b in (parsed.get("benefits") or []) if str(b).strip()]
    if benefits:
        p.recurring_benefit = benefits[0][:255]
        p.benefit2          = benefits[1][:255] if len(benefits) > 1 else ""
        p.benefit3          = benefits[2][:255] if len(benefits) > 2 else ""

    if parsed.get("visit_discount_percent") is not None:
        try: p.visit_discount_percent = float(parsed["visit_discount_percent"])
        except Exception: pass
    if parsed.get("discount_condition") in ("always", "weekdays", "weekends"):
        p.discount_condition = parsed["discount_condition"]
    if parsed.get("accent_color"):       p.accent_color      = str(parsed["accent_color"])[:9]
    if not p.card_title:                 p.card_title        = "Oak & Ivy"
    p.is_active = True
    # Fee changed → invalidate cached PayPal plan so it's recreated at new price.
    if parsed.get("monthly_fee_cents"):
        p.paypal_plan_id = None
    db.commit()

    card_url = _render(p, db)
    join_url = f"{settings.public_base_url}/vip/join"
    benefit_lines = [b for b in (p.recurring_benefit, p.benefit2, p.benefit3) if b]
    # Echo the benefits VERBATIM so the operator can confirm exactly what was saved.
    summary = (f"Saved ✓ — {p.program_name}, {vip_svc.monthly_fee_str(p)}/month.\n"
               "Benefits (as you stated them):\n"
               + "\n".join(f"  • {b}" for b in benefit_lines)
               + f"\n\nPreview the card: {preview_url}\n"
               + f"Customer signup link: {join_url}")
    return JSONResponse({"reply": summary, "card_url": card_url,
                         "config": _config_dict(p), "join_url": join_url, "preview_url": preview_url})


@router.post("/api/admin/vip/card")
async def vip_card_update(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    p = _ensure_program(db)
    field_map = {
        "card_title": str, "card_subtitle": str, "recurring_benefit": str,
        "benefit2": str, "benefit3": str, "accent_color": str, "bg_color": str,
        "program_name": str, "card_website": str,
    }
    for k, cast in field_map.items():
        if k in body and body[k] is not None:
            setattr(p, k, cast(body[k])[:255])
    if "monthly_fee_cents" in body and body["monthly_fee_cents"] is not None:
        try:
            p.monthly_fee_cents = int(body["monthly_fee_cents"]); p.paypal_plan_id = None
        except Exception: pass
    if "visit_discount_percent" in body and body["visit_discount_percent"] is not None:
        try: p.visit_discount_percent = float(body["visit_discount_percent"])
        except Exception: pass
    db.commit()
    return JSONResponse({"card_url": _render(p, db), "config": _config_dict(p)})


@router.post("/api/admin/vip/logo")
async def vip_logo(file: UploadFile = File(...), db: Session = Depends(get_db)):
    os.makedirs(_LOGO_DIR, exist_ok=True)
    p = _ensure_program(db)
    ext = os.path.splitext(file.filename or "")[1].lower() or ".png"
    if ext not in (".png", ".jpg", ".jpeg", ".webp"):
        ext = ".png"
    fname = f"r{p.restaurant_id}-logo{ext}"
    path  = os.path.join(_LOGO_DIR, fname)
    with open(path, "wb") as f:
        f.write(await file.read())
    p.logo_url = f"{settings.public_base_url}/static/vip_logos/{fname}"
    db.commit()
    return JSONResponse({"logo_url": p.logo_url, "card_url": _render(p, db)})
