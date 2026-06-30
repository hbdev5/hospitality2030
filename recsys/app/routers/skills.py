"""
Operator console API for self-learning agent skills.

  GET  /api/skills                list taught skills
  POST /api/skills/suggest        AI-draft a skill from {guest_message, note}
  POST /api/skills                create an (operator-approved) skill
  GET  /api/raffle/entries        list captured raffle entrants
  POST /api/raffle/draw           pick a random winner
"""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from app.services import skills as skills_svc
from app.services import guestbook as gb_svc
from app.services.phone_check import validate_mobile
from app.config import get_settings
from app.paths import home

router = APIRouter()
settings  = get_settings()
templates = Jinja2Templates(directory=home("templates"))
RID = 1   # demo / public-consumer fallback


def _rid(request):
    """Logged-in merchant's store for console views; demo tenant otherwise."""
    try:
        return request.session.get('restaurant_id') or 1
    except Exception:
        return 1


@router.get("/api/skills")
def list_skills(request: Request):
    return {"skills": skills_svc.list_skills(_rid(request))}


@router.post("/api/skills/suggest")
async def suggest_skill(request: Request):
    b = await request.json()
    return skills_svc.draft_skill_from_note(
        b.get("guest_message", ""), b.get("note", ""), _rid(request))


@router.post("/api/skills")
async def create_skill(request: Request):
    rid = _rid(request)
    b = await request.json()
    sid = skills_svc.create_skill(
        rid,
        b.get("name", "Skill"),
        b.get("trigger_phrases") or [],
        b.get("response", ""),
        b.get("action", "none"),
        b.get("source_log_id"),
    )
    return {"id": sid, "skills": skills_svc.list_skills(rid)}


@router.get("/api/raffle/entries")
def list_raffle_entries(request: Request):
    entries = skills_svc.raffle_entries(_rid(request))
    return {"entries": entries, "count": len(entries)}


@router.post("/api/raffle/draw")
def draw_raffle(request: Request):
    rid = _rid(request)
    w = skills_svc.draw_winner(rid)
    emailed = skills_svc.email_winner(rid, w) if w else None
    return {"winner": w, "emailed": emailed}


@router.get("/raffle/join", response_class=HTMLResponse)
def raffle_join_page(request: Request, p: str = "", s: str = "", c: str = "web_form"):
    """Autofill-friendly signup form the agent links to (Chrome fills name/email)."""
    return templates.TemplateResponse(request=request, name="raffle_join.html", context={
        "base": settings.base_path, "phone": p, "session": s, "channel": c})


@router.post("/api/raffle/submit")
async def raffle_submit(request: Request):
    b = await request.json()
    if not (b.get("name") and b.get("email")):
        return {"ok": False, "error": "Name and email are required."}
    # Validate the phone is a real, SMS-reachable (mobile) number when provided.
    chk = validate_mobile(b.get("phone"))
    if not chk["ok"]:
        return {"ok": False, "error": chk["error"], "phone_type": chk["type"]}
    skills_svc.submit_entry(restaurant_id=RID, name=b.get("name"), email=b.get("email"),
                            phone=chk["phone"] or b.get("phone"), session_id=b.get("session"),
                            channel=b.get("channel") or "web_form")
    return {"ok": True, "phone_type": chk["type"]}


@router.get("/api/guestbook")
def list_guestbook(request: Request):
    entries = gb_svc.list_entries(_rid(request))
    return {"entries": entries, "count": len(entries)}
