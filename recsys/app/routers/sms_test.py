"""
Dev-only SMS test console — NOT linked from any operator UI.

  GET  /smsTest?key=…             dev page: pick a Plivo number, send, watch replies
  GET  /api/smsTest/numbers       your rented Plivo numbers (for the From dropdown)
  POST /api/smsTest/send          send an SMS via Plivo, logged
  GET  /api/smsTest/messages      recent thread (outbound + inbound)
  POST /api/smsTest/inbound       Plivo inbound webhook — captures replies

Gated by settings.smstest_key (?key=…). Point a test number's Message webhook at
…/smsTest/inbound to capture replies. The inbound webhook is intentionally
ungated so Plivo can call it.
"""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.database import SessionLocal, SmsTestMessage
from app.config import get_settings
from app.paths import home

router    = APIRouter()
settings  = get_settings()
templates = Jinja2Templates(directory=home("templates"))


def _authed(key: str) -> bool:
    expected = settings.smstest_key
    return (not expected) or (key == expected)


def _unauth():
    return JSONResponse({"error": "unauthorized"}, status_code=403)


def _log(direction, frm, to, body, status, uuid=None):
    if isinstance(uuid, list):
        uuid = uuid[0] if uuid else None
    db = SessionLocal()
    try:
        db.add(SmsTestMessage(direction=direction, from_number=frm, to_number=to,
                              body=body, status=status, message_uuid=uuid))
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"[smsTest] log err: {e}")
    finally:
        db.close()


@router.get("/smsTest", response_class=HTMLResponse)
def sms_test_page(request: Request, key: str = ""):
    if not _authed(key):
        return HTMLResponse("<h3>Not authorized</h3><p>Append <code>?key=YOUR_KEY</code> to the URL.</p>",
                            status_code=403)
    return templates.TemplateResponse(request=request, name="sms_test.html", context={
        "base": settings.base_path, "key": key, "default_from": settings.plivo_number})


@router.get("/api/smsTest/numbers")
def sms_test_numbers(key: str = ""):
    if not _authed(key):
        return _unauth()
    nums = []
    try:
        import plivo
        client = plivo.RestClient(settings.plivo_auth_id, settings.plivo_auth_token)
        resp = client.numbers.list(limit=20)
        items = getattr(resp, "objects", None) or resp
        for n in items:
            num = getattr(n, "number", None) or (n.get("number") if isinstance(n, dict) else None)
            if num:
                nums.append(num)
    except Exception as e:
        print(f"[smsTest] list numbers err: {e}")
    if settings.plivo_number and settings.plivo_number not in nums:
        nums.insert(0, settings.plivo_number)
    return {"numbers": nums}


@router.post("/api/smsTest/send")
async def sms_test_send(request: Request, key: str = ""):
    if not _authed(key):
        return _unauth()
    b = await request.json()
    src  = (b.get("from") or settings.plivo_number or "").strip()
    dst  = (b.get("to") or "").strip()
    text = (b.get("text") or "").strip()
    if not (src and dst and text):
        return {"ok": False, "error": "from, to and message are all required."}

    status, uuid, err = "sent", None, None
    try:
        import plivo
        client = plivo.RestClient(settings.plivo_auth_id, settings.plivo_auth_token)
        r = client.messages.create(src=src, dst=dst, text=text)
        uuid = getattr(r, "message_uuid", None)
        if isinstance(uuid, list):
            uuid = uuid[0] if uuid else None
    except Exception as e:
        status, err = "failed", str(e)
    _log("out", src, dst, text, status, uuid)
    return {"ok": status == "sent", "status": status, "error": err, "message_uuid": uuid}


@router.post("/api/smsTest/inbound")
async def sms_test_inbound(request: Request):
    form = await request.form()
    body = form.get("Text", "")
    frm  = form.get("From", "")
    to   = form.get("To", "")
    _log("in", frm, to, body, "received", form.get("MessageUUID"))
    print(f"[smsTest] inbound from={frm} to={to}: {body[:60]!r}")
    return PlainTextResponse("", media_type="application/xml")


@router.get("/api/smsTest/messages")
def sms_test_messages(key: str = "", limit: int = 60):
    if not _authed(key):
        return _unauth()
    db = SessionLocal()
    try:
        rows = (db.query(SmsTestMessage)
                  .order_by(SmsTestMessage.created_at.desc())
                  .limit(min(limit, 200)).all())
        return {"messages": [{
            "id": m.id, "direction": m.direction, "from": m.from_number or "",
            "to": m.to_number or "", "body": m.body or "", "status": m.status or "",
            "uuid": m.message_uuid or "",
            "time": m.created_at.strftime("%H:%M:%S") if m.created_at else "",
        } for m in rows]}
    finally:
        db.close()
