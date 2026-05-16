# Hospitality 2030 — AI Concierge

Agents for Hospitality.

We got a real phone number from Plivo and connected it to a **Master Concierge Agent** with sub-agents for room service, golf tee times, dinner reservations, spa, and sauna.

You can interrupt the conversation mid-sentence (barge-in) — for example ask *"what coffees do you have?"* while the agent is still talking.

## Try It Live

**Call or text: [(646) 440-8480](tel:+16464408480)**

Sometimes guests are too busy to call — the SMS agent works exactly the same way.

## Agent Architecture

- **Master Agent** — answers every call and text, routes to the right sub-agent instantly
- **Golf Sub-Agent** — books tee times (5:30 PM or 7:20 AM dawn round with breakfast box)
- **Dinner Sub-Agent** — reserves at Bemelmans Bar or Dowling's at the Carlyle
- **Spa / Sauna Sub-Agent** — schedules sessions, suggests add-ons
- **Room Service AI** — Claude Haiku reads the live menu and picks the best match (~1.5s)

One conversation. All hotel amenities are listening.

## Pages

| Page | Description |
|------|-------------|
| [how-it-works.html](https://hbdev5.github.io/hospitality2030/how-it-works.html) | Architecture overview |
| [recsys-demo.html](https://hbdev5.github.io/hospitality2030/recsys-demo.html) | Interactive demo — live conversation replay |

## Source Code

Full FastAPI source is in the [`recsys/`](./recsys/) folder:

```
recsys/
├── app/
│   ├── routers/plivo_hooks.py   # SMS + voice webhooks
│   ├── routers/voice_ws.py      # Bidirectional WebSocket pipeline
│   └── services/recommender.py  # Claude AI integration
├── templates/                   # HTML pages including offers page
├── deploy/                      # systemd + Apache config
├── requirements.txt
└── .env.example
```

## Stack

Plivo · Deepgram Nova-2 · Anthropic Claude Haiku · ElevenLabs Turbo v2 · FastAPI · MySQL
