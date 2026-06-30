"""
Operator-taught agent skills — the self-learning loop.

A restaurant operator turns a failed conversation turn into a new capability:
their plain-English comment is drafted (gpt-4o-mini) into an intent — trigger
phrases + a response + an optional action — which they review and approve. The
unified pipeline then runs it as a deterministic fast-path BEFORE the LLM (the
same mechanism as vip.keyword_intent), so the agent gains the capability with no
redeploy. The reference example is 'raffle': recognise the intent, capture the
entrant, and confirm.

  match(...)                 → reply string if a skill triggers, else None
  draft_skill_from_note(...) → AI draft {name, trigger_phrases, response, action}
  create_skill / list_skills / raffle_entries / draw_winner
"""
import json, random, re
from app.database import SessionLocal, AgentSkill, RaffleEntry

_cache: dict = {}      # restaurant_id -> list[dict]  (active skills, hot path)
_pending: dict = {}    # identity -> {restaurant_id, skill_id, response, channel}

_EMAIL_RE = re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+')


def _identity(caller, session_id):
    return (caller or "").strip() or (session_id or "")


def bust(restaurant_id=None):
    if restaurant_id is None:
        _cache.clear()
    else:
        _cache.pop(restaurant_id, None)


def _parse_phrases(raw):
    if not raw:
        return []
    try:
        v = json.loads(raw)
        if isinstance(v, list):
            return [str(p).strip().lower() for p in v if str(p).strip()]
    except Exception:
        pass
    return [p.strip().lower() for p in raw.replace("\n", ",").split(",") if p.strip()]


def _load(restaurant_id):
    if restaurant_id in _cache:
        return _cache[restaurant_id]
    db = SessionLocal()
    try:
        rows = (db.query(AgentSkill)
                  .filter(AgentSkill.restaurant_id == restaurant_id,
                          AgentSkill.active == True).all())
        skills = [{
            "id": r.id, "name": r.name, "action": r.action or "none",
            "response": r.response or "", "phrases": _parse_phrases(r.trigger_phrases),
        } for r in rows]
        _cache[restaurant_id] = skills
        return skills
    finally:
        db.close()


def pending_reply(message, *, restaurant_id=1, session_id=None, caller=None, channel="text"):
    """If this person owes us raffle contact info (we asked on the previous turn),
    consume THIS message as their name+email, store the entrant, and confirm.
    Must run before everything else in the pipeline. Returns a reply or None."""
    ident = _identity(caller, session_id)
    p = _pending.get(ident)
    if not p:
        return None
    msg = (message or "").strip()
    m = _EMAIL_RE.search(msg)
    if not m:
        return "I just need an email to enter you — reply like: Jane Doe, jane@email.com"
    email = m.group(0)
    name = msg.replace(email, "").strip(" ,;:-—\t").strip() or None
    _pending.pop(ident, None)
    _store_raffle(restaurant_id, p["skill_id"], name=name, email=email,
                  caller=caller, session_id=session_id, channel=channel)
    who = name.split()[0] if name else "you"
    return f"You're entered, {who}! 🎉 {p.get('response') or 'Good luck!'}"


def match(message, *, restaurant_id=1, session_id=None, caller=None, channel="text"):
    """Return a reply if an operator-taught skill triggers on this message, else None."""
    msg = (message or "").lower().strip()
    if not msg:
        return None
    for sk in _load(restaurant_id):
        if any(p and p in msg for p in sk["phrases"]):
            return _fire(sk, restaurant_id=restaurant_id, session_id=session_id,
                         caller=caller, channel=channel)
    return None


def _fire(sk, *, restaurant_id, session_id, caller, channel):
    resp = sk["response"] or "Got it!"
    if sk["action"] == "raffle_entry":
        # Already entered (dedupe by phone for SMS/voice)? Don't re-collect.
        if _already_entered(restaurant_id, caller):
            return "You're already entered in the raffle — good luck! 🍀"
        # Send a quick web form (Chrome autofills name/email) and also accept a
        # typed reply as a fallback — so collect state is set either way.
        _pending[_identity(caller, session_id)] = {
            "restaurant_id": restaurant_id, "skill_id": sk["id"],
            "response": resp, "channel": channel}
        return _raffle_prompt(restaurant_id, caller, session_id, channel)
    return resp


def _raffle_prompt(restaurant_id, caller, session_id, channel):
    from urllib.parse import urlencode
    from app.config import get_settings
    base = get_settings().public_base_url.rstrip("/")
    params = {"c": channel}
    if caller:
        params["p"] = caller
    if session_id:
        params["s"] = session_id
    link = f"{base}/raffle/join?{urlencode(params)}"
    return (f"🎟️ You're almost in! Enter the raffle here (autofills in seconds): {link}\n"
            "Or just reply with your name & email.")


def _already_entered(restaurant_id, caller):
    phone = (caller or "").strip()
    if not phone:
        return False   # browser/text: no stable identity before they give an email
    db = SessionLocal()
    try:
        return (db.query(RaffleEntry)
                  .filter(RaffleEntry.restaurant_id == restaurant_id,
                          RaffleEntry.phone == phone).first()) is not None
    finally:
        db.close()


def _store_raffle(restaurant_id, skill_id, *, name, email, caller, session_id, channel):
    phone = (caller or "").strip() or None
    db = SessionLocal()
    try:
        # Dedupe: one raffle entry per email/phone per restaurant.
        dup = None
        if email:
            dup = db.query(RaffleEntry).filter(RaffleEntry.restaurant_id == restaurant_id,
                                               RaffleEntry.email == email).first()
        if not dup and phone:
            dup = db.query(RaffleEntry).filter(RaffleEntry.restaurant_id == restaurant_id,
                                               RaffleEntry.phone == phone).first()
        if not dup:
            db.add(RaffleEntry(restaurant_id=restaurant_id, skill_id=skill_id,
                               name=name, email=email, phone=phone,
                               session_id=session_id, channel=channel))
            db.commit()
    except Exception as e:
        db.rollback()
        print(f"[skills] raffle store err: {e}")
    finally:
        db.close()
    # Mirror into the hospitality guestbook (separate from the Java app's CRM).
    from app.services import guestbook as gb
    gb.add_entry(restaurant_id=restaurant_id, name=name, email=email, phone=phone, source="raffle")


def submit_entry(*, restaurant_id=1, name=None, email=None, phone=None, session_id=None, channel="web_form"):
    """Web-form submission of raffle contact info → store entry + guestbook, and
    clear any pending SMS/voice collect state for this person."""
    skill_id = next((s["id"] for s in _load(restaurant_id) if s["action"] == "raffle_entry"), None)
    _store_raffle(restaurant_id, skill_id, name=name, email=email,
                  caller=phone, session_id=session_id, channel=channel)
    for ident in {(phone or "").strip(), (session_id or "").strip()}:
        if ident:
            _pending.pop(ident, None)
    return True


# ── operator console operations ──────────────────────────────────────────────

def create_skill(restaurant_id, name, phrases, response, action="none", source_log_id=None):
    if isinstance(phrases, str):
        phrases = [phrases]
    phrases = [p.strip() for p in (phrases or []) if str(p).strip()]
    db = SessionLocal()
    try:
        sk = AgentSkill(
            restaurant_id=restaurant_id,
            name=(name or "Skill").strip()[:64],
            trigger_phrases=json.dumps(phrases),
            response=(response or "").strip(),
            action=action if action in ("none", "raffle_entry") else "none",
            source_log_id=source_log_id,
            active=True,
        )
        db.add(sk)
        db.commit()
        db.refresh(sk)
        bust(restaurant_id)
        return sk.id
    finally:
        db.close()


def list_skills(restaurant_id):
    db = SessionLocal()
    try:
        rows = (db.query(AgentSkill)
                  .filter(AgentSkill.restaurant_id == restaurant_id)
                  .order_by(AgentSkill.created_at.desc()).all())
        return [{
            "id": r.id, "name": r.name, "phrases": _parse_phrases(r.trigger_phrases),
            "response": r.response or "", "action": r.action or "none",
            "active": bool(r.active),
            "created_at": r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else "",
        } for r in rows]
    finally:
        db.close()


_SUGGEST_SYS = (
    "You design a new intent ('skill') for a restaurant's ordering assistant from "
    "the operator's instruction about a real guest message the bot failed to handle. "
    "Return STRICT JSON: {\"name\": short title, \"trigger_phrases\": [3-6 short "
    "lowercase phrases/keywords a guest would say], \"response\": one friendly "
    "SMS-length reply (<=160 chars), \"action\": \"raffle_entry\" if the intent is "
    "about entering/joining a raffle/giveaway/draw, else \"none\"}. No prose."
)


def draft_skill_from_note(guest_message, operator_note, restaurant_id=1):
    """Use gpt-4o-mini to draft a skill the operator can edit. Falls back to a
    keyword heuristic if the LLM is unavailable."""
    from app.config import get_settings
    s = get_settings()
    data = {}
    try:
        from openai import OpenAI
        client = OpenAI(api_key=s.openai_api_key)
        user = f"Guest said: {guest_message!r}\nOperator instruction: {operator_note!r}"
        r = client.chat.completions.create(
            model="gpt-4o-mini", temperature=0.3,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": _SUGGEST_SYS},
                      {"role": "user", "content": user}],
            max_tokens=200,
        )
        data = json.loads(r.choices[0].message.content)
    except Exception as e:
        print(f"[skills] draft err: {e}")

    name = (data.get("name") or "New Skill").strip()[:64]
    phrases = data.get("trigger_phrases") or data.get("phrases") or []
    if isinstance(phrases, str):
        phrases = [phrases]
    phrases = [str(p).strip() for p in phrases if str(p).strip()][:8]
    response = (data.get("response") or "").strip()
    action = data.get("action") if data.get("action") in ("none", "raffle_entry") else "none"

    # Heuristic fallback so the operator always gets a usable draft.
    blob = f"{guest_message} {operator_note}".lower()
    if not phrases:
        if "raffle" in blob or "giveaway" in blob or "draw" in blob:
            phrases = ["raffle", "add me to raffle", "enter raffle", "giveaway"]
        else:
            phrases = [w for w in (guest_message or "").lower().split() if len(w) > 3][:4]
    if action == "none" and ("raffle" in blob or "giveaway" in blob or "draw" in blob):
        action = "raffle_entry"
    if not response:
        response = ("You're entered in our raffle — good luck! 🎉"
                    if action == "raffle_entry" else "Sure — happy to help with that!")
    return {"name": name, "trigger_phrases": phrases, "response": response, "action": action}


def raffle_entries(restaurant_id):
    db = SessionLocal()
    try:
        rows = (db.query(RaffleEntry)
                  .filter(RaffleEntry.restaurant_id == restaurant_id)
                  .order_by(RaffleEntry.created_at.desc()).all())
        return [{
            "id": r.id, "name": r.name or "", "email": r.email or "",
            "phone": r.phone or "", "channel": r.channel or "",
            "session_id": r.session_id or "", "won": bool(r.won),
            "created_at": r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else "",
        } for r in rows]
    finally:
        db.close()


def draw_winner(restaurant_id):
    db = SessionLocal()
    try:
        rows = db.query(RaffleEntry).filter(RaffleEntry.restaurant_id == restaurant_id).all()
        if not rows:
            return None
        w = random.choice(rows)
        w.won = True
        db.commit()
        return {"id": w.id, "name": w.name or "", "email": w.email or "",
                "phone": w.phone or "", "channel": w.channel or ""}
    finally:
        db.close()


def email_winner(restaurant_id, winner):
    """Notify the drawn winner by email (simulated if SMTP isn't configured)."""
    if not winner or not winner.get("email"):
        return {"ok": False, "info": "no email on winner"}
    from app.services import emailer
    from app.database import Restaurant
    db = SessionLocal()
    try:
        rest = db.query(Restaurant).filter(Restaurant.id == restaurant_id).first()
        store = rest.name if rest else "Our Store"
    finally:
        db.close()
    name = winner.get("name") or "there"
    html = (f'<div style="font-family:Arial,sans-serif;max-width:480px">'
            f'<h2>🎉 You won, {name}!</h2>'
            f"<p>Congratulations — you've won {store}'s raffle. "
            f"We'll be in touch shortly with how to claim your prize.</p>"
            f'<p style="color:#888;font-size:12px">Thanks for entering. — {store}</p></div>')
    res = emailer.send_html([winner["email"]], f"🎉 You won the {store} raffle!", html, from_name=store)
    simulated = res.get("sent", 0) == 0 and "not configured" in (res.get("info") or "")
    return {"ok": res.get("sent", 0) > 0, "simulated": simulated, "to": winner["email"], "info": res.get("info")}
