"""
Plivo bidirectional Stream WebSocket handler.

Protocol:
  Plivo → us: JSON text frames  {event: connected|start|media|stop}
  Us → Plivo: JSON text frames  {event: playAudio|clearAudio}

Sequence:
  1. Accept WS
  2. Wait for Plivo "connected" then "start" events
  3. Start Google Speech streaming (gRPC in background thread)
  4. Forward media frames to Google via asyncio.Queue
  5. On Google is_final transcript → Claude → ElevenLabs response
  6. Barge-in: interim transcript while TTS active → clearAudio + cancel

STT: Google Cloud Speech-to-Text v1 streaming
  - 220+ languages with automatic language detection
  - phone_call model optimised for 8kHz mulaw audio
  - Languages: en-US, es-US, es-ES, hi-IN, fr-FR, pt-BR (expandable)

NOTE: Greeting is played by the inbound XML <Play> before Stream starts.
"""

import asyncio, json, base64, time, hashlib, threading, os, random
from dataclasses import dataclass, field
from enum import Enum
from types import SimpleNamespace
import httpx
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState
from google.cloud import speech as google_speech
from google.oauth2 import service_account
from app.config import get_settings
from app.database import SessionLocal, Restaurant, Menu, CallLog
from app.services.recommender import get_recommendation, complete_order as finalize_order
from app.services import cart as cart_svc

router   = APIRouter()
settings = get_settings()

def _make_speech_client():
    """Create Google SpeechClient with explicit service account credentials."""
    import os
    creds_path = (settings.google_application_credentials
                  or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", ""))
    if creds_path and os.path.exists(creds_path):
        credentials = service_account.Credentials.from_service_account_file(
            creds_path,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        return google_speech.SpeechClient(credentials=credentials)
    return google_speech.SpeechClient()

# Single-word noise to ignore (picked up from breathing / background)
_NOISE_WORDS = {"uh", "um", "hmm", "hm", "ah", "mm", "mhm", "huh", "oh"}


# ── Google STT config builder ─────────────────────────────────────────────────
# Java parity (BargeInPhoneBotRest line 10027-10039):
#   model="phone_call", use_enhanced=true, addSpeechContexts(menu_items_as_phrases)
#
# We do not use alternative_language_codes anymore — it forces "latest_long" /
# "latest_short", which are NOT tuned for 8 kHz telephony audio. The accuracy
# loss on en-US is much worse than the gain from auto-detecting Spanish. Spanish
# callers can be served by per-restaurant config later (separate code path).

_STT_PHRASE_CACHE: dict = {}    # restaurant_id → list[str] (top menu item names)


def _build_phrase_hints_from_menu(restaurant_id: int, menu_text: str, limit: int = 400) -> list:
    """
    Build Google STT phrase hints. Prefers the structured menu (item + modifier
    names with proper spelling) and falls back to regex-on-raw-text for
    restaurants without structured data.
    """
    # Prefer structured menu — actual item / modifier vocabulary, no false positives
    try:
        from app.services import menu_cache as _mc
        hints = _mc.get_phrase_hints(restaurant_id, menu_text)
        if hints:
            return list(hints)[:limit]
    except Exception as e:
        print(f"[stt] phrase hints structured lookup failed: {e}")

    # Legacy raw-text extraction (used only if structured menu is empty)
    import re as _re
    hints = set()
    for line in (menu_text or "").replace("\\n", "\n").splitlines():
        line = line.strip()
        if not line or len(line) > 80:
            continue
        if line.isupper() and 4 <= len(line) <= 60:
            cleaned = _re.sub(r'[®™\(\)\*]', '', line).strip()
            if cleaned:
                hints.add(cleaned.title())
        for m in _re.finditer(r'\b((?:[A-Z][a-zA-Z\-]+\s+){1,5}[A-Z][a-zA-Z\-]+)\b', line):
            phrase = m.group(1).strip()
            if 6 <= len(phrase) <= 50:
                hints.add(phrase)
    hints.update(["place my order", "I'd like to order", "I want", "add to cart",
                  "no bacon", "no onions", "extra cheese", "hash browns",
                  "I'm done", "checkout", "cancel"])
    return list(hints)[:limit]


def _build_stt_config(restaurant_id: int = 1, menu_text: str = "") -> "google_speech.StreamingRecognitionConfig":
    """Per-call STT config with menu phrase hints baked in."""
    phrases = _build_phrase_hints_from_menu(restaurant_id, menu_text)
    speech_contexts = []
    if phrases:
        speech_contexts.append(google_speech.SpeechContext(phrases=phrases, boost=15.0))

    return google_speech.StreamingRecognitionConfig(
        config=google_speech.RecognitionConfig(
            encoding=google_speech.RecognitionConfig.AudioEncoding.MULAW,
            sample_rate_hertz=8000,
            language_code="en-US",
            model="phone_call",               # Java parity — purpose-built for 8 kHz telephony
            use_enhanced=True,                # Java parity — higher-accuracy model variant
            enable_automatic_punctuation=True,
            speech_contexts=speech_contexts,  # menu items as phrase hints (boost=15)
            max_alternatives=1,
            profanity_filter=False,
        ),
        interim_results=True,                 # needed for barge-in detection
        single_utterance=False,
    )


# Fallback when call starts before menu is loaded
_GOOGLE_STT_CONFIG = _build_stt_config(1, "")

_menu_cache:   dict = {}
_claude_cache: dict = {}


class CallState(Enum):
    GREETING      = "greeting"
    BROWSING      = "browsing"
    ITEM_SELECTED = "item_selected"
    UPSELL        = "upsell"
    CONFIRMATION  = "confirmation"
    CLOSING       = "closing"

@dataclass
class Session:
    state:    CallState = CallState.GREETING
    items:    list      = field(default_factory=list)
    turns:    int       = 0
    language: str       = "en"   # updated live from Deepgram detect_language
    history:  list      = field(default_factory=list)   # [{"role":"user"/"assistant","content":...}]


_filler_cache: dict = {}  # phrase -> mulaw bytes

_FILLERS = {
    # Single phrase across all states — caller asked: "use 'absolutely' not the
    # variety pack". Keeps the sound consistent and natural.
    "browsing":       ["Absolutely."],
    "item_selected":  ["Absolutely."],
    "upsell":         ["Absolutely."],
    "default":        ["Absolutely."],
}

# Per-call filler rate limiting — don't play on every turn ("absolutely.
# absolutely. absolutely." gets noisy). Skip 2 out of 3 turns.
_FILLER_RATE = 0.33   # probability of playing

async def _warm_filler_cache(eleven_chunks_fn):
    """Pre-generate ElevenLabs TTS for all filler phrases. Called once per process."""
    filler_dir = "/home/azureuser/work/recsys/static/audio/fillers"
    os.makedirs(filler_dir, exist_ok=True)
    all_phrases = {p for phrases in _FILLERS.values() for p in phrases}
    for phrase in all_phrases:
        key = hashlib.md5(phrase.encode()).hexdigest()
        path = f"{filler_dir}/{key}.mulaw"
        if os.path.exists(path):
            with open(path, "rb") as f:
                _filler_cache[phrase] = f.read()
            print(f"[filler] loaded from disk: {repr(phrase)}")
            continue
        try:
            chunks = []
            async for chunk in eleven_chunks_fn(phrase):
                chunks.append(chunk)
            if chunks:
                data = b"".join(chunks)
                _filler_cache[phrase] = data
                with open(path, "wb") as f:
                    f.write(data)
                print(f"[filler] cached: {repr(phrase)}")
        except Exception as e:
            print(f"[filler] failed {repr(phrase)}: {e}")


def _load_menu(plivo_number: str) -> tuple:
    if plivo_number in _menu_cache:
        return _menu_cache[plivo_number]
    db = SessionLocal()
    try:
        rest = db.query(Restaurant).filter(Restaurant.plivo_number == plivo_number).first()
        if not rest:
            rest = db.query(Restaurant).first()
        if not rest:
            return None, None
        menu = (db.query(Menu)
                  .filter(Menu.restaurant_id == rest.id)
                  .order_by(Menu.id.desc()).first())
        rest_data = SimpleNamespace(id=rest.id, name=rest.name)
        result = (rest_data, menu.raw_text if menu else None)
        _menu_cache[plivo_number] = result
        return result
    finally:
        db.close()


def _cached_rec(menu_text: str, question: str, state=None, language: str = "en",
                restaurant_id: int = 1, session_id: str = "phone-default",
                history: list = None) -> dict:
    state_val = state.value if hasattr(state, "value") else "browsing"
    # Don't cache anything when there's conversation history — context matters
    has_history = bool(history)
    cart_ops = {"add_to_cart", "remove_from_cart", "complete_order", "cancel_order", "get_cart"}
    q_lower  = question.lower().strip()
    skip_cache = has_history or any(op.replace("_", " ") in q_lower for op in ["add", "remove", "order", "cart", "yes", "yeah", "sure"])
    if not skip_cache:
        key = hashlib.md5(
            f"{restaurant_id}:{q_lower}:{state_val}:{language}".encode()
        ).hexdigest()
        if key in _claude_cache:
            print(f"[claude] cache hit: {repr(question)}")
            return _claude_cache[key]
    else:
        key = None

    result = get_recommendation(menu_text, question, state_val, language,
                                restaurant_id, session_id, history=history)
    if key:
        _claude_cache[key] = result
    return result


async def _eleven_chunks(text: str, language: str = "en"):
    """Get ElevenLabs MP3, convert to mulaw 8000Hz via ffmpeg, yield 640-byte chunks."""
    api_key  = settings.elevenlabs_api_key
    voice_id = settings.elevenlabs_voice_id
    if not api_key:
        return

    # Pre-recorded filler audio ("Sure, one moment.", "Excellent choice.", etc.) plays
    # alongside the real response — no need to prepend "Mm." here, which stacked
    # two filler sounds at the start of every turn.

    payload = {
        "text": text,
        "model_id": "eleven_turbo_v2_5",   # multilingual — supports EN + ES + more
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    }
    if language and not language.startswith("en"):
        payload["language_code"] = language[:2]   # "es", "fr", etc.

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            headers={"xi-api-key": api_key, "Content-Type": "application/json"},
            json=payload,
        )
        mp3_data = resp.content

    if not mp3_data:
        print("[tts] ElevenLabs returned empty response")
        return

    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-i", "pipe:0",
        "-ar", "8000", "-ac", "1",
        "-acodec", "pcm_mulaw", "-f", "mulaw", "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    ulaw_data, _ = await proc.communicate(input=mp3_data)

    if not ulaw_data:
        print("[tts] ffmpeg produced no output")
        return

    print(f"[tts] converted {len(mp3_data)}b MP3 -> {len(ulaw_data)}b mulaw")

    chunk_size = 640
    for i in range(0, len(ulaw_data), chunk_size):
        chunk = ulaw_data[i:i + chunk_size]
        if chunk:
            yield chunk


def _ws_open(ws: WebSocket) -> bool:
    return ws.client_state == WebSocketState.CONNECTED


@router.websocket("/ws/voice")
async def voice_ws(ws: WebSocket):
    await ws.accept()
    print("[ws] accepted")

    import uuid
    call_session_id = f"phone-{uuid.uuid4().hex[:12]}"
    to_number  = settings.plivo_number
    # Caller's phone — passed by voice_inbound as ?from=... since Plivo's WS start event
    # does not include it. Used downstream to SMS the checkout link on complete_order.
    from_query = (ws.query_params.get("from") or "").strip()
    rest       = None
    menu_text  = None
    rest_name  = "the restaurant"
    tts_active = False
    stop_tts   = asyncio.Event()
    tts_task   = None
    session     = Session()
    filler_task = None
    _cache_warmed = False

    # ── Audio framing ─────────────────────────────────────────────────────────
    # 8 kHz mulaw = 8000 bytes/sec. We send ~160ms frames (1280 bytes) and pad
    # 40ms of silence at the leading edge of TTS payloads (mulaw silence = 0xFF)
    # to avoid the click that ElevenLabs' MP3→mulaw conversion produces at t=0.
    #
    # NOTE: we tried Plivo `mark`/ack-based sync — Plivo's Stream API does NOT
    # reliably echo a playedStream event back, so awaiting an ack added 2s of
    # dead air to every reply. Removed.
    _MULAW_SILENCE_BYTE  = b"\xff"
    _FRAME_SIZE_BYTES    = 1280           # 160ms per frame
    _LEAD_PAD_BYTES      = 320            # 40ms silence at start (no trailing pad)

    async def safe_send(payload: dict):
        try:
            if _ws_open(ws):
                await ws.send_json(payload)
        except Exception as e:
            print(f"[ws] send error: {e}")

    async def _send_mulaw_stream(data: bytes, label: str = "tts"):
        """Stream a mulaw payload to Plivo as 160ms frames."""
        if not data or not _ws_open(ws):
            return
        padded = (_MULAW_SILENCE_BYTE * _LEAD_PAD_BYTES) + data
        # 160ms of audio per frame, paced at 140ms wall clock so Plivo's
        # jitter buffer stays slightly ahead of real time → no underrun clicks.
        for i in range(0, len(padded), _FRAME_SIZE_BYTES):
            if stop_tts.is_set() or not _ws_open(ws):
                await safe_send({"event": "clearAudio"})
                return
            await safe_send({
                "event": "playAudio",
                "media": {
                    "contentType": "audio/x-mulaw",
                    "sampleRate":  8000,
                    "payload":     base64.b64encode(padded[i:i + _FRAME_SIZE_BYTES]).decode(),
                },
            })
            await asyncio.sleep(0.140)

    async def send_tts(text: str):
        nonlocal tts_active
        stop_tts.clear()
        print(f"[tts] fetching [{session.language}]: {text[:60]}")
        try:
            buf = b""
            async for chunk in _eleven_chunks(text, session.language):
                buf += chunk
            if not buf or not _ws_open(ws):
                return
            tts_active = True
            print(f"[tts] playing {len(buf)/8000:.2f}s of audio")
            await _send_mulaw_stream(buf, label="tts")
        except asyncio.CancelledError:
            await safe_send({"event": "clearAudio"})
            print("[tts] cancelled -- clearAudio sent")
        finally:
            tts_active = False

    async def send_filler(state_key: str):
        """
        Play a brief "Absolutely." filler — but only sometimes.
        Rules (tuned from user feedback):
        - Only fire 1 in 3 turns (random rate-limit) — prevents "absolutely,
          absolutely, absolutely" on every reply.
        - Wait 1.2s first — if LLM finishes quickly, skip it entirely.
        - Once started, plays to completion; main TTS waits for it.
        """
        if random.random() > _FILLER_RATE:
            return
        await asyncio.sleep(1.2)
        if tts_active or stop_tts.is_set() or not _ws_open(ws):
            return
        phrases = _FILLERS.get(state_key, _FILLERS["default"])
        phrase  = random.choice(phrases)
        data    = _filler_cache.get(phrase)
        if not data or not _ws_open(ws):
            return
        print(f"[filler] playing: {phrase!r}")
        padded = (_MULAW_SILENCE_BYTE * _LEAD_PAD_BYTES) + data
        for i in range(0, len(padded), _FRAME_SIZE_BYTES):
            if stop_tts.is_set() or not _ws_open(ws):
                return
            await safe_send({
                "event": "playAudio",
                "media": {
                    "contentType": "audio/x-mulaw",
                    "sampleRate":  8000,
                    "payload":     base64.b64encode(padded[i:i + _FRAME_SIZE_BYTES]).decode(),
                },
            })
            await asyncio.sleep(0.140)

    _CLOSING_SET = {
        # Single-word stop signals
        "stop", "done", "finished", "cancel", "quit", "enough", "bye", "goodbye",
        "nevermind",
        # Short phrases
        "never mind", "all done", "no more", "that's it", "that's all",
        "that's everything", "that's enough", "nothing else", "all good",
        "no thanks", "no thank you", "thank you", "thanks", "bye bye",
        "sounds good", "that will be all", "that would be all",
        # "I" phrases
        "i'm done", "i am done", "i'm good", "i am good", "i'm all set",
        "i am all set", "i'm set", "i am set", "i'm finished", "i am finished",
        "i'm fine", "i am fine", "i'm satisfied", "i am satisfied",
        # Thank-you closings
        "great thanks", "great thank you", "ok thanks", "okay thanks",
        "ok thank you", "okay thank you", "perfect thanks",
        # Spanish
        "gracias", "hasta luego", "adios", "adiós", "listo", "ya es todo",
        "eso es todo", "nada más", "muchas gracias", "está bien", "esta bien",
    }

    _INTENTS = {
        "checkin":         ("Welcome to Rosewood! May I suggest a relaxing sauna at 5:15 PM to unwind? "
                            "I can also hold a dinner table at Bemelmans Bar or Dowling's at the Carlyle tonight."),
        "room_service":    ("Of course! Your order is on its way. "
                            "Shall I add a fresh orange juice to complete your morning?"),
        "anniversary":     ("How special! I'll reserve the window table at Dowling's at the Carlyle for this evening. "
                            "Shall I arrange a bottle of Barolo to make it truly memorable?"),
        "dinner_bemelmans":("Perfect! Dinner for two reserved at Bemelmans Bar at 7 PM. "
                            "Nightly jazz, iconic murals -- a wonderful choice. Enjoy!"),
        "dinner_dowlings": ("Wonderful! Dinner for two reserved at Dowling's at the Carlyle at 7 PM. "
                            "Seasonal New York cuisine in a stunning setting. Enjoy!"),
        "dinner_generic":  ("Happy to arrange dinner. Would you prefer Bemelmans Bar for live jazz, "
                            "or Dowling's at the Carlyle for a seasonal tasting menu?"),
        "golf_early":      ("Excellent! I've booked a 7:20 AM tee time for tomorrow morning. "
                            "A breakfast box will be waiting at hole 1. Sleep well!"),
        "golf":            ("Perfect! Your tee time is reserved for 5:30 PM. "
                            "Please arrive at the pro shop 15 minutes early. Enjoy your round!"),
        "free_time":       ("Great news! May I suggest a spa treatment, a twilight golf session, "
                            "or a sauna session to recharge? I can arrange any of these right away."),
        "sauna":           ("Of course! I'll reserve your sauna session at 5:15 PM. "
                            "Shall I also arrange a quiet dinner table for afterwards?"),
        "spa":             ("Wonderful choice! Would you prefer a massage, a facial, "
                            "or shall I recommend our signature Rosewood ritual?"),
        "flight_delay":    ("I'm sorry to hear that! I've arranged a late checkout, rebooked your shuttle, "
                            "and placed a hold on your spa appointment. Enjoy a leisurely breakfast -- we'll handle everything."),
        "closing":         "Thank you for choosing Rosewood Hotels. Have a wonderful stay!",
    }

    def _hotel_intent(text: str):
        t = text.lower().strip()
        n = t.rstrip(".!?,—")

        if n in _CLOSING_SET:
            return _INTENTS["closing"]
        if any(k in t for k in ["flight delay", "flight's delay", "flight is delay",
                                 "delayed", "missed flight", "late checkout"]):
            return _INTENTS["flight_delay"]
        if any(k in t for k in ["done with meeting", "done early", "free time",
                                 "finished early", "meetings done", "done with work"]):
            return _INTENTS["free_time"]
        if any(k in t for k in ["anniversary", "somewhere nice", "special occasion",
                                 "celebrate", "celebration", "romantic"]):
            return _INTENTS["anniversary"]
        if "sauna" in t:
            return _INTENTS["sauna"]
        if any(k in t for k in ["spa", "massage", "facial", "treatment"]):
            return _INTENTS["spa"]
        if any(k in t for k in ["early", "morning", "dawn", "tomorrow"]) and \
           any(k in t for k in ["golf", "tee", "round"]):
            return _INTENTS["golf_early"]
        if any(k in t for k in ["golf", "tee time", "tee-time"]):
            return _INTENTS["golf"]
        if any(k in t for k in ["checking in", "check in", "check-in", "i arrive", "arriving"]):
            return _INTENTS["checkin"]
        if any(k in t for k in ["espresso", "croissant", "breakfast", "room service",
                                 "send up", "bring up", "order food", "order coffee"]):
            return _INTENTS["room_service"]
        if any(k in t for k in ["dinner", "restaurant", "dining", "table for", "eat tonight"]):
            if "bemelmans" in t:
                return _INTENTS["dinner_bemelmans"]
            if "dowling" in t or "carlyle" in t:
                return _INTENTS["dinner_dowlings"]
            return _INTENTS["dinner_generic"]
        return None

    def _is_closing(text: str) -> bool:
        """
        Detect intent to end the call. Previous version required exact set match,
        so "Okay, I'm done." wouldn't trigger because of the "Okay, " prefix
        (only "i'm done" alone matched). Now we also do substring matching for
        the most reliable closing phrases.
        """
        t = text.lower().strip().rstrip(".!?,")
        if t in _CLOSING_SET:
            return True
        # Substring match for high-confidence closing phrases.
        # Avoid single-token matches ("done") to prevent false positives like
        # "I'm done thinking" / "almost done choosing".
        STRONG_CLOSINGS = (
            "i'm done", "i am done", "i'm finished", "i am finished",
            "i'm all set", "i am all set", "that's all", "that's it",
            "that will be all", "that would be all", "nothing else",
            "place my order", "place the order", "complete my order",
            "complete the order", "checkout", "check out",
        )
        return any(c in t for c in STRONG_CLOSINGS)

    async def process_question(text: str):
        nonlocal tts_task, filler_task, session

        session.turns += 1
        t = text.lower().strip()

        if tts_task and not tts_task.done():
            stop_tts.set()
            tts_task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(tts_task), timeout=0.3)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
        stop_tts.clear()

        if _is_closing(text) or session.state == CallState.CLOSING:
            session.state = CallState.CLOSING
            # If the caller has items in their cart, finalize the order first —
            # this saves the Order row and SMSes the PayPal checkout link.
            cart = cart_svc.get(call_session_id)
            if cart and cart.items:
                confirmation = finalize_order(
                    restaurant_id=rest.id if rest else 1,
                    session_id=call_session_id,
                )
                print(f"[ws] order finalized on close: {confirmation[:80]}")
                tts_task = asyncio.create_task(send_tts(confirmation))
            else:
                tts_task = asyncio.create_task(
                    send_tts("Thank you for calling. Goodbye!"))
            return

        hotel_reply = _hotel_intent(text)
        if hotel_reply:
            print(f"[ws] hotel intent ({session.state.value}): {repr(text[:50])}")
            if any(k in t for k in ["golf", "tee", "dinner", "restaurant", "sauna",
                                     "spa", "massage", "room service", "breakfast",
                                     "flight", "checkout"]):
                session.state = CallState.ITEM_SELECTED
                session.items.append(t)
            elif any(k in t for k in ["checking in", "check in", "arriving"]):
                session.state = CallState.BROWSING
            filler_key = session.state.value if session.state.value in _FILLERS else "default"
            if _filler_cache:
                filler_task = asyncio.create_task(send_filler(filler_key))
                await asyncio.sleep(0.05)
            tts_task = asyncio.create_task(send_tts(hotel_reply))
            return

        if not menu_text:
            tts_task = asyncio.create_task(
                send_tts("Our menu isn't available right now. Please call back soon."))
            return

        session.state = CallState.BROWSING if session.state == CallState.GREETING else session.state
        if session.state == CallState.GREETING:
            session.state = CallState.BROWSING

        filler_key = session.state.value if session.state.value in _FILLERS else "default"
        if _filler_cache:
            stop_tts.clear()
            filler_task = asyncio.create_task(send_filler(filler_key))
            await asyncio.sleep(0.05)

        result = _cached_rec(menu_text, text, session.state, session.language,
                             rest.id if rest else 1, call_session_id,
                             history=session.history)
        rec    = result["recommendation"]
        # Append this turn to history so the NEXT user message has context
        # ("yes"/"sure"/"add it" need the prior assistant reply to make sense)
        session.history.append({"role": "user",      "content": text})
        session.history.append({"role": "assistant", "content": rec})
        # Trim to last 6 turns = 12 messages — keeps token cost bounded
        if len(session.history) > 12:
            session.history = session.history[-12:]
        print(f"[claude] {result['claude_latency_ms']}ms -> {rec[:60]}")

        # Wait for the filler task to finish (don't cut it mid-word — that's
        # what made the audio sound glitchy before). If LLM was fast (<800ms),
        # the filler was never started (it sleeps 800ms first), so this is a
        # no-op. If LLM was slow, the filler is mid-play and we wait for it
        # to finish naturally — then TTS plays cleanly afterwards.
        if filler_task and not filler_task.done():
            try:
                await asyncio.wait_for(filler_task, timeout=3.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        stop_tts.clear()

        if session.state == CallState.BROWSING:
            session.state = CallState.ITEM_SELECTED
            suffix = " Anything else?"
        elif session.state == CallState.ITEM_SELECTED:
            session.state = CallState.UPSELL
            suffix = ""
        else:
            suffix = ""

        db = SessionLocal()
        try:
            log = CallLog(
                restaurant_id     = rest.id if rest else None,
                call_type         = "voice",
                caller_number     = to_number,
                transcript        = text,
                recommendation    = rec,
                claude_latency_ms = result["claude_latency_ms"],
                total_latency_ms  = result["claude_latency_ms"],
                noise_flag        = result["noise_flag"],
                status            = "ok",
            )
            db.add(log); db.commit()
        finally:
            db.close()

        tts_task = asyncio.create_task(send_tts(rec + suffix))

    stt_task = None

    try:
        start_received = False
        while not start_received:
            try:
                raw = await asyncio.wait_for(ws.receive(), timeout=30.0)
            except asyncio.TimeoutError:
                print("[ws] timeout waiting for start")
                return

            if raw["type"] == "websocket.disconnect":
                print("[ws] disconnected before start")
                return

            text = raw.get("text", "")
            if not text:
                continue

            data  = json.loads(text)
            event = data.get("event", "")
            print(f"[ws] event: {event}")

            if event == "connected":
                print(f"[ws] Plivo connected -- waiting for start")

            elif event == "start":
                info        = data.get("start", {})
                to_number   = info.get("to", to_number) or to_number
                # Plivo's start event omits `from` — rely on the query param set by voice_inbound
                from_number = from_query or info.get("from", "") or ""
                rest, menu_text = _load_menu(to_number)
                if rest:
                    rest_name = rest.name
                # Pre-create the cart so complete_order can SMS this caller a checkout link
                if from_number:
                    cart_svc.get_or_create(call_session_id, rest.id if rest else 1).caller_phone = from_number
                print(f"[ws] call started to={to_number} from={from_number} rest={rest_name}")
                start_received = True

        if not _filler_cache and settings.elevenlabs_api_key:
            asyncio.create_task(_warm_filler_cache(_eleven_chunks))

        # ── Google Speech streaming setup ─────────────────────────────────
        audio_queue      = asyncio.Queue(maxsize=1000)  # Plivo audio → Google
        transcript_queue = asyncio.Queue()              # Google results → handler
        main_loop        = asyncio.get_event_loop()
        stt_stop         = threading.Event()

        def _google_stt_thread():
            """
            Runs Google Speech gRPC streaming in a background thread.
            Google's streaming API kills the stream after ~5 min of silence OR
            after ~5 min total. We auto-reconnect on Audio Timeout errors so
            the call can continue. (Saw `400 Audio Timeout Error: Long duration
            elapsed without audio` mid-call breaking STT permanently.)
            """
            stt_cfg = _build_stt_config(rest.id if rest else 1, menu_text)
            n_hints = len(stt_cfg.config.speech_contexts[0].phrases) if stt_cfg.config.speech_contexts else 0
            print(f"[stt] Google Speech connected (model=phone_call, enhanced=True, hints={n_hints})")

            def _audio_gen():
                # Pump silence frames (mulaw 0xFF) every 100ms when no real audio
                # arrives. This keeps Google's stream alive across user pauses
                # without hitting the Audio Timeout error.
                last_send = main_loop.time() if main_loop else 0
                while not stt_stop.is_set():
                    fut = asyncio.run_coroutine_threadsafe(
                        asyncio.wait_for(audio_queue.get(), timeout=0.5),
                        main_loop,
                    )
                    try:
                        chunk = fut.result(timeout=1.0)
                        if chunk is None:   # sentinel — stop stream
                            return
                        yield google_speech.StreamingRecognizeRequest(audio_content=chunk)
                    except Exception:
                        if stt_stop.is_set():
                            return
                        # Keep-alive: pump 100ms of mulaw silence
                        yield google_speech.StreamingRecognizeRequest(
                            audio_content=b"\xff" * 800
                        )

            reconnect_attempts = 0
            while not stt_stop.is_set():
                try:
                    client = _make_speech_client()
                    for response in client.streaming_recognize(stt_cfg, _audio_gen()):
                        for result in response.results:
                            if not result.alternatives:
                                continue
                            transcript = result.alternatives[0].transcript.strip()
                            is_final   = result.is_final
                            lang = (result.language_code or "en").split("-")[0].lower()
                            asyncio.run_coroutine_threadsafe(
                                transcript_queue.put((transcript, is_final, lang)),
                                main_loop,
                            )
                    # Stream ended cleanly — reconnect if call is still active
                    if stt_stop.is_set():
                        return
                    reconnect_attempts += 1
                    print(f"[stt] stream ended; reconnecting (attempt {reconnect_attempts})")
                except Exception as e:
                    if stt_stop.is_set():
                        return
                    reconnect_attempts += 1
                    print(f"[stt] error: {e}; reconnecting (attempt {reconnect_attempts})")
                    if reconnect_attempts > 5:
                        print(f"[stt] giving up after 5 reconnect attempts")
                        return

        stt_thread = threading.Thread(target=_google_stt_thread, daemon=True)
        stt_thread.start()

        # ── FINAL debounce ────────────────────────────────────────────────────
        # Google STT fires "FINAL" on any pause >~300ms. So utterances like
        # "and uh, [pause] how about a coffee" arrive as TWO finals. We were
        # firing GPT on each one → wasted round-trip + GPT confusion.
        # Strategy: after a FINAL arrives, wait ~400ms. If another FINAL
        # arrives in that window, concatenate. Then fire ONE process_question.
        pending_final     = {"text": "", "task": None}
        FINAL_DEBOUNCE_MS = 400

        async def _flush_pending_final():
            await asyncio.sleep(FINAL_DEBOUNCE_MS / 1000.0)
            text = pending_final["text"].strip()
            pending_final["text"] = ""
            pending_final["task"] = None
            if not text:
                return
            tokens = [w.strip(".,!?¿¡") for w in text.lower().split()]
            # Skip if all noise OR a single noise/filler word
            if all(t in _NOISE_WORDS for t in tokens) or (len(tokens) == 1 and tokens[0] in _NOISE_WORDS):
                print(f"[stt] noise skipped: {repr(text)}")
                return
            # Skip ultra-short connector-only phrases ("and uh", "um yeah", "well so")
            CONNECTORS = {"and", "but", "so", "or", "well", "ok", "okay", "yeah", "uh", "um", "hmm"}
            if len(tokens) <= 2 and all(t in CONNECTORS or t in _NOISE_WORDS for t in tokens):
                print(f"[stt] connector-only skipped: {repr(text)}")
                return
            asyncio.create_task(process_question(text))

        async def stt_reader():
            while True:
                transcript, is_final, lang = await transcript_queue.get()

                # Live language update
                if lang and lang != session.language:
                    session.language = lang
                    print(f"[stt] language switched: {lang}")

                if not transcript:
                    continue

                print(f"[stt] {'FINAL' if is_final else 'partial'}: {repr(transcript)}")

                if tts_active:
                    stop_tts.set()

                if is_final:
                    # Cancel any pending flush, append this transcript, reschedule
                    if pending_final["task"] and not pending_final["task"].done():
                        pending_final["task"].cancel()
                    pending_final["text"] = (
                        (pending_final["text"] + " " + transcript).strip()
                        if pending_final["text"] else transcript
                    )
                    pending_final["task"] = asyncio.create_task(_flush_pending_final())

        stt_task = asyncio.create_task(stt_reader())

        while True:
            try:
                raw = await asyncio.wait_for(ws.receive(), timeout=120.0)
            except asyncio.TimeoutError:
                print("[ws] idle timeout")
                break

            if raw["type"] == "websocket.disconnect":
                print("[ws] Plivo disconnected")
                break

            text = raw.get("text", "")
            if not text:
                continue

            data  = json.loads(text)
            event = data.get("event", "")

            if event == "media":
                audio = base64.b64decode(data["media"]["payload"])
                try:
                    audio_queue.put_nowait(audio)
                except asyncio.QueueFull:
                    pass   # drop frame if backed up

            elif event == "stop":
                print("[ws] call stopped")
                break

    except WebSocketDisconnect:
        print("[ws] WebSocketDisconnect")
    except Exception as e:
        print(f"[ws] error: {e}")
    finally:
        # Safety net: if the call drops while items are still in the cart
        # (caller hung up before saying "I'm done", or barge-in / hallucination
        # bypassed the closing flow), finalize the order automatically so the
        # caller still gets their PayPal SMS link. Without this, the customer
        # has paid no money AND received no notification — worst outcome.
        try:
            _final_cart = cart_svc.get(call_session_id)
            if _final_cart and _final_cart.items:
                print(f"[ws] auto-finalizing order on disconnect ({len(_final_cart.items)} items in cart)")
                confirmation = finalize_order(
                    restaurant_id=rest.id if rest else 1,
                    session_id=call_session_id,
                )
                print(f"[ws] auto-finalize result: {confirmation[:100]}")
        except Exception as e:
            print(f"[ws] auto-finalize failed: {e}")

        stt_stop.set()
        try:
            audio_queue.put_nowait(None)   # sentinel to unblock generator
        except Exception:
            pass
        if stt_task:
            stt_task.cancel()
        if tts_task and not tts_task.done():
            tts_task.cancel()
        print("[ws] cleaned up")
