"""
Plivo webhook handlers — Record + Deepgram + ElevenLabs.

Latency optimisations:
  - <Record timeout="2"> stops after 2s of silence
  - Menu in process memory
  - Claude responses cached by question hash
  - ElevenLabs MP3s cached on disk
  - RecordStop event ignored (only Redirect fires XML back to call)
  - No GetDigits wrapper (was causing greeting loop)
"""

from fastapi import APIRouter, Request, Depends, BackgroundTasks
from types import SimpleNamespace
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session
from app.database import get_db, SessionLocal, CallLog, Menu, Restaurant
from app.services.recommender import get_recommendation
from app.services.tts import text_to_speech
from app.config import get_settings
import plivo, httpx, time, hashlib

router   = APIRouter()
settings = get_settings()
BASE     = settings.base_path

# ── in-process caches ──────────────────────────────────────────────────────────
_menu_cache: dict   = {}
_claude_cache: dict = {}


def get_menu_cached(db: Session, plivo_number: str) -> tuple:
    if plivo_number in _menu_cache:
        return _menu_cache[plivo_number]
    rest = db.query(Restaurant).filter(Restaurant.plivo_number == plivo_number).first()
    if not rest:
        rest = db.query(Restaurant).first()
    if not rest:
        _menu_cache[plivo_number] = (None, None)
        return None, None
    menu = db.query(Menu).filter(Menu.restaurant_id == rest.id).order_by(Menu.id.desc()).first()
    rest_data = SimpleNamespace(id=rest.id, name=rest.name)
    result = (rest_data, menu.raw_text if menu else None)
    _menu_cache[plivo_number] = result
    return result


def get_recommendation_cached(menu_text: str, question: str) -> dict:
    key = hashlib.md5(f"{menu_text[:200]}:{question.lower().strip()}".encode()).hexdigest()
    if key in _claude_cache:
        print(f"[claude] cache hit: {repr(question)}")
        return _claude_cache[key]
    result = get_recommendation(menu_text, question)
    _claude_cache[key] = result
    return result


def bust_menu_cache(plivo_number: str = None):
    if plivo_number:
        _menu_cache.pop(plivo_number, None)
    else:
        _menu_cache.clear()


def deepgram_transcribe(audio_url: str) -> str:
    dg_key     = settings.deepgram_api_key
    auth_id    = settings.plivo_auth_id
    auth_token = settings.plivo_auth_token
    if not dg_key:
        return ""
    try:
        t0 = time.time()
        dl = httpx.get(audio_url, auth=(auth_id, auth_token), timeout=10.0)
        dl.raise_for_status()
        print(f"[deepgram] download {len(dl.content)}b in {round((time.time()-t0)*1000)}ms")
        t1   = time.time()
        resp = httpx.post(
            "https://api.deepgram.com/v1/listen?model=nova-2&smart_format=true",
            headers={"Authorization": f"Token {dg_key}", "Content-Type": "audio/mp3"},
            content=dl.content,
            timeout=15.0,
        )
        resp.raise_for_status()
        transcript = (resp.json().get("results", {})
                                 .get("channels", [{}])[0]
                                 .get("alternatives", [{}])[0]
                                 .get("transcript", ""))
        print(f"[deepgram] transcribe {round((time.time()-t1)*1000)}ms → {repr(transcript)}")
        return transcript.strip()
    except Exception as e:
        print(f"[deepgram] error: {e}")
        return ""


def speak_xml(text: str) -> str:
    url = text_to_speech(text)
    if url:
        return f"<Play>{url}</Play>"
    safe = text.replace("&", "and").replace("<", "").replace(">", "")
    return f'<Speak voice="WOMAN">{safe}</Speak>'


# ── VOICE: inbound call ─────────────────────────────────────────────────────
@router.post("/api/voice/inbound", response_class=PlainTextResponse)
async def voice_inbound(request: Request, db: Session = Depends(get_db)):
    form      = await request.form()
    to_number = form.get("To", settings.plivo_number)

    rest, _ = get_menu_cached(db, to_number)
    rest_name = rest.name if rest else "the restaurant"
    ws_url    = f"wss://support.hostbuddy.io{BASE}/ws/voice"
    greeting  = f"Welcome to {rest_name}. What would you like today?"

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {speak_xml(greeting)}
  <Stream bidirectional="true" contentType="audio/x-mulaw;rate=8000" streamTimeout="86400">{ws_url}</Stream>
  <Wait length="3600"/>
</Response>"""
    return PlainTextResponse(content=xml, media_type="application/xml")


# ── VOICE: recording callback ────────────────────────────────────────────────
@router.post("/api/voice/transcribe", response_class=PlainTextResponse)
async def voice_transcribe(request: Request, db: Session = Depends(get_db)):
    t_start = time.time()
    form    = await request.form()

    event         = form.get("Event", "")
    recording_url = form.get("RecordUrl") or form.get("RecordFile", "")
    caller        = form.get("From", "unknown")
    to_num        = form.get("To", settings.plivo_number)

    print(f"[transcribe] event={event} url_present={bool(recording_url)}")

    transcribe_url = f"https://support.hostbuddy.io{BASE}/api/voice/transcribe"

    # Plivo fires two events per recording: Redirect (action) and RecordStop (callback).
    # Only Redirect expects XML — RecordStop is a fire-and-forget notification.
    if event == "RecordStop":
        return PlainTextResponse(content="", media_type="application/xml")

    rest, menu_text = get_menu_cached(db, to_num)
    rest_name = rest.name if rest else "us"

    transcript = deepgram_transcribe(recording_url) if recording_url else ""

    if not transcript:
        retry = "Sorry, I did not catch that. Please speak after the beep."
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {speak_xml(retry)}
  <Record action="{transcribe_url}" method="POST"
          maxLength="10" timeout="2" finishOnKey="#" />
</Response>"""
        return PlainTextResponse(content=xml, media_type="application/xml")

    if not menu_text:
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {speak_xml("The menu is not available right now. Please try again later.")}
</Response>"""
        return PlainTextResponse(content=xml, media_type="application/xml")

    result   = get_recommendation_cached(menu_text, transcript)
    rec_text = result["recommendation"]
    total_ms = round((time.time() - t_start) * 1000, 1)
    print(f"[transcribe] total_ms={total_ms}")

    log = CallLog(
        restaurant_id     = rest.id if rest else None,
        call_type         = "voice",
        caller_number     = caller,
        transcript        = transcript,
        recommendation    = rec_text,
        claude_latency_ms = result["claude_latency_ms"],
        total_latency_ms  = total_ms,
        noise_flag        = result["noise_flag"],
        status            = "ok",
    )
    db.add(log); db.commit()

    # Respond then immediately open next recording — no greeting loop
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {speak_xml(rec_text)}
  <Speak voice="WOMAN">Anything else?</Speak>
  <Record action="{transcribe_url}" method="POST"
          maxLength="10" timeout="2" finishOnKey="#" />
</Response>"""
    return PlainTextResponse(content=xml, media_type="application/xml")


# ── Hotel concierge static intents ───────────────────────────────────────────

_CLOSING_PHRASES = {
    "i'm done", "i am done", "that's all", "that's everything", "that's it",
    "nothing else", "no thanks", "no thank you", "thank you", "thanks",
    "goodbye", "bye", "bye bye", "i'm good", "i am good", "i'm all set",
    "all good", "sounds good",
}

# Intent responses
_R = {
    # 1. Check-in → sauna + dinner offer
    "checkin": (
        "Welcome to Rosewood! May I suggest a relaxing sauna session at 5:15 PM to unwind after your journey? "
        "I can also hold a quiet dinner table at Bemelmans Bar or Dowling's at the Carlyle for this evening."
    ),
    # 2. Morning room service
    "room_service": (
        "Of course! Your order is on its way. "
        "Shall I add a fresh orange juice to complete your morning?"
    ),
    # 3. Anniversary / special occasion dinner
    "anniversary": (
        "How special! I'll reserve the window table at Dowling's at the Carlyle for this evening. "
        "Shall I arrange a bottle of Barolo to make it truly memorable?"
    ),
    # 4. Specific dinner restaurants
    "dinner_bemelmans": (
        "Perfect! Dinner for two reserved at Bemelmans Bar at 7 PM. "
        "Iconic murals, nightly jazz — a wonderful choice. Enjoy your evening!"
    ),
    "dinner_dowlings": (
        "Wonderful! Dinner for two reserved at Dowling's at the Carlyle at 7 PM. "
        "Seasonal New York cuisine in a stunning setting. Enjoy!"
    ),
    "dinner_generic": (
        "Happy to arrange dinner. Would you prefer Bemelmans Bar for live jazz, "
        "or Dowling's at the Carlyle for a seasonal tasting menu?"
    ),
    # 5. Early / dawn golf
    "golf_early": (
        "Excellent! I've booked a 7:20 AM tee time for tomorrow morning. "
        "A breakfast box will be waiting for you at hole 1. Sleep well!"
    ),
    # 6. Regular golf / tee time
    "golf": (
        "Perfect! Your golf tee time is reserved for 5:30 PM. "
        "Please arrive at the pro shop 15 minutes early. Enjoy your round!"
    ),
    # 7. Free time / done with meetings early
    "free_time": (
        "Great news! With some free time, may I suggest a spa treatment, "
        "a twilight golf session, or a sauna to recharge? I can arrange any of these right away."
    ),
    # 8. Sauna
    "sauna": (
        "Of course! The sauna is available now. I'll reserve your private session at 5:15 PM. "
        "Shall I also arrange a quiet dinner table for afterwards?"
    ),
    # 9. Spa
    "spa": (
        "Wonderful choice! I'll book a spa treatment for you. "
        "Would you prefer a massage, a facial, or shall I recommend our signature Rosewood ritual?"
    ),
    # 10. Flight delayed / departure rescue
    "flight_delay": (
        "I'm sorry to hear that! I've arranged a late checkout, rebooked your shuttle, "
        "and placed a hold on any spa appointments. Please enjoy a leisurely breakfast — we'll handle everything."
    ),
    # 11. Closing
    "closing": "Thank you for choosing Rosewood Hotels. Have a wonderful stay!",
}


def _hotel_intent(text: str):
    """Match guest message to a concierge intent. Returns response string or None."""
    t = text.lower().strip()
    n = t.rstrip(".!?,—")

    # Closing
    if n in _CLOSING_PHRASES:
        return _R["closing"]

    # Flight delay / departure rescue
    if any(k in t for k in ["flight delay", "flight's delay", "flight is delay",
                             "delayed", "missed flight", "late checkout", "late check-out"]):
        return _R["flight_delay"]

    # Free time / done with meetings
    if any(k in t for k in ["done with meeting", "done early", "free time",
                             "finished early", "meeting's done", "meetings done",
                             "done with work", "finished with meeting"]):
        return _R["free_time"]

    # Anniversary / special occasion
    if any(k in t for k in ["anniversary", "somewhere nice", "special occasion",
                             "celebrate", "celebration", "romantic"]):
        return _R["anniversary"]

    # Sauna
    if "sauna" in t:
        return _R["sauna"]

    # Spa
    if any(k in t for k in ["spa", "massage", "facial", "treatment"]):
        return _R["spa"]

    # Early / dawn golf (check before generic golf)
    if any(k in t for k in ["early", "morning", "dawn", "tomorrow", "7am", "7 am"]) and \
       any(k in t for k in ["golf", "tee", "round"]):
        return _R["golf_early"]

    # Generic golf / tee time
    if any(k in t for k in ["golf", "tee time", "tee-time"]):
        return _R["golf"]

    # Check-in
    if any(k in t for k in ["checking in", "check in", "check-in", "i arrive", "arriving"]):
        return _R["checkin"]

    # Room service / morning order (food/drink keywords without dinner context)
    if any(k in t for k in ["espresso", "croissant", "breakfast", "room service",
                             "send up", "bring up", "order food", "order coffee"]):
        return _R["room_service"]

    # Dinner — specific restaurant or generic
    if any(k in t for k in ["dinner", "restaurant", "dining", "table for", "eat tonight"]):
        if "bemelmans" in t:
            return _R["dinner_bemelmans"]
        if "dowling" in t or "carlyle" in t:
            return _R["dinner_dowlings"]
        return _R["dinner_generic"]

    return None


# ── SMS dedup — prevent Plivo retries from sending duplicate replies ──────────
_sms_seen: dict = {}   # key: (caller, body_hash) → timestamp

def _sms_dedup_key(caller: str, body: str) -> str:
    return f"{caller}:{hashlib.md5(body.lower().strip().encode()).hexdigest()}"

def _is_duplicate_sms(caller: str, body: str, window_sec: int = 60) -> bool:
    key = _sms_dedup_key(caller, body)
    now = time.time()
    if key in _sms_seen and now - _sms_seen[key] < window_sec:
        return True
    _sms_seen[key] = now
    return False


def _process_sms_background(body: str, caller: str, to_num: str):
    """Run in background so the webhook returns 200 instantly (stops Plivo retries)."""
    t_start = time.time()
    db = SessionLocal()
    try:
        rest, menu_text = get_menu_cached(db, to_num)
        offers_url = f"https://support.hostbuddy.io{BASE}/offers"

        hotel_reply = _hotel_intent(body)
        if hotel_reply:
            reply, claude_ms, noise = hotel_reply, 0.0, False
            print(f"[sms] hotel intent: {repr(body[:40])}")
            # Append offers link on check-in so guest sees all available services
            if any(k in body.lower() for k in ["checking in", "check in", "check-in", "arrive"]):
                link = f" {offers_url}"
                reply = reply[:160 - len(link)] + link
        elif not menu_text:
            reply, claude_ms, noise = "Menu not available yet.", 0.0, False
        else:
            result    = get_recommendation_cached(menu_text, body or "What do you recommend?")
            reply     = result["recommendation"]
            claude_ms = result["claude_latency_ms"]
            noise     = result["noise_flag"]
            # Append offers link so guests can explore all services
            reply = reply[:130] + f" More: {offers_url}"

        total_ms = round((time.time() - t_start) * 1000, 1)
        print(f"[sms] reply ({total_ms}ms): {reply[:80]}")

        try:
            client = plivo.RestClient(settings.plivo_auth_id, settings.plivo_auth_token)
            client.messages.create(src=to_num, dst=caller, text=reply[:160])
        except Exception as e:
            print(f"[sms] send failed: {e}")

        log = CallLog(
            restaurant_id     = rest.id if rest else None,
            call_type         = "sms",
            caller_number     = caller,
            transcript        = body,
            recommendation    = reply,
            claude_latency_ms = claude_ms,
            total_latency_ms  = total_ms,
            noise_flag        = noise,
            status            = "ok",
        )
        db.add(log); db.commit()
    finally:
        db.close()


# ── SMS ──────────────────────────────────────────────────────────────────────
@router.post("/api/sms/inbound", response_class=PlainTextResponse)
async def sms_inbound(request: Request, background_tasks: BackgroundTasks):
    form   = await request.form()
    body   = form.get("Text", "").strip()
    caller = form.get("From", "unknown")
    to_num = form.get("To", settings.plivo_number)

    print(f"[sms] from={caller} text={repr(body)}")

    # Deduplicate: Plivo retries the webhook if we're slow. Ignore repeats within 60s.
    if _is_duplicate_sms(caller, body):
        print(f"[sms] duplicate ignored: {repr(body[:40])}")
        return PlainTextResponse(content="", media_type="application/xml")

    # Return 200 immediately — process + reply in background to prevent retries
    background_tasks.add_task(_process_sms_background, body, caller, to_num)
    return PlainTextResponse(content="", media_type="application/xml")
