"""
Browser-based voice test endpoint.
POST /api/voice/web/chat -- accepts audio blob, returns {transcript, reply, audio_url}
GET  /voice-test          -- serves the test UI (handled in main.py)

STT: Google Cloud Speech-to-Text (same service account as voice_ws.py)
  - 220+ languages, auto-detects from en/es/hi/fr/pt
  - Returns detected language so Claude responds in same language
"""

from fastapi import APIRouter, UploadFile, File, Request
from fastapi.responses import JSONResponse
from types import SimpleNamespace
from google.cloud import speech as google_speech
from google.oauth2 import service_account
from app.database import SessionLocal, Restaurant, Menu
from app.services.recommender import get_recommendation
from app.services.tts import text_to_speech
from app.config import get_settings
import hashlib, time, os

router   = APIRouter()
settings = get_settings()

_menu_cache:   dict = {}
_claude_cache: dict = {}
_history:      dict = {}   # session_id → list[{role,content}]

def bust_web_menu_cache():
    _menu_cache.clear()
    _claude_cache.clear()
_speech_client       = None   # lazy-init on first request

def _get_speech_client():
    global _speech_client
    if _speech_client is None:
        creds_path = (
            settings.google_application_credentials
            or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
        )
        if creds_path and os.path.exists(creds_path):
            credentials = service_account.Credentials.from_service_account_file(
                creds_path,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            _speech_client = google_speech.SpeechClient(credentials=credentials)
        else:
            _speech_client = google_speech.SpeechClient()
        print(f"[voice-web] Google Speech client ready (creds: {creds_path})")
    return _speech_client

def _load_menu() -> tuple:
    if "default" in _menu_cache:
        return _menu_cache["default"]
    db = SessionLocal()
    try:
        rest = db.query(Restaurant).first()
        if not rest:
            return None, None
        menu = db.query(Menu).filter(Menu.restaurant_id == rest.id).order_by(Menu.id.desc()).first()
        rest_data = SimpleNamespace(id=rest.id, name=rest.name)
        result = (rest_data, menu.raw_text if menu else None)
        _menu_cache["default"] = result
        return result
    finally:
        db.close()


def _transcribe_audio(audio_bytes: bytes, content_type: str = "audio/webm") -> tuple:
    """Returns (transcript, language_code) using Google Speech."""
    try:
        audio  = google_speech.RecognitionAudio(content=audio_bytes)
        config = google_speech.RecognitionConfig(
            encoding=google_speech.RecognitionConfig.AudioEncoding.WEBM_OPUS,
            language_code="en-US",
            alternative_language_codes=["es-US", "es-ES"],
            enable_automatic_punctuation=True,
            model="latest_short",   # optimised for short utterances (< 60s)
        )
        response = _get_speech_client().recognize(config=config, audio=audio)
        if not response.results:
            return "", "en"
        result     = response.results[0]
        transcript = result.alternatives[0].transcript.strip() if result.alternatives else ""
        language   = (result.language_code or "en-US").split("-")[0].lower()
        print(f"[voice-web] stt [{language}]: {repr(transcript)}")
        return transcript, language
    except Exception as e:
        print(f"[voice-web] transcribe error: {e}")
        return "", "en"


@router.get("/api/voice/web/greet")
async def web_voice_greet():
    rest, _ = _load_menu()
    name     = rest.name if rest else "our hotel"
    greeting = f"Welcome to {name}. I'm Alex, your voice concierge. How can I help you today?"
    audio_url = text_to_speech(greeting) or ""
    return JSONResponse({"greeting": greeting, "audio_url": audio_url})


@router.post("/api/voice/web/chat")
async def web_voice_chat(audio: UploadFile = File(...),
                         session_id: str = "web-default"):
    t_start      = time.time()
    audio_bytes  = await audio.read()
    content_type = audio.content_type or "audio/webm"

    transcript, language = _transcribe_audio(audio_bytes, content_type)
    if not transcript:
        return JSONResponse({"error": "Could not transcribe audio", "transcript": "", "reply": "", "audio_url": ""})

    rest, menu_text = _load_menu()

    checkout_url = None
    cart_items   = None
    cart_total   = None

    if not menu_text:
        reply = "Our menu isn't available right now. Please try again soon."
    else:
        hist = _history.setdefault(session_id, [])
        # Skip cache when there is conversation context, or for cart-affecting words.
        # With per-session cart + history + fast-path, caching by transcript alone
        # would return stale answers — disable the moment there's any prior turn.
        _SKIP_CACHE_WORDS = ("add", "remove", "cart", "order", "cancel", "place", "checkout", "pay",
                             "yes", "yeah", "sure", "ok", "okay")
        skip_cache = bool(hist) or any(w in transcript.lower().split() for w in _SKIP_CACHE_WORDS)
        key = hashlib.md5(f"{rest.id if rest else 0}:{language}:{transcript.lower().strip()}".encode()).hexdigest()
        if not skip_cache and key in _claude_cache:
            result = _claude_cache[key]
        else:
            result = get_recommendation(
                menu_text, transcript,
                state="browsing",
                language=language,
                restaurant_id=rest.id if rest else 1,
                session_id=session_id,
                history=hist,
            )
            if not skip_cache:
                _claude_cache[key] = result
        reply        = result["recommendation"]
        checkout_url = result.get("checkout_url")
        cart_items   = result.get("cart_items")
        cart_total   = result.get("total")

        # Append turn to history for next request; trim to last 12 messages
        hist.append({"role": "user",      "content": transcript})
        hist.append({"role": "assistant", "content": reply})
        if len(hist) > 12:
            _history[session_id] = hist[-12:]

    # Always include cart snapshot so the UI can render a live receipt even
    # when an order isn't being placed yet (parity with the SMS / phone flow).
    if cart_items is None:
        try:
            from app.services import cart as cart_svc
            _live_cart = cart_svc.get(session_id)
            if _live_cart and _live_cart.items:
                cart_items = _live_cart.to_dict()["items"]
                cart_total = f"{_live_cart.total():.2f}"
        except Exception as e:
            print(f"[voice-web] cart snapshot err: {e}")

    audio_url = text_to_speech(reply) or ""
    total_ms  = round((time.time() - t_start) * 1000)
    print(f"[voice-web] {total_ms}ms [{language}] | {repr(transcript)} -> {repr(reply[:60])}")

    resp: dict = {
        "transcript":   transcript,
        "reply":        reply,
        "audio_url":    audio_url,
        "language":     language,
        "ms":           total_ms,
    }
    if checkout_url:
        resp["checkout_url"] = checkout_url
    if cart_items:
        resp["cart_items"] = cart_items
        resp["cart_total"] = cart_total
    return JSONResponse(resp)
