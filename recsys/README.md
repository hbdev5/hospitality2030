# RecsYS — AI Hotel Concierge (Voice + SMS)

A real-time AI concierge for Rosewood Hotels powered by Plivo, Deepgram, Claude (Anthropic), and ElevenLabs. Handles inbound phone calls and SMS with natural language understanding, instant intent matching, and sub-2-second AI responses.

## Features

- **Bidirectional Voice Streaming** — Plivo WebSocket stream, Deepgram Nova-2 STT, ElevenLabs TTS
- **True Barge-in** — Guest can interrupt the AI mid-sentence; TTS stops within 80ms
- **Hotel Concierge Intents** — Check-in, golf, sauna, spa, dinner, flight delay, room service (static, instant)
- **Claude AI Fallback** — Room service / menu questions answered by claude-haiku-4-5 in ~1.5s
- **SMS Support** — Same intent engine over SMS with dedup to prevent Plivo retry duplicates
- **Offers Page** — Branded luxury amenities page sent on check-in SMS

## Architecture

```
Plivo (call) → /api/voice/inbound (greeting XML + Stream)
                     ↓
             /ws/voice (WebSocket)
               ├── Deepgram WS (STT, streaming)
               ├── Intent matcher (static responses, 0ms)
               └── Claude Haiku (AI response, ~1.5s) → ElevenLabs (TTS) → ffmpeg (mulaw) → Plivo

Plivo (SMS)  → /api/sms/inbound → background task → Intent/Claude → Plivo SMS reply
```

## Requirements

- Python 3.11+
- ffmpeg (for MP3 → mulaw conversion)
- MySQL 8+
- Apache2 with mod_proxy_wstunnel

## Setup

```bash
git clone <repo>
cd recsys
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env with your API keys

# Create DB
mysql -u root -p -e "CREATE DATABASE recsys_db CHARACTER SET utf8mb4;"
# Tables auto-created on startup

uvicorn app.main:app --host 127.0.0.1 --port 5000
```

## Deployment (systemd + Apache)

```bash
# Copy service file
sudo cp deploy/recsys.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable recsys
sudo systemctl start recsys

# Apache: add to your VirtualHost
# See deploy/recsys.conf
sudo a2enmod proxy proxy_http proxy_wstunnel
sudo systemctl reload apache2
```

## Plivo Webhook Configuration

| Webhook | URL |
|---------|-----|
| Voice Answer URL | `https://yourdomain.com/recsys/api/voice/inbound` |
| SMS URL | `https://yourdomain.com/recsys/api/sms/inbound` |

## Project Structure

```
recsys/
├── app/
│   ├── main.py              # FastAPI app, routes
│   ├── config.py            # Settings (pydantic-settings)
│   ├── database.py          # SQLAlchemy models
│   ├── routers/
│   │   ├── plivo_hooks.py   # Voice inbound + SMS webhooks
│   │   ├── voice_ws.py      # Bidirectional WebSocket handler
│   │   ├── menu.py          # Menu upload/management
│   │   └── metrics.py       # Call log dashboard API
│   └── services/
│       ├── recommender.py   # Claude AI integration
│       ├── tts.py           # ElevenLabs REST TTS (for SMS/fallback)
│       └── pdf_parser.py    # Menu PDF parser
├── templates/               # Jinja2 HTML templates
├── static/                  # Audio cache, assets
├── deploy/
│   ├── recsys.service       # systemd unit
│   └── recsys.conf          # Apache proxy config
├── requirements.txt
└── .env.example
```

## Key Implementation Notes

- `<Stream>` in Plivo XML is non-blocking — must follow with `<Wait length="3600"/>` to keep call alive
- ElevenLabs `eleven_turbo_v2` always returns MP3; convert via ffmpeg to mulaw 8000Hz for Plivo
- Barge-in: set `tts_active=True` only after chunks collected (not during ElevenLabs preflight) to avoid false interrupts
- Deepgram `endpointing=600` (600ms silence) balances responsiveness vs. premature cutoff
- SMS dedup: return 200 immediately, process in BackgroundTask, cache `(caller, md5(body))` for 60s
