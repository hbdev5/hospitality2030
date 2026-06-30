"""
Admin operations — gated by the admin key (smstest_key).

  GET  /admin/authorize-number?slug=&key=   confirmation page (NO purchase)
  POST /admin/authorize-number              actually buy the requested number

Two-step on purpose: a GET can be prefetched by email clients, so the GET only
shows a confirm button; the POST (a deliberate click) does the billable buy.
"""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.config import get_settings
from app.database import SessionLocal, Restaurant, Owner
from app.services import provisioning as prov
from app.services import onboarding_email as ob_email

router   = APIRouter()
settings = get_settings()

_PAGE = "<div style='font-family:-apple-system,sans-serif;max-width:460px;margin:48px auto;color:#1f2430'>{body}</div>"


def _admin_ok(key):
    expected = settings.smstest_key
    return (not expected) or (key == expected)


@router.get("/admin/authorize-number", response_class=HTMLResponse)
def authorize_page(request: Request, slug: str = "", key: str = ""):
    if not _admin_ok(key):
        return HTMLResponse(_PAGE.format(body="<h3>Not authorized.</h3>"), status_code=403)
    db = SessionLocal()
    try:
        r = db.query(Restaurant).filter(Restaurant.slug == slug).first()
        if not r:
            return HTMLResponse(_PAGE.format(body="<h3>Store not found.</h3>"), status_code=404)
        num, name, cur = r.requested_number, r.name, r.plivo_number
    finally:
        db.close()
    if cur and not num:
        return HTMLResponse(_PAGE.format(body=f"<h2>Already live</h2><p><b>{name}</b> already has <b>{cur}</b>.</p>"))
    if not num:
        return HTMLResponse(_PAGE.format(body=f"<h2>Nothing pending</h2><p>No number request for <b>{name}</b>.</p>"))
    body = (f"<h2>Authorize number</h2>"
            f"<p><b>{name}</b> requested <b>+{num}</b>. Authorizing will <b>purchase</b> it on Plivo "
            f"(~$0.50/mo) and set it as the store's voice line.</p>"
            f"<form method='post' action='{settings.base_path}/admin/authorize-number'>"
            f"<input type='hidden' name='slug' value='{slug}'>"
            f"<input type='hidden' name='key' value='{key}'>"
            f"<button style='background:#16a34a;color:#fff;border:none;border-radius:9px;padding:13px 22px;"
            f"font-size:15px;font-weight:600;cursor:pointer'>Authorize &amp; buy +{num}</button></form>")
    return HTMLResponse(_PAGE.format(body=body))


@router.post("/admin/authorize-number", response_class=HTMLResponse)
async def authorize_buy(request: Request):
    form = await request.form()
    slug, key = form.get("slug", ""), form.get("key", "")
    if not _admin_ok(key):
        return HTMLResponse(_PAGE.format(body="<h3>Not authorized.</h3>"), status_code=403)
    db = SessionLocal()
    try:
        r = db.query(Restaurant).filter(Restaurant.slug == slug).first()
        if not r or not r.requested_number:
            return HTMLResponse(_PAGE.format(body="<h3>Nothing to authorize.</h3>"), status_code=400)
        rid, num, name = r.id, r.requested_number, r.name
        owner = db.query(Owner).filter(Owner.id == r.owner_id).first()
        owner_email = owner.email if owner else None
    finally:
        db.close()

    res = prov.provision_number(rid, num)   # buys + voice app + assigns plivo_number
    if not res.get("ok"):
        return HTMLResponse(_PAGE.format(body=f"<h3>Purchase failed.</h3><p>{res.get('error')}</p>"), status_code=400)

    db = SessionLocal()
    try:
        r = db.query(Restaurant).filter(Restaurant.id == rid).first()
        r.requested_number = None
        db.commit()
    finally:
        db.close()

    if owner_email:
        ob_email.send_number_live(owner_email, name, res["number"], sms=res.get("sms", False))
    sms_line = ("voice <b>+ compliant SMS</b> (linked to 10DLC campaign)"
                if res.get("sms") else f"voice (SMS link pending: {res.get('sms_info','')})")
    return HTMLResponse(_PAGE.format(body=(
        f"<h2>✅ Number live</h2><p><b>+{res['number']}</b> is now {name}'s line — {sms_line}, "
        f"routed to its AI agent. The operator has been emailed.</p>")))
