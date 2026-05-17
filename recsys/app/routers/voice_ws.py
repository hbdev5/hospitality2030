"""
Plivo bidirectional Stream WebSocket handler.

Protocol:
  Plivo → us: JSON text frames  {event: connected|start|media|stop}
  Us → Plivo: JSON text frames  {event: playAudio|clearAudio}

Sequence:
  1. Accept WS
  2. Wait for Plivo "connected" then "start" events
  3. Connect Deepgram streaming WS
  4. Forward media frames to Deepgram
  5. On Deepgram speech_final → Claude → ElevenLabs response
  6. Barge-in: interim transcript while TTS active → clearAudio + cancel

NOTE: Greeting is played by the inbound XML <Play> before Stream starts.
"""

import asyncio, json, base64, time, hashlib
from types import SimpleNamespace
import httpx, websockets
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState
from app.config import get_settings
from app.database import SessionLocal, Restaurant, Menu, CallLog
from app.services.recommender import get_recommendation

router   = APIRouter()
settings = get_settings()

DEEPGRAM_WS_URL = (
    "wss://api.deepgram.com/v1/listen"
    "?encoding=mulaw&sample_rate=8000"
    "&model=nova-2&smart_format=true"
    "&interim_results=true&endpointing=600"
)

_menu_cache:   dict = {}
_claude_cache: dict = {}


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


def _cached_rec(menu_text: str, question: str) -> dict:
    key = hashlib.md5(f"{menu_text[:200]}:{question.lower().strip()}".encode()).hexdigest()
    if key in _claude_cache:
        print(f"[claude] cache hit: {repr(question)}")
        return _claude_cache[key]
    result = get_recommendation(menu_text, question)
    _claude_cache[key] = result
    return result


async def _eleven_chunks(text: str):
    """Get ElevenLabs MP3, convert to mulaw 8000Hz via ffmpeg, yield 640-byte chunks."""
    api_key  = settings.elevenlabs_api_key
    voice_id = settings.elevenlabs_voice_id
    if not api_key:
        return

    # ElevenLabs eleven_turbo_v2 always returns MP3 regardless of output_format.
    # Fetch the full MP3, then convert to raw mulaw 8000Hz using ffmpeg.
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            headers={"xi-api-key": api_key, "Content-Type": "application/json"},
            json={
                "text": text,
                "model_id": "eleven_turbo_v2",
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
            },
        )
        mp3_data = resp.content

    if not mp3_data:
        print("[tts] ElevenLabs returned empty response")
        return

    # Convert MP3 → raw mulaw 8000Hz mono via ffmpeg
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

    print(f"[tts] converted {len(mp3_data)}b MP3 → {len(ulaw_data)}b mulaw")

    # Yield in 640-byte chunks (80ms per chunk at 8000 Hz)
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

    to_number  = settings.plivo_number
    rest       = None
    menu_text  = None
    rest_name  = "the restaurant"
    tts_active = False
    stop_tts   = asyncio.Event()
    tts_task   = None

    async def safe_send(payload: dict):
        try:
            if _ws_open(ws):
                await ws.send_json(payload)
        except Exception as e:
            print(f"[ws] send error: {e}")

    async def send_tts(text: str):
        nonlocal tts_active
        stop_tts.clear()
        # NOTE: tts_active stays False during ElevenLabs download + ffmpeg.
        # Barge-in is only enabled once we actually start sending audio chunks,
        # so delayed Deepgram partials from the user's own question don't kill
        # the response before it even plays.
        print(f"[tts] fetching: {text[:60]}")
        try:
            # Collect all mulaw chunks before enabling barge-in
            chunks = []
            async for chunk in _eleven_chunks(text):
                chunks.append(chunk)

            if not chunks or not _ws_open(ws):
                return

            # Audio is ready — NOW enable barge-in
            tts_active = True
            print(f"[tts] playing {len(chunks)} chunks")
            for chunk in chunks:
                if stop_tts.is_set() or not _ws_open(ws):
                    await safe_send({"event": "clearAudio"})
                    print("[tts] barge-in — stopped")
                    return
                await safe_send({
                    "event": "playAudio",
                    "media": {
                        "contentType": "audio/x-mulaw",
                        "sampleRate": 8000,
                        "payload": base64.b64encode(chunk).decode(),
                    },
                })
                # Pace to real-time: 640 bytes @ 8kHz = 80ms/chunk
                await asyncio.sleep(0.075)
        except asyncio.CancelledError:
            await safe_send({"event": "clearAudio"})
            print("[tts] cancelled — clearAudio sent")
        finally:
            tts_active = False

    # ── Hotel concierge intents + closing detection ───────────────────────────
    _CLOSING_SET = {
        "i'm done", "i am done", "that's all", "that's everything", "that's it",
        "nothing else", "no thanks", "no thank you", "thank you", "thanks",
        "goodbye", "bye", "bye bye", "i'm good", "i am good", "i'm all set",
        "i am all set", "i'm set", "i am set", "all good", "perfect",
        "great thanks", "great thank you", "sounds good", "that will be all",
    }

    _INTENTS = {
        "checkin":         ("Welcome to Rosewood! May I suggest a relaxing sauna at 5:15 PM to unwind? "
                            "I can also hold a dinner table at Bemelmans Bar or Dowling's at the Carlyle tonight."),
        "room_service":    ("Of course! Your order is on its way. "
                            "Shall I add a fresh orange juice to complete your morning?"),
        "anniversary":     ("How special! I'll reserve the window table at Dowling's at the Carlyle for this evening. "
                            "Shall I arrange a bottle of Barolo to make it truly memorable?"),
        "dinner_bemelmans":("Perfect! Dinner for two reserved at Bemelmans Bar at 7 PM. "
                            "Nightly jazz, iconic murals — a wonderful choice. Enjoy!"),
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
                            "and placed a hold on your spa appointment. Enjoy a leisurely breakfast — we'll handle everything."),
        "closing":         "Thank you for choosing Rosewood Hotels. Have a wonderful stay!",
    }

    def _hotel_intent(text: str) -> str | None:
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
        return text.lower().strip().rstrip(".!?,") in _CLOSING_SET

    async def process_question(text: str):
        nonlocal tts_task
        # Cancel any in-flight TTS properly.
        if tts_task and not tts_task.done():
            stop_tts.set()
            tts_task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(tts_task), timeout=0.3)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
        stop_tts.clear()

        # Closing + hotel intents — static responses, no Claude needed
        hotel_reply = _hotel_intent(text)
        if hotel_reply:
            print(f"[ws] hotel intent: {repr(text[:50])}")
            tts_task = asyncio.create_task(send_tts(hotel_reply))
            return

        if not menu_text:
            tts_task = asyncio.create_task(
                send_tts("The menu is not available right now."))
            return

        result = _cached_rec(menu_text, text)
        rec    = result["recommendation"]
        print(f"[claude] {result['claude_latency_ms']}ms → {rec[:60]}")

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

        tts_task = asyncio.create_task(send_tts(rec + " Anything else?"))

    # ── main receive loop ──────────────────────────────────────────────────────
    dg_ws  = None
    dg_task = None

    try:
        # Wait for Plivo's "connected" then "start" events
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
                print(f"[ws] Plivo connected — waiting for start")

            elif event == "start":
                info      = data.get("start", {})
                to_number = info.get("to", to_number) or to_number
                rest, menu_text = _load_menu(to_number)
                if rest:
                    rest_name = rest.name
                print(f"[ws] call started to={to_number} rest={rest_name}")
                start_received = True

        # Connect Deepgram streaming WS
        dg_headers = {"Authorization": f"Token {settings.deepgram_api_key}"}
        dg_ws = await websockets.connect(DEEPGRAM_WS_URL, additional_headers=dg_headers)
        print("[ws] Deepgram connected")

        async def deepgram_reader():
            async for raw_dg in dg_ws:
                try:
                    d            = json.loads(raw_dg)
                    if d.get("type") != "Results":
                        continue
                    alts         = d.get("channel", {}).get("alternatives", [{}])
                    transcript   = alts[0].get("transcript", "").strip()
                    is_final     = d.get("is_final", False)
                    speech_final = d.get("speech_final", False)

                    if not transcript:
                        continue

                    print(f"[dg] {'FINAL' if is_final else 'partial'}: {repr(transcript)}")

                    # Barge-in on any speech while TTS is playing
                    if tts_active:
                        stop_tts.set()

                    if speech_final:
                        asyncio.create_task(process_question(transcript))

                except Exception as e:
                    print(f"[dg] reader err: {e}")

        dg_task = asyncio.create_task(deepgram_reader())

        # Process remaining Plivo frames
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
                    await dg_ws.send(audio)
                except Exception as e:
                    print(f"[ws] dg send err: {e}")

            elif event == "stop":
                print("[ws] call stopped")
                break

    except WebSocketDisconnect:
        print("[ws] WebSocketDisconnect")
    except Exception as e:
        print(f"[ws] error: {e}")
    finally:
        if dg_task:
            dg_task.cancel()
        if tts_task and not tts_task.done():
            tts_task.cancel()
        if dg_ws:
            try:
                await dg_ws.close()
            except Exception:
                pass
        print("[ws] cleaned up")
