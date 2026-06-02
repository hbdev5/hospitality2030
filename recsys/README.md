# RecsYS — Voice / SMS / Browser AI Ordering + VIP + Self-Running Marketing

FastAPI service powering an AI ordering concierge for restaurants, plus VIP
memberships and a self-running weekly marketing studio. Demo tenant: **Prime House**.

> Python port of the well-tested Java/Tomcat logic. Three channels — **phone**
> (Plivo WS → Google STT), **browser voice/kiosk**, and **SMS** — share ONE
> pipeline (`recommender.get_recommendation`): per-session cart, current-item
> state machine, modifier fast-path, required-modifier prompting.

## Features
- **Ordering** — menu-grounded, required modifier configuration, PayPal checkout links.
- **VIP memberships** — text "VIP" → PayPal subscription → member card (HTML flip
  card, QR → verify page) → once-a-day redeem; per-visit benefits (free perk +
  condition-aware discount, e.g. *10% on weekdays*) auto-applied at checkout.
- **AI Studio** (`/agentstudio`) — agent launcher (Text / Phone / Kiosk / Marketing).
- **Self-Running Marketing** (`/srm`) — weekly menu-driven VIP email campaign the
  owner edits/approves; 3 selectable email styles; simulated send (SMTP drop-in).

## Stack
FastAPI · SQLAlchemy (MySQL) · OpenAI gpt-4o-mini · Google Cloud STT · ElevenLabs
TTS · Plivo (voice/SMS) · PayPal (Orders v2 + Subscriptions) · Pillow/qrcode.

## Run locally
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # fill in keys; provide google-credentials.json
uvicorn app.main:app --reload --port 5000 --root-path /recsys
```

## Layout
```
app/main.py          FastAPI app + page routes
app/config.py        env-driven settings
app/database.py      SQLAlchemy models (menu, orders, vip_*, playbooks)
app/routers/         voice_ws, voice_web, plivo_hooks (SMS), text_chat,
                     checkout, vip_web, admin_vip, srm, menu, metrics
app/services/        recommender, menu_cache, cart, tts, paypal, vip,
                     vip_card, emailer, srm
templates/           kiosk, agentstudio, text_chat, checkout, vip_*, srm_*
scripts/migrate_menu.py   import a merchant's menu into the structured tables
deploy/              systemd unit + reverse-proxy conf
```

See **HANDOVER.md** for architecture, design decisions, and the multi-tenancy
port plan. Secrets (`.env`, `google-credentials.json`) are git-ignored — never commit them.
