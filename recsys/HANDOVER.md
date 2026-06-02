# RecsYS Handover — May 2026

## What This Is
Voice + SMS + Browser-Kiosk AI ordering concierge for restaurants.
Active demo client: **Curry Bliss** (structured menu migrated from the Java
production DB — 25 items, 10 categories, 8 modifier groups). Being prepared
for a demo to podcaster Robert Scoble — conversation smoothness is the bar.

- Live phone: **+1 (646) 440-8480**
- Kiosk / browser voice UI: **https://support.hostbuddy.io/recsys/kiosk** (alias of `/voice-test`)
- Menu upload / dashboard: **https://support.hostbuddy.io/recsys/**
- **Agent Studio console: https://support.hostbuddy.io/recsys/agentstudio**
- **Text-to-Order (browser SMS, owner testing): https://support.hostbuddy.io/recsys/text**
- **VIP signup page: https://support.hostbuddy.io/recsys/vip/subscribe/{session}**

> **3 channels share ONE pipeline.** Phone (Plivo WS), browser voice, and SMS
> all route through `recommender.get_recommendation()` with per-session cart +
> history + the modifier fast-path + the current-item state machine. Fix logic
> once and all three channels benefit.

---

## Access Everything

### VM (Azure)
```bash
ssh -i ~/work/ssh-keys/HB-New_key.pem azureuser@20.127.222.82
```
- IP: `20.127.222.82`
- SSH key: `~/work/ssh-keys/HB-New_key.pem`
- Code on VM: `/home/azureuser/work/recsys`
- Code local: `/Users/sagar/work/handover/recsys`

### GitLab (Java Tomcat source — the reference architecture)
```bash
git clone git@gitlab.com:hostbuddy-ai/hostbuddy.git
# SSH key: ~/.ssh/gitlab_ed25519  (already in ~/.ssh/config)
```
Local Java source: `/Users/sagar/work/handover/src` — MenuCache.java,
OpenAIOrchestrator.java, CallSession.java, SpeechHintBuilder.java,
ConversationContext.java. This is the well-tested logic the Python port copies.

### Credentials files (local, DO NOT COMMIT)
- All API keys: `/Users/sagar/work/handover/recsys/.env`
- Infra / SSH / URLs: `/Users/sagar/work/handover/recsys/.infra`
- VM live env: `/home/azureuser/work/recsys/.env`
- Google creds JSON: `~/work/handover/recsys/google-credentials.json`
  and `/home/azureuser/work/recsys/google-credentials.json`

> `.env` holds LIVE keys (OpenAI, Anthropic, ElevenLabs, Plivo, Deepgram,
> PayPal sandbox) + DB creds. Never commit. The Java prod DB (`apppod_beta`)
> uses `root` / `change12@` on localhost — only reachable on the VM.

---

## API Keys Checklist

| Service | Env var | Notes |
|---------|---------|-------|
| OpenAI gpt-4o-mini | `OPENAI_API_KEY` | server-side tool calling, MAX_TOOL_LOOPS=2, max_tokens=120 |
| Anthropic Claude (fallback) | `ANTHROPIC_API_KEY` | |
| Google Cloud STT | `GOOGLE_APPLICATION_CREDENTIALS` → JSON on VM | gRPC streaming (phone) + REST (browser) |
| ElevenLabs TTS | `ELEVENLABS_API_KEY` | `eleven_turbo_v2_5` (TURBO tier, not premium) |
| Plivo (phone + SMS) | `PLIVO_AUTH_ID` / `PLIVO_AUTH_TOKEN` / `PLIVO_NUMBER` | 10DLC; SMS spam filter is touchy (see below) |
| Deepgram (unused, kept) | `DEEPGRAM_API_KEY` | |
| PayPal (sandbox) | in `.env` | Orders v2 |
| MySQL | `DB_URL` | db=recsys user=recsys pass=recsys2026 |

---

## Architecture

```
Browser / Phone / SMS
      │
Apache2 (SSL reverse proxy — support.hostbuddy.io; strips /recsys/ prefix)
      │
  /recsys → FastAPI :5000          /appvoyage → Tomcat :8080
  (Python voice AI)                 (Java: merchant dashboard, Square/Clover
                                     sync, OAuth, cart, SMS — the reference)
      │
  MySQL  recsys db (recsys app)  +  apppod_beta db (Java prod, source of menu)
```

### Python service layout (`/home/azureuser/work/recsys/`)
```
app/main.py                FastAPI app; routes incl. /kiosk + /voice-test (same handler)
app/database.py            SQLAlchemy models incl. 5 STRUCTURED MENU tables (see below)
app/routers/
  voice_web.py    POST /api/voice/web/chat  GET /api/voice/web/greet — browser VAD → Google STT REST → pipeline → ElevenLabs
  voice_ws.py     WS  voice stream          — Plivo mulaw → Google STT gRPC (phone_call+use_enhanced) → pipeline → ElevenLabs
  plivo_hooks.py  POST inbound call/SMS webhooks — SMS uses the unified pipeline
  checkout.py     GET /checkout/{order_id}  + PayPal create/capture — auto-capture on ?paypal=1 return
  text_chat.py    POST /api/text/chat       — browser SMS-like channel (owner testing); unified pipeline + VIP
  vip_web.py      VIP signup + capture + /vip/verify (redeem) + /vip/preview + /vip/join
  admin_vip.py    /api/vip/config + /api/admin/vip/setup|card|logo — admin VIP plan + card editor
  srm.py          Self-Running Marketing — generate/current/update/approve/menu-items + /srm pages
  menu.py         menu upload/list          metrics.py  dashboards
app/services/
  recommender.py  OpenAI gpt-4o-mini; modifier FAST-PATH before LLM; current-item state machine; subscribe_vip tool; VIP per-visit benefit
  menu_cache.py   queries STRUCTURED tables; match_utterance_to_modifiers() (negation-aware); get_item_modifier_groups()
  cart.py         per-session cart w/ current_item_id state; modifier-aware total(); smart merge
  tts.py          ElevenLabs eleven_turbo_v2_5
  paypal.py       create_order_with_link() → approval URL; Subscriptions API (ensure_subscription_plan, create/get_subscription)
  vip.py          VIP program lookup, pitch, subscriber state, keyword_intent
  vip_card.py     Pillow member-card PNG (front+back, QR→verify); merchant-configurable look&feel
  srm.py          weekly campaign generation (menu-grounded), enrichment, send
  emailer.py      stdlib SMTP sender (provider-agnostic); simulate if unconfigured
scripts/
  migrate_menu.py apppod_beta (Curry Bliss merchant 51934) → recsys structured tables (idempotent)
templates/
  voice_test.html KIOSK UI — metallic bezel, orange bar, 60/40 voice+receipt grid, PLACE ORDER
  checkout.html   receipt (line items + modifiers), PayPal Smart Buttons, sandbox creds
  agentstudio.html Agent Studio console (route /agentstudio) — chat + 9-card launcher, SVG icons
  text_chat.html  Text-to-Order browser chat (route /text)
  vip_subscribe.html VIP signup — PayPal subscription buttons
```

### Systemd
```bash
sudo systemctl restart recsys
sudo journalctl -u recsys -f --no-pager
```

---

## Structured Menu (replaced PDF-regex approach)

5 tables in `app/database.py`:
- `menu_categories` (id, restaurant_id, name, sort_order, external_id)
- `menu_items` (id, restaurant_id, category_id→cat, name, display_name, price_cents, description, external_id)
- `menu_modifier_groups` (id, restaurant_id, name, selection_type, min_select, max_select, external_id)
- `menu_modifier_options` (id, group_id→grp, name, price_cents, external_id)
- `menu_item_modifier_groups` (item_id, group_id — composite PK link table)

`external_id` preserves the source IDs from the Java DB so re-migration is idempotent.

On startup `menu_cache` logs e.g.
`[menu_cache] loaded structured menu for restaurant 1: 25 items, 10 categories, 8 mod groups`.

### Re-run / refresh the menu
```bash
ssh -i ~/work/ssh-keys/HB-New_key.pem azureuser@20.127.222.82
cd /home/azureuser/work/recsys && python3 scripts/migrate_menu.py
# Wipes restaurant 1's menu, re-imports Curry Bliss (apppod_beta merchant 51934),
# renames restaurant 1 → "Curry Bliss". RESET_FIRST=True makes it idempotent.
sudo systemctl restart recsys   # so menu_cache reloads
```
To migrate a different merchant: edit `SRC_MERCHANT_ID` at top of the script.

---

## The Two Key Mechanisms (copied from Java, the part that makes it work)

### 1. Current-item state machine (`cart.py`)
While a consumer is configuring a combo (e.g. a Slamburger needs protein +
sides), the cart tracks `current_item_id` / `current_item_name`. Follow-up
utterances like "with extra cheese", "avocado for protein", "sweet potato
fries" attach to that item instead of creating new line items. `clear_current()`
on commit. This fixed the "3 Lumberjack Slams" and "Sweet Potato Fries as a
separate item" bugs.

### 2. Modifier fast-path (`recommender.py`, before the LLM — ~0ms)
If `cart.current_item_id` is set and the utterance isn't a STOP_ROUTING phrase,
`menu_cache.match_utterance_to_modifiers()` resolves modifiers server-side with
**zero LLM call**. Negation is clause-boundary aware ("no bacon, and chicken"
negates only bacon — stops at comma/and/or/but). Longest-match wins. Returns
`{recommendation, claude_latency_ms:0, total, cart_items}`. End-to-end SMS test
hit 3 fast-path turns at 0.0ms for a correct $13.50 order.

`cart.total()` looks up modifier prices via `menu_cache.get_item_modifier_groups`
so totals include add-on cost (fixed the wrong-total bug).

### 3. Required-modifier configuration (Java BrowserBotTest item-config rule)
A modifier group with `min_select >= 1` is REQUIRED. The agent must collect a
choice for each required group before placing the order ("Do not invoke
place_order until item configuration is complete"). Source of truth for the
flags is the Java `COMBOITEMS` table (`required = MINMAX>0`, single-select when
`MAXIMUM==1`); `migrate_menu.py` now carries these into
`menu_modifier_groups.min_select/max_select/selection_type`.
- `menu_cache.get_item_modifier_groups` returns `required/min/max`;
  `menu_cache.unsatisfied_required_groups(rid, item, mods)` lists what's missing.
- `recommender` injects required groups into the configuring prompt AND guards
  `complete_order`: if a cart item is missing a required choice, it reminds
  ("your Slam Burger needs a side — fries, onion rings, sweet potato fries?")
  instead of completing. Curry Bliss combos require side+protein+topping+fruit.

### 4. No phantom doubles (cart.add idempotency)
Re-calling `add_to_cart` for the item the guest is already configuring (e.g. a
bare "yes") no longer bumps quantity — it's a confirmation no-op. Only a
different item or an explicit quantity>1 increases the count. (Java's
CallSession appends + relies on currentInventoryId; this is the merge-cart
equivalent.)

---

## Google Speech config (phone vs browser)

- **Phone** (`voice_ws.py`, gRPC streaming): `model="phone_call"`,
  `use_enhanced=True`, MULAW 8kHz, `speech_contexts` phrase hints built from the
  menu (ALL-CAPS headers + Title-Case items + ordering phrases, boost=15.0),
  interim_results for barge-in, auto-reconnect on Audio Timeout, NO
  `alternative_language_codes`. Audio sent as 1280-byte (160ms) mulaw frames,
  40ms lead silence, 0.140s pacing. **No mark/ack** — Plivo's Stream API does
  not echo `playedStream` (that's a Twilio feature; adding it caused 2s dead air).
- **Browser** (`voice_web.py`, REST): `model="latest_short"`,
  `language_code="en-US"` + `alternative_language_codes=["es-US","es-ES"]`,
  WEBM_OPUS, auto punctuation.

Service account: `speechapi@hostbuddy-187005.iam.gserviceaccount.com`.
Both routers explicitly load the JSON (systemd doesn't inherit env, so
`google.auth.default` would fail otherwise).

---

## PayPal Checkout Flow
1. `complete_order` (recommender) builds `settings.public_base_url/checkout/{order_id}`.
2. Reply includes that URL (SMS: inline in the text; voice: `checkout_url` in JSON).
3. `checkout.py` serves `checkout.html` — our receipt (line items + modifiers +
   total) + PayPal Smart Buttons + sandbox test creds.
4. On PayPal return (`?paypal=1` + token) the page auto-captures.

> SMS note: Plivo flags promotional-sounding text as spam (error_code 30). Keep
> SMS bodies transactional. `complete_order` skips auto-SMS for `sms-` sessions
> (the SMS reply already carries the link).

---

## VIP Membership / Subscriptions (May 2026)

Ported from the Java VIP flow (handover `src`: `BrowserBotTest.java` `subscribe_vip`
+ per-visit benefit ~line 8565, `StripeVipWebhook.java`, `VipSubscription.java`,
`VipMemberCardGenerator.java`). **Payment rail is PayPal, not Stripe** — recsys
reuses its existing PayPal sandbox instead of Stripe Connect.

**Curry Bliss plan:** $5/month → "Free drink or chips every visit" (seeded on
startup, restaurant_id=1).

Pieces:
- `app/database.py` — `VipProgram` (plan + cached PayPal product/plan ids) and
  `VipSubscriber` (active members, keyed by phone or session id). `seed_vip_program()`
  runs in `init_db()`.
- `app/services/vip.py` — `get_program`, `pitch()`, `subscribe_link()`,
  `is_subscriber`, `mark_subscriber`, and `keyword_intent()` (deterministic VIP
  keyword route shared by SMS + browser Text).
- `app/services/paypal.py` — Subscriptions API: `create_product`,
  `create_monthly_plan`, `ensure_subscription_plan` (cached on VipProgram),
  `get_subscription`, `create_subscription`. **Note:** PayPal 400s on a bad
  product `category` enum — we omit the field.
- `app/services/recommender.py` — `subscribe_vip` tool (phone/kiosk path) +
  per-visit benefit auto-applied in `complete_order`. Adds a $0 "VIP Perk: <recurring_benefit>"
  line, and a **conditional discount** line. Benefits ONLY activate after PayPal approval.
  - **Conditional discount (honest)**: `visit_discount_percent` is applied ONLY when
    `vip.discount_applies_today(discount_condition)` is true. `discount_condition` ∈
    {always, weekdays, weekends}, captured verbatim from the operator (admin chat).
    The discount is added as an explicit negative line item (`VIP 10% off (weekdays) −$X`)
    so the receipt/checkout math is transparent — not hidden in the total.
  - ⚠️ **Timezone caveat**: the weekday/weekend check uses **America/Los_Angeles**
    (`zoneinfo`). For a restaurant in another tz this is wrong near midnight — a
    per-restaurant timezone column belongs in the multi-tenant phase (see below).
  - **Verbatim benefit capture** (`admin_vip._parse_plan`): benefits are extracted as
    a VERBATIM list and REPLACE all 3 card lines; the LLM is told never to reword
    ("on weekdays" stays "on weekdays"). The setup reply echoes them back ("Saved ✓").
- `app/routers/vip_web.py` + `templates/vip_subscribe.html` —
  `GET /vip/subscribe/{session}` (PayPal subscription buttons) and
  `POST /api/vip/subscribe/capture` (verify via PayPal, persist subscriber,
  SMS a welcome).
- `app/routers/plivo_hooks.py` — VIP keyword takes top priority in the SMS pipeline.
- `app/services/vip_card.py` — Pillow port of Java `VipMemberCardGenerator`: 900×500
  front+back card, configurable accent/bg/title/subtitle/benefit/logo, phone QR on
  the back (issued cards). Writes PNG to `static/vip_cards/`, served by /static.
- `app/routers/admin_vip.py` — merchant admin: `GET /api/vip/config`,
  `POST /api/admin/vip/setup` (gpt-4o-mini parses the operator's plan sentence →
  updates program + renders card), `POST /api/admin/vip/card` (visual-editor fields),
  `POST /api/admin/vip/logo` (logo upload). On subscribe, `vip_web.vip_capture`
  renders the member's personalised card (name + phone QR) and SMSes the link.

**Admin / consumer modes (Agent Studio):** the Admin/User toggle is functional.
- **Admin:** the console becomes the VIP setup assistant ("set up a VIP plan,
  $5/month for chips or a drink for every entree") → parses → updates program →
  shows the card under the Loyalty tab with a full visual editor (title, subtitle,
  benefit×2, price, discount, accent/bg color pickers, logo upload; live preview).
- **Consumer (User):** the Loyalty tab pops PayPal subscription buttons (plan from
  `/api/vip/config`); on approval the studio shows the issued member card.

VIP look & feel is persisted on `VipProgram` (card_title, card_subtitle, benefit2,
accent_color, bg_color, logo_url, card_url) — added via `ALTER TABLE` on the VM
(SQLAlchemy `create_all` does NOT alter existing tables). `qrcode[pil]` installed
in the venv for the card QR.

**The 3 VIP entry points:**
- **SMS / browser Text:** literal "VIP" (or membership/loyalty/subscribe/perks)
  → 0ms keyword pitch + signup link.
- **Phone / kiosk voice:** the `subscribe_vip` LLM tool fires when the guest asks
  about membership; the agent pitches verbally. (The 18-word voice cap trims the
  spoken URL — phone link delivery via SMS is a TODO, see Known Issues.)

---

## Agent Studio UI (`templates/agentstudio.html`, route `/agentstudio`)

The "Console v2.0" AI Studio dashboard. Left = chat console (wired live to the
Text pipeline — type "VIP" right there) + a 9-card agent launcher; right =
System/Loyalty/Config tabs, system log, stat cards, Agent Performance, Upload.
Line-art SVG icons (no emoji) match the original mockup. Cards wired LIVE:
**Text → /text, Phone → tel:, Kiosk → /kiosk, Loyalty → /vip/subscribe/web-demo**;
Drive Through / Vision / POS Connect / Performance IQ / Multi-Modal AI are
"SOON" placeholders. The VM's existing `/` `dashboard.html` is left untouched.

---

## Self-Running Marketing (SRM) — weekly VIP email campaigns (May 2026)

**Why:** for complex menus (Cheesecake Factory / Oak & Rye), members don't know
the menu or drinks. Each week the system drafts a menu-driven VIP email built
around ONE **hero** item; the owner reviews/edits/approves in the studio; on
approval it sends to VIP subscriber emails (simulated for the pilot; real SMTP
drops in with no code change).

### Files
- `app/services/srm.py`
  - `menu_items()` — distinct REAL items (name, price, category); powers the
    owner's featured-item picker.
  - `generate(rid, featured_item=None)` — builds the campaign around a hero.
    - Hero = **owner-picked item (final say)** OR, if none, **AI-rotated across
      the restaurant's REAL menu sections** (rotates by playbook count so each
      Regenerate is a different section). **Never invents items** — hard prompt
      constraint + grounded in the structured menu (names + prices). (Earlier
      bug: rotating through cocktail/dessert sections that don't exist made the
      model fabricate "Blissful Symphony"/"Curry Mango Madness" — fixed.)
    - Output JSON: `featured{emoji,title(real item),item,category,description,
      was,now}` + 5 `secondary{emoji,title(real item),offer}`.
    - **Pricing discipline** (balanced vs the VIP monthly fee): item <$5 may be
      free/BOGO; $5–10 up to $3 off; >$10 a few $ off or 20–30%, NEVER free.
    - Stored in `Playbook` (`campaigns_json` + status DRAFT/SENT). gpt-4o-mini,
      temperature 0.95 + random seed for weekly variety.
  - `enrich_for_email()` — adds hero/secondary stock images (keyword→Unsplash
    map keyed off category + offer text), price points (was/now), short badges
    ("$5 OFF"/"FREE"/"20% OFF").
  - `update(rid, featured, secondary, style)` — owner edits + chosen style;
    resets to DRAFT (edited campaigns need re-approval).
  - `approve(rid)` — marks SENT, records subscriber/email counts, calls `_send()`.
  - `_send()` / `_email_html()` — real SMTP via `emailer.py` if configured, else
    simulate (logged). Builds the branded campaign HTML.
- `app/services/emailer.py` — stdlib `smtplib`, provider-agnostic. Env:
  `SMTP_HOST/PORT/USER/PASS/FROM`. Unset → simulate. Works with Gmail app
  password, Brevo, SendGrid SMTP. (Old Java Gmail creds are dead — Google blocks
  plain-password SMTP; needs an app password.)
- `app/routers/srm.py`:
  - `POST /api/srm/generate {featured_item?}` · `GET /api/srm/current`
  - `GET /api/srm/menu-items` · `POST /api/srm/update {featured,secondary,style}`
  - `POST /api/srm/approve`
  - `GET /srm` — owner studio: Regenerate at top, **featured-item picker**
    (Auto/AI-suggest default, owner override), elegant hero (image + Reg$/VIP$
    chip), editable offers, **3-style picker**, embedded live email preview,
    Approve & Send.
  - `GET /srm/email?style=N` — consumer email preview.
- `templates/`: `srm_studio.html`, `srm_email_1/2/3.html` (Editorial / Card-grid
  / Magazine — adapted from owner-supplied mockups).

Agent Studio: the **"Auto Marketing"** card (replaced Loyalty) links to `/srm`.
Admin chat command "marketing"/"campaign" also returns the `/srm` link.

Cadence is **on-demand** for the demo (Generate → edit → Approve). Weekly
auto-send hook: add a cron/systemd timer calling `generate()` + notify owner;
auto-send if approved (the example's "Tuesday 9 AM").

---

## Design choices & decisions (why things are the way they are)

- **Payment rail = PayPal** (reuse existing sandbox), NOT Stripe Connect. Java
  used Stripe Connect (per-merchant Express accounts); we avoided that onboarding
  for the pilot. PayPal Subscriptions API: product + monthly plan cached on
  `VipProgram`; JS buttons (`vault=true&intent=subscription`).
- **VIP data model**: purpose-built `VipProgram` + `VipSubscriber` tables instead
  of Java's shared `PromotionDetails` (type='vip'). Clearer; `VipSubscription.java`
  field semantics preserved in column names.
- **Per-visit benefit** auto-applied at `complete_order` (Java parity); **redeem**
  is once-per-calendar-day (`VipSubscriber.last_redeemed_at`).
- **Member card** = Pillow-rendered PNG to local `/static/vip_cards/` (NOT
  Cloudinary like Java); QR on the card encodes the **verify URL**; card config
  (colors/logo/3 benefits/website/validity) is merchant-editable in admin mode.
- **Verify page** (`/vip/verify/{sub_id}`) hosted on our VM: staff/member confirm
  active status, benefits, paid period, card, + once-a-day Redeem.
- **SRM**: menu-grounded generation (never invent items); **owner has final say**
  via featured-item picker (AI suggestion is the default); margin-aware discounts;
  simulate email for pilot; 3 owner-selectable email styles; images from an
  Unsplash keyword map (no per-item photos yet).
- **Channels share ONE pipeline** (`recommender.get_recommendation`): phone WS,
  browser Text (`/text`), SMS. VIP keyword fast-path on SMS+Text; `subscribe_vip`
  LLM tool on phone/kiosk.

### Latent bugs found + fixed (watch for regressions)
1. **Voice/SMS 500 → busy signal**: `plivo_hooks.get_menu_cached` cached the
   `Restaurant` ORM object; after a commit expired it + session closed, later
   webhook calls hit `DetachedInstanceError`. Fix: cache a `SimpleNamespace`
   snapshot (same pattern as `voice_web._load_menu`). NOT caused by subscriptions.
2. **"latte" not found**: `menu_cache.search_menu` ran a NAIVE substring Spanish-
   alias replace with `"te"→"tea"`, turning "latte"→"lattea". Fix: whole-word regex.
3. **Cart double-count**: Python `cart.add` bumped quantity on exact re-add; the
   LLM re-calling add_to_cart doubled orders. Aligned to Java + prompt rules
   (FINISH WORDS → complete_order; DON'T DUPLICATE).
4. **Modifier items never added**: prompt said "AT MOST ONE tool per turn" so an
   options item spent its tool on get_modifier_options and never added. Fixed +
   raised MAX_TOOL_LOOPS to 3; required-modifier prompting + completion guard.

---

## Multi-tenancy — current state, Java mapping, and port plan

**Goal:** one deployment serving many restaurants (like the Java app), each with
its own menu, VIP program, subscribers, campaigns, number, and branding.

### How Java does it (the reference)
- Tenant = a **`Store`** row; `Store.id` == `merchantId` (`mid`). Owner = an
  **`Authentication`** row (email, company, phone), linked via `Store.createdby`.
- **Tenant resolution** (`CommonWebhook`): inbound `To` number → `+<To>` →
  `select from Store where agentPhone == '<toPhone>'` → that Store's `mid`.
  Everything after is scoped by `mid`.
- All data tables are keyed by `merchantId`: `INVENTORYMERCHANT`,
  `MODIFIERMERCHANT`, `CATEGORYMERCHANT`, `PromotionDetails`, `GuestBook`, orders.
- Payments are per-merchant via **Stripe Connect** (`Store.stripeAccountId`).

### Where recsys stands today
- **Schema is already multi-tenant-ready** — every table has `restaurant_id`:
  `restaurants`, `menus`, the 5 structured-menu tables, `vip_programs`,
  `vip_subscribers`, `orders`, `playbooks`, `call_logs`.
- **Voice/SMS already resolve the tenant by number** — `plivo_hooks.get_menu_cached`
  does `Restaurant.plivo_number == To` (this is exactly Java's `Store.agentPhone`
  lookup). `restaurants` ≈ Java `Store`.
- **Gaps (what makes it single-tenant in practice):**
  1. `restaurant_id = 1` is hardcoded across the **VIP / SRM / admin** endpoints
     (`get_program(1)`, `generate(1)`, `subscriber_count(1)`, `_ensure_program` rid=1…).
  2. **Browser surfaces** (`/agentstudio`, `/text`, `/kiosk`, `/srm`, `/vip/*`) assume rid=1.
  3. Restaurant **name** is partly hardcoded in templates (now "Prime House").
  4. One shared **PayPal** sandbox account (no per-merchant payout).
  5. No **owner** record, no per-restaurant **timezone**.

### Data-model changes to port from Java
| Java | recsys today | to add for multi-tenant |
|------|--------------|--------------------------|
| `Store` (id=mid, agentPhone, phone, stripeAccountId, subscriptionId) | `restaurants` (id, name, plivo_number) | + `timezone`, + `paypal_*` (per-merchant), + `slug` (for browser routing), + `is_active` |
| `Authentication` (owner email/company/phone, Store.createdby) | — (none) | new `owners` table (or columns on `restaurants`) |
| `*MERCHANT` tables keyed by merchantId | structured-menu tables keyed by `restaurant_id` | already fine |
| `PromotionDetails` (type='vip') | `vip_programs` (restaurant_id) | already fine; + `timezone` honored for discount condition |

### Port plan (pending tasks, ordered)
1. **Tenant resolver** — `resolve_restaurant(request)`: voice/SMS by Plivo `To`
   (done — generalize into one helper); browser by **path slug** (`/r/{slug}/…`) or
   **subdomain** (`{slug}.support.hostbuddy.io`); verify/redeem by
   `member_id → subscriber.restaurant_id`.
2. **Thread `restaurant_id`** through SRM/VIP/admin endpoints — remove every
   hardcoded `1`; routes/sessions carry the tenant.
3. **De-hardcode the name** — pass the resolved restaurant name into every template
   context (kill remaining literals); drive card/email/greeting from DB.
4. **Per-tenant caches & sessions** — key `voice_web`/`text_chat` menu caches by
   restaurant (today they grab "first restaurant"); encode tenant in `session_id`
   (`r{rid}-…`) so carts/history don't collide.
5. **Add owner + timezone** — `owners` table (or columns), `restaurants.timezone`;
   use the tenant tz in `vip.discount_applies_today` (fixes the LA-only caveat).
6. **Per-merchant payments** — PayPal multiparty or **Stripe Connect** (Java's
   `Store.stripeAccountId`) so funds route to each merchant; per-merchant
   `paypal_plan_id` already cached on `vip_programs`.
7. **Onboarding** — `migrate_menu.py` already takes `SRC_MERCHANT_ID`; wrap it so a
   new merchant = create `restaurants` row + assign Plivo number (agentPhone) +
   migrate menu + seed VIP program.

---

## Kiosk UI (`templates/voice_test.html`)
Redesigned from the dark test page into an in-store kiosk per user mockup:
metallic bezel (pure CSS box-shadow, no image), orange top bar, Playfair Display
"CURRY BLISS", 60/40 grid — left = amber mic + status stack + transcript chip,
right = "YOUR ORDER" receipt with line items/modifiers, Subtotal / Tax 8.5% /
TOTAL, blue PLACE ORDER button. All original VAD / mic / STT / TTS JS preserved;
legacy element IDs kept (cartBox, cartTotal, checkoutBar, checkoutLink) plus new
ones (statusLine, statusSub, subtotalVal, taxVal, totalVal, placeOrderBtn).
`renderReceipt(items, serverTotal)` builds rows + tax. Mobile breakpoint 900px
stacks the receipt below.

---

## Deploy Any Change
```bash
# Single file
scp -i ~/work/ssh-keys/HB-New_key.pem \
  /Users/sagar/work/handover/recsys/app/services/recommender.py \
  azureuser@20.127.222.82:/home/azureuser/work/recsys/app/services/recommender.py

# Restart + tail
ssh -i ~/work/ssh-keys/HB-New_key.pem azureuser@20.127.222.82 \
  "sudo systemctl restart recsys && sudo journalctl -u recsys -n 20 --no-pager"
```

Verify local == VM for a file:
```bash
md5 -q FILE   # local (macOS)
ssh ... "md5sum FILE"   # VM (linux)
```
As of this handoff all 12 modified files match byte-for-byte on both sides.

---

## Test Scripts
- `/tmp/test_sms_flow.py` (VM) — 6-turn end-to-end SMS test through the unified
  pipeline. Confirms fast-path turns at 0.0ms and a $13.50 order with checkout
  URL. Run on VM: `python3 /tmp/test_sms_flow.py`.

---

## Java Tomcat Build (existing server — do not break)
```bash
cd /home/azureuser/work/hostbuddy   # source on VM
ant -f build-cl.xml clean && ant -f build-cl.xml build
ant -f build-cl.xml DataNucleus-Enhancer
ant -f build-cl.xml package         # → bin/appvoyage.war
ant -f build-cl.xml deploy          # → /opt/tomcat/webapps/
sudo systemctl restart tomcat
```

---

## Known Issues / Next Steps
1. **Latency on first turn** — early-call STT first-result can mis-recognize;
   phrase hints + phone_call model help. Watch for glitches between responses
   (the demo bar). Logs are timestamped — `journalctl -u recsys -f`.
2. **SMS spam filter** — keep bodies transactional; checkout link is fine.
3. **Multi-tenancy** — single-tenant in practice (rid=1 hardcoded in VIP/SRM/admin
   + browser surfaces). Schema is multi-tenant-ready. **See the dedicated
   "Multi-tenancy — current state, Java mapping, and port plan" section above** for
   the data model and ordered port-from-Java tasks.
4. **Migration script** lives in `scripts/migrate_menu.py` (was in /tmp). Edit
   `SRC_MERCHANT_ID` to onboard a different merchant.
5. **Spanish** — browser path detects es; phone path is en-only (phone_call model).
6. **Phone VIP link delivery** — the `subscribe_vip` tool pitches verbally but the
   18-word voice cap drops the spoken URL. For phone, auto-SMS the signup link
   (reuse the `complete_order` Plivo send pattern) instead of speaking it.
7. **VIP card is now an HTML/CSS flip card** (`templates/vip_card.html`, served at
   `/vip/preview` + `/vip/verify`) — steak/gold "Prime House" design, front/back
   **flip**, inline data-URI QR → verify page. The old Pillow PNG generator
   (`vip_card.py`) is superseded/unused but left in place. The card's color pickers
   in the Studio editor still save but no longer tint the fixed HTML art.
8. **PayPal subscription webhook** — capture currently relies on the browser
   `onApprove` POST. Add a `BILLING.SUBSCRIPTION.ACTIVATED` webhook as a backstop
   (Java had `StripeVipWebhook` for the close-the-browser case).
9. **SRM real email** — wire `SMTP_*` in `.env` (Gmail app password / Brevo /
   SendGrid) to flip `_send()` from simulate to real. Low volume (pilot).
10. **SRM weekly auto-send** — add a cron/systemd timer (Tuesday 9 AM) to
    generate + notify + auto-send-if-approved. On-demand only today.
11. **Per-item campaign photos** — SRM uses an Unsplash keyword map; let owners
    upload real dish photos per item for the email hero/cards.
12. **SRM is single-restaurant** (rid=1), like the rest of recsys.
13. **VIP discount timezone** — `vip.discount_applies_today` uses America/Los_Angeles
    for the weekday/weekend check. Move to a per-restaurant `timezone` column in the
    multi-tenant phase (see Multi-tenancy section).
14. **VIP discount conditions** — supports always / weekdays / weekends. Other
    conditions (specific days, happy-hour time windows, item-specific discounts) are
    not modeled yet; `discount_condition` is a single enum on `vip_programs`.
