"""
VIP membership service — ported from the Java VIP flow.

Java reference (handover src):
  - BrowserBotTest.subscribe_vip       → pitch + subscription link
  - BrowserBotTest per-visit benefit    → applied on every order (lines ~8565)
  - StripeVipWebhook                     → persists subscriber + delivers card
  - VipSubscription.java                 → semantic field names over PromotionDetails

Payment rail here is PayPal (recsys reuses its existing PayPal sandbox rather
than Stripe Connect). The agent hands out a link to /vip/subscribe/{session};
that page renders PayPal subscription buttons and, on approval, calls back to
persist a VipSubscriber. Per-visit benefits are then auto-applied at checkout.
"""

import re
from typing import Optional
from app.database import SessionLocal, VipProgram, VipSubscriber
from app.config import get_settings

settings = get_settings()

# Words that should deterministically trigger the VIP pitch (the user's headline
# flow: "when consumer texts VIP"). Matched as whole words, case-insensitive.
_VIP_KEYWORDS = ("vip", "membership", "member", "loyalty",
                 "subscribe", "subscription", "perks")


def discount_applies_today(condition: str) -> bool:
    """Is the per-visit discount valid today, given its condition?
    Uses US/Pacific (typical restaurant tz) for the weekday determination."""
    cond = (condition or "always").lower()
    if any(w in cond for w in ("always", "everyday", "every day", "all", "any")):
        return True
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        wd = datetime.now(ZoneInfo("America/Los_Angeles")).weekday()  # Mon=0..Sun=6
    except Exception:
        from datetime import datetime
        wd = datetime.now().weekday()
    is_weekend = wd >= 5
    if "weekday" in cond:
        return not is_weekend
    if "weekend" in cond:
        return is_weekend
    return True


def get_program(restaurant_id: int = 1, db=None) -> Optional[VipProgram]:
    """Active VIP program for a restaurant, or None."""
    owns = db is None
    db = db or SessionLocal()
    try:
        return (db.query(VipProgram)
                  .filter(VipProgram.restaurant_id == restaurant_id,
                          VipProgram.is_active == True)
                  .order_by(VipProgram.id.desc())
                  .first())
    finally:
        if owns:
            db.close()


def monthly_fee_str(program: VipProgram) -> str:
    cents = program.monthly_fee_cents or 0
    dollars = cents / 100.0
    return f"${int(dollars)}" if dollars == int(dollars) else f"${dollars:.2f}"


def pitch(program: VipProgram) -> str:
    """The spoken/written sales pitch. Mirrors the user's example wording:
    'we have a VIP membership plan which is $5/month for free drinks or chips
    every visit.'"""
    fee = monthly_fee_str(program)
    benefit = (program.recurring_benefit or "exclusive member perks").strip()
    extra = ""
    if program.visit_discount_percent and program.visit_discount_percent > 0:
        extra = f" plus {int(program.visit_discount_percent)}% off every visit"
    return f"We have a {program.program_name} membership — {fee}/month for {benefit.lower()}{extra}."


def subscribe_link(session_id: str) -> str:
    return f"{settings.public_base_url}/vip/subscribe/{session_id}"


def is_subscriber(identifier: str, restaurant_id: int = 1, db=None) -> Optional[VipSubscriber]:
    """Active subscriber for the given identifier (phone or session id), or None."""
    if not identifier:
        return None
    owns = db is None
    db = db or SessionLocal()
    try:
        return (db.query(VipSubscriber)
                  .filter(VipSubscriber.restaurant_id == restaurant_id,
                          VipSubscriber.identifier == identifier,
                          VipSubscriber.status == 'active')
                  .order_by(VipSubscriber.id.desc())
                  .first())
    finally:
        if owns:
            db.close()


def keyword_intent(text: str, session_id: str, identifier: str = None,
                   restaurant_id: int = 1) -> Optional[str]:
    """Deterministic VIP route shared by SMS + browser Text. Returns the pitch +
    signup link (or an 'already a member' note) if the message is a VIP keyword,
    else None so the caller falls through to the normal pipeline."""
    t = (text or "").lower()
    if not any(re.search(rf"\b{re.escape(k)}\b", t) for k in _VIP_KEYWORDS):
        return None
    program = get_program(restaurant_id)
    if not program:
        return None
    ident = identifier or session_id
    if is_subscriber(ident, restaurant_id):
        return (f"You're already a {program.program_name} member — your "
                f"{(program.recurring_benefit or 'benefits').lower()} applies automatically every visit.")
    return f"{pitch(program)} Join here: {subscribe_link(session_id)}"


def mark_subscriber(identifier: str, restaurant_id: int = 1,
                    phone: str = None, email: str = None,
                    paypal_subscription_id: str = None, db=None) -> VipSubscriber:
    """Create or refresh an active subscriber record. Idempotent on identifier."""
    owns = db is None
    db = db or SessionLocal()
    try:
        sub = (db.query(VipSubscriber)
                 .filter(VipSubscriber.restaurant_id == restaurant_id,
                         VipSubscriber.identifier == identifier)
                 .first())
        if sub is None:
            sub = VipSubscriber(restaurant_id=restaurant_id, identifier=identifier)
            db.add(sub)
        sub.status = 'active'
        if phone:                  sub.phone = phone
        if email:                  sub.email = email
        if paypal_subscription_id: sub.paypal_subscription_id = paypal_subscription_id
        db.commit()
        db.refresh(sub)
        return sub
    finally:
        if owns:
            db.close()
