"""
VIP membership signup (PayPal subscription).

Java reference: BrowserBotTest.subscribe_vip handed out a Stripe subscription
link; StripeVipWebhook persisted the subscriber + SMSed a member card. Here the
agent hands out a link to GET /vip/subscribe/{session}; that page renders PayPal
subscription buttons against a cached monthly plan. On approval the browser posts
the subscription id back and we persist a VipSubscriber + SMS a confirmation.

Routes:
  GET  /vip/subscribe/{session}        → signup page (PayPal subscription buttons)
  POST /api/vip/subscribe/capture      → verify + persist subscriber, SMS confirm
"""

import os, secrets
from datetime import datetime, date
from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db, VipSubscriber
from app.config   import get_settings
from app.paths    import home
from app.services import vip as vip_svc
from app.services import paypal as paypal_svc

router    = APIRouter()
settings  = get_settings()
templates = Jinja2Templates(directory=home("templates"))


def _identifier_for_session(session: str) -> str:
    """Map a session id to the per-visit benefit identifier used at checkout.
    SMS/voice sessions are keyed by the caller's phone; everything else by the
    raw session id (matches recommender: identifier = caller_phone or session_id)."""
    if session.startswith("sms-"):
        return session[len("sms-"):]
    return session


def _member_key(identifier: str) -> str:
    """Stable card filename key for a subscriber identifier."""
    return "member-" + "".join(c for c in (identifier or "") if c.isalnum())[:24]


def _validity(created_at) -> tuple:
    """(from, to) as mm/yyyy — paid period is signup month → +12 months."""
    from datetime import date, datetime
    d = created_at or datetime.utcnow()
    return f"{d.month:02d}/{d.year}", f"{d.month:02d}/{d.year + 1}"


def _qr_data_uri(data: str) -> str:
    try:
        import qrcode, io, base64
        buf = io.BytesIO()
        qrcode.make(data).save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        print(f"[vip] qr error: {e}")
        return ""


def _split_benefit(b: str) -> tuple:
    for sep in (" with ", " on ", " for ", " every "):
        i = b.lower().find(sep)
        if i > 0:
            return b[:i].strip(), b[i:].strip()
    return b, ""


def _card_benefits(program) -> list:
    raw = [b for b in (program.recurring_benefit if program else None,
                       program.benefit2 if program else None,
                       program.benefit3 if program else None) if b]
    icons = ["drink", "star", "cloche"]
    out = []
    for i, b in enumerate(raw[:3]):
        head, sub = _split_benefit(b)
        out.append({"icon": icons[i] if i < len(icons) else "cloche", "head": head, "sub": sub})
    return out


def _card_ctx(program, member_name, member_id, valid_from, valid_to, qr_url, mode, **flags):
    ctx = {
        "base":        settings.base_path,
        "title":       (program.card_title if program else "Oak & Ivy") or "Oak & Ivy",
        "tagline":     (program.card_subtitle if program else "") or "Premium Steaks. Memorable Moments.",
        "member_name": member_name,
        "member_id":   member_id,
        "valid_from":  valid_from,
        "valid_to":    valid_to,
        "benefits":    _card_benefits(program),
        "website":     (program.card_website if program else "") or "",
        "qr":          _qr_data_uri(qr_url),
        "mode":        mode,
        "active": False, "found": False, "can_redeem": False, "redeemed_today": False,
    }
    ctx.update(flags)
    return ctx


@router.get("/vip/subscribe/{session}", response_class=HTMLResponse)
async def vip_subscribe_page(session: str, request: Request, db: Session = Depends(get_db)):
    program = vip_svc.get_program(1, db=db)
    plan_id = None
    error   = None
    if program:
        try:
            plan_id = paypal_svc.ensure_subscription_plan(program, db)
        except Exception as e:
            error = f"Payment setup unavailable: {e}"
            print(f"[vip] ensure_subscription_plan failed: {e}")

    identifier = _identifier_for_session(session)
    already    = vip_svc.is_subscriber(identifier, 1, db=db) is not None

    return templates.TemplateResponse(
        request=request,
        name="vip_subscribe.html",
        context={
            "base":             settings.base_path,
            "session":          session,
            "identifier":       identifier,
            "program_name":     program.program_name if program else "VIP",
            "fee":              vip_svc.monthly_fee_str(program) if program else "$5",
            "benefit":          (program.recurring_benefit if program else "Free drink or chips every visit"),
            "discount":         (int(program.visit_discount_percent) if program and program.visit_discount_percent else 0),
            "plan_id":          plan_id or "",
            "paypal_client_id": settings.paypal_client_id,
            "already":          already,
            "error":            error,
        },
    )


@router.post("/api/vip/subscribe/capture")
async def vip_capture(request: Request, db: Session = Depends(get_db)):
    body            = await request.json()
    subscription_id = (body.get("subscription_id") or "").strip()
    session         = (body.get("session") or "").strip()
    email           = (body.get("email") or "").strip() or None
    if not subscription_id or not session:
        return JSONResponse({"status": "error", "detail": "missing subscription_id or session"}, status_code=400)

    # Verify with PayPal that the subscription is real + active/approved.
    status = None
    try:
        sub    = paypal_svc.get_subscription(subscription_id)
        status = sub.get("status")
        if not email:
            email = (sub.get("subscriber", {}) or {}).get("email_address")
    except Exception as e:
        print(f"[vip] subscription verify failed: {e}")
        return JSONResponse({"status": "error", "detail": "could not verify subscription"}, status_code=502)

    if status not in ("ACTIVE", "APPROVED", "APPROVAL_PENDING"):
        return JSONResponse({"status": "not_active", "paypal_status": status}, status_code=400)

    identifier = _identifier_for_session(session)
    phone      = identifier if session.startswith("sms-") else None
    vip_svc.mark_subscriber(identifier, 1, phone=phone, email=email,
                            paypal_subscription_id=subscription_id, db=db)
    print(f"[vip] subscriber active: {identifier} sub={subscription_id} status={status}")

    # Generate the personalised member card (Java VipMemberCardGenerator parity):
    # member name + phone QR + validity. Best-effort.
    program   = vip_svc.get_program(1, db=db)
    # Prefer the PayPal account holder's name, fall back to the email local part.
    member_nm = None
    try:
        nm = (sub.get("subscriber", {}) or {}).get("name", {}) or {}
        full = " ".join(p for p in (nm.get("given_name"), nm.get("surname")) if p).strip()
        member_nm = full or None
    except Exception:
        pass
    if not member_nm:
        member_nm = (email.split("@")[0] if email else "VIP Member").replace(".", " ").title()

    # Validity: this month → +12 months (mm/yyyy).
    from datetime import date
    today = date.today()
    v_from = f"{today.month:02d}/{today.year}"
    v_to   = f"{today.month:02d}/{today.year + 1}"

    safe_key  = _member_key(identifier)
    card_url  = None
    try:
        from app.services import vip_card
        card_url = vip_card.render(program, member_name=member_nm,
                                   member_id=subscription_id, phone=phone, key=safe_key,
                                   validity_from=v_from, validity_to=v_to)
        print(f"[vip] member card: {card_url}")
    except Exception as e:
        print(f"[vip] member card render failed: {e}")

    verify_url = f"{settings.public_base_url}/vip/verify/{subscription_id}"

    # SMS the VERIFY link to the member (phone sessions only). The verify page
    # shows the card, benefits, and paid period — and is what staff scans.
    if phone:
        try:
            import plivo as _plivo
            benefit = program.recurring_benefit if program else "your member benefits"
            text = (f"⭐ Welcome to Curry Bliss {program.program_name if program else 'VIP'}! "
                    f"You now get {benefit.lower()}. "
                    f"View & verify your membership: {verify_url}")
            dst = phone if phone.startswith("+") else f"+{''.join(c for c in phone if c.isdigit())}"
            client = _plivo.RestClient(settings.plivo_auth_id, settings.plivo_auth_token)
            client.messages.create(src=settings.plivo_number, dst=dst, text=text[:320])
            print(f"[vip] welcome SMS sent to {dst}")
        except Exception as e:
            print(f"[vip] welcome SMS failed: {e}")

    return JSONResponse({"status": "active", "identifier": identifier,
                         "card_url": card_url, "verify_url": verify_url})


def _mask_phone(p):
    digits = "".join(c for c in (p or "") if c.isdigit())
    return f"•••• {digits[-4:]}" if len(digits) >= 4 else ""


def _mask_email(e):
    if not e or "@" not in e:
        return ""
    local, dom = e.split("@", 1)
    return (local[0] + "•••" if local else "•••") + "@" + dom


@router.get("/vip/verify/{member_id}", response_class=HTMLResponse)
async def vip_verify(member_id: str, request: Request, db: Session = Depends(get_db)):
    """Staff/member-facing verification page hosted on the VM. Confirms an active
    membership, its benefits, and the paid period."""
    program = vip_svc.get_program(1, db=db)
    sub = (db.query(VipSubscriber)
             .filter(VipSubscriber.paypal_subscription_id == member_id)
             .order_by(VipSubscriber.id.desc()).first())

    if member_id == "PREVIEW" or sub is None:
        active   = (member_id == "PREVIEW")
        v_from, v_to = ("MM / DD / YYYY", "MM / DD / YYYY")
        name     = "John Doe" if active else "Unknown"
        redeemed = False
    else:
        active   = (sub.status == "active")
        v_from, v_to = _validity(sub.created_at)
        name     = (sub.email.split('@')[0].replace('.', ' ').title() if sub.email else "VIP Member")
        redeemed = bool(sub.last_redeemed_at and sub.last_redeemed_at.date() == date.today())

    ctx = _card_ctx(program, member_name=name, member_id=member_id,
                    valid_from=v_from, valid_to=v_to,
                    qr_url=f"{settings.public_base_url}/vip/verify/{member_id}", mode="verify",
                    active=active, found=(sub is not None or member_id == "PREVIEW"),
                    can_redeem=(active and sub is not None), redeemed_today=redeemed)
    return templates.TemplateResponse(request=request, name="vip_card.html", context=ctx,
                                      headers={"Cache-Control": "no-store"})


@router.get("/vip/preview", response_class=HTMLResponse)
async def vip_card_preview(request: Request, db: Session = Depends(get_db)):
    """Standalone, shareable flip-card preview (the elegant member card)."""
    program = vip_svc.get_program(1, db=db)
    ctx = _card_ctx(program, member_name="John Doe", member_id="PREVIEW",
                    valid_from="MM / DD / YYYY", valid_to="MM / DD / YYYY",
                    qr_url=f"{settings.public_base_url}/vip/verify/PREVIEW", mode="preview")
    return templates.TemplateResponse(request=request, name="vip_card.html", context=ctx,
                                      headers={"Cache-Control": "no-store"})


@router.get("/vip/join")
async def vip_join():
    """Shareable consumer signup link — each visitor gets a fresh subscribe session."""
    return RedirectResponse(url=f"{settings.base_path}/vip/subscribe/join-{secrets.token_hex(4)}")


@router.post("/api/vip/redeem/{member_id}")
async def vip_redeem(member_id: str, db: Session = Depends(get_db)):
    """Redeem the member's per-visit benefit — allowed once per calendar day."""
    sub = (db.query(VipSubscriber)
             .filter(VipSubscriber.paypal_subscription_id == member_id)
             .order_by(VipSubscriber.id.desc()).first())
    if sub is None or sub.status != "active":
        return JSONResponse({"status": "invalid"}, status_code=404)
    if sub.last_redeemed_at and sub.last_redeemed_at.date() == date.today():
        return JSONResponse({"status": "already",
                             "redeemed_at": sub.last_redeemed_at.strftime("%I:%M %p")})
    sub.last_redeemed_at = datetime.utcnow()
    db.commit()
    print(f"[vip] redeemed benefit for {member_id}")
    return JSONResponse({"status": "redeemed"})
