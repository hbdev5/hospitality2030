from sqlalchemy import create_engine, Column, Integer, BigInteger, String, Text, Float, DateTime, Boolean, Enum, ForeignKey, Index
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from datetime import datetime
import os, sys
_APP_HOME = (os.environ.get("APP_HOME")
             or os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _APP_HOME)
from app.config import get_settings

settings = get_settings()
engine = create_engine(settings.db_url, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class Owner(Base):
    """A merchant identity (Google account). Owns one or more restaurants."""
    __tablename__ = 'owners'
    id          = Column(Integer, primary_key=True, index=True)
    google_sub  = Column(String(64), unique=True, index=True)   # stable Google user id
    email       = Column(String(255), unique=True, index=True)
    name        = Column(String(128))
    picture     = Column(String(512))
    created_at  = Column(DateTime, default=datetime.utcnow)
    last_login  = Column(DateTime)

class Restaurant(Base):
    __tablename__ = 'restaurants'
    id          = Column(Integer, primary_key=True, index=True)
    name        = Column(String(255), nullable=False)
    plivo_number= Column(String(20), unique=True)
    owner_id    = Column(Integer, index=True)            # → owners.id (tenant owner)
    slug        = Column(String(64), unique=True, index=True)  # public path-slug routing
    onboarded   = Column(Boolean, default=False)         # completed the setup wizard
    requested_number = Column(String(20))                # number awaiting admin authorization
    created_at  = Column(DateTime, default=datetime.utcnow)

class Menu(Base):
    __tablename__ = 'menus'
    id            = Column(Integer, primary_key=True, index=True)
    restaurant_id = Column(Integer, nullable=False)
    filename      = Column(String(255))
    raw_text      = Column(Text)
    items_json    = Column(Text)
    uploaded_at   = Column(DateTime, default=datetime.utcnow)

class CallLog(Base):
    __tablename__ = 'call_logs'
    id                = Column(Integer, primary_key=True, index=True)
    restaurant_id     = Column(Integer)
    call_type         = Column(String(16))   # voice / sms / web_voice / text
    caller_number     = Column(String(20))
    session_id        = Column(String(128), index=True)  # groups turns into one conversation
    transcript        = Column(Text)
    recommendation    = Column(Text)
    claude_latency_ms = Column(Float)
    total_latency_ms  = Column(Float)
    noise_flag        = Column(Boolean, default=False)
    status            = Column(String(10), default='ok')
    # Human-in-the-loop: an operator can correct the AI reply and/or leave a note
    # on any turn from the Agent Studio console (the ✏️ pencil).
    operator_reply    = Column(Text)        # operator-edited / corrected response
    operator_note     = Column(Text)        # operator comment / annotation
    reviewed_at       = Column(DateTime)    # when an operator last annotated this turn
    timestamp         = Column(DateTime, default=datetime.utcnow)

class Order(Base):
    __tablename__ = 'orders'
    id            = Column(Integer, primary_key=True, index=True)
    restaurant_id = Column(Integer, nullable=False)
    session_id    = Column(String(128))
    caller_number = Column(String(20))
    status        = Column(String(20), default='pending')   # pending/confirmed/paid/cancelled
    items_json    = Column(Text)   # JSON list of CartItem dicts
    total_cents   = Column(Integer, default=0)
    payment_provider = Column(String(20))   # 'paypal', future: 'stripe'
    payment_id       = Column(String(64))   # provider order/capture id
    paid_at          = Column(DateTime)
    created_at    = Column(DateTime, default=datetime.utcnow)

class Playbook(Base):
    __tablename__ = 'playbooks'
    id            = Column(Integer, primary_key=True, index=True)
    restaurant_id = Column(Integer)
    week_of       = Column(String(20))
    campaigns_json= Column(Text)
    status        = Column(String(20), default='DRAFT')
    created_at    = Column(DateTime, default=datetime.utcnow)


# ── Structured Menu (Java-equivalent schema) ─────────────────────────────────
# Replaces the regex-on-raw-text approach with proper categories/items/modifier-
# groups/options/item-to-group mapping. Mirrors apppod_beta.{INVENTORYMERCHANT,
# MODIFIERMERCHANT, CATEGORYMERCHANT} but with cleaner naming + an explicit
# many-to-many between items and modifier groups (Java duplicates modifier
# rows per item — we use a join table instead).

class MenuCategory(Base):
    __tablename__ = 'menu_categories'
    id            = Column(Integer, primary_key=True, index=True)
    restaurant_id = Column(Integer, nullable=False, index=True)
    name          = Column(String(128), nullable=False)
    sort_order    = Column(Integer, default=0)
    external_id   = Column(String(64))   # source-system id for syncing
    # ── Java CategoryMerchant parity ──
    alternate_name = Column(String(256))
    flag_cat       = Column(Integer, default=0)
    flag_delete    = Column(Boolean, default=False)   # soft delete

class MenuItem(Base):
    __tablename__ = 'menu_items'
    id            = Column(Integer, primary_key=True, index=True)
    restaurant_id = Column(Integer, nullable=False, index=True)
    category_id   = Column(Integer, ForeignKey('menu_categories.id'), index=True)
    name          = Column(String(256), nullable=False)
    display_name  = Column(String(256))
    price_cents   = Column(Integer, default=0)
    description   = Column(Text)
    image_url     = Column(String(512))   # dish photo shown on kiosk/ordering
    external_id   = Column(String(64), index=True)
    # ── Java InventoryMerchant parity ──
    alternate_name      = Column(String(256))   # STT / search aliases
    code                = Column(String(64))
    sku                 = Column(String(64))
    price_type          = Column(String(32))    # fixed | variable | per_unit
    unit_name           = Column(String(32))    # each | per lb | bunch | dozen | pint
    tax_id              = Column(String(64))
    tax_rate            = Column(Integer, default=0)
    is_radio            = Column(String(8))
    modifier_group_id   = Column(String(256))   # source modifier-group id(s)
    modifier_group_name = Column(String(256))
    tags                = Column(String(256))
    flag_show           = Column(Integer, default=1)   # visible on menu
    flag_inv            = Column(Integer, default=0)
    flag_delete         = Column(Boolean, default=False)  # soft delete
    modified_time       = Column(BigInteger)    # source modified epoch ms
    category      = relationship('MenuCategory', lazy='joined')

class MenuModifierGroup(Base):
    __tablename__ = 'menu_modifier_groups'
    id              = Column(Integer, primary_key=True, index=True)
    restaurant_id   = Column(Integer, nullable=False, index=True)
    name            = Column(String(128), nullable=False)   # "Sides", "Topping", "protein"
    selection_type  = Column(String(16), default='multi')   # single | multi
    min_select      = Column(Integer, default=0)
    max_select      = Column(Integer, default=99)
    external_id     = Column(String(64), index=True)
    options         = relationship('MenuModifierOption', lazy='joined',
                                   cascade='all, delete-orphan')

class MenuModifierOption(Base):
    __tablename__ = 'menu_modifier_options'
    id            = Column(Integer, primary_key=True, index=True)
    group_id      = Column(Integer, ForeignKey('menu_modifier_groups.id'), nullable=False, index=True)
    name          = Column(String(128), nullable=False)   # "fries", "Extra Cheese"
    price_cents   = Column(Integer, default=0)
    external_id   = Column(String(64))
    # ── Java ModifierMerchant parity ──
    alternate_name = Column(String(256))
    square_id      = Column(String(128))    # POS (Square) modifier id
    modifier_url   = Column(String(512))
    default_flag   = Column(String(8), default='0')   # preselected option
    flag_mod       = Column(Integer, default=0)

class MenuItemModifierGroup(Base):
    """Many-to-many between items and modifier groups."""
    __tablename__ = 'menu_item_modifier_groups'
    item_id       = Column(Integer, ForeignKey('menu_items.id'), primary_key=True)
    group_id      = Column(Integer, ForeignKey('menu_modifier_groups.id'), primary_key=True)


# ── VIP membership (subscriptions) ───────────────────────────────────────────
# Python equivalent of the Java VIP model. Java stored the program in the shared
# PromotionDetails table (type='vip') and the subscriber state across
# Authentication.subscriptionId + a GuestBook VIP entry. We split that into two
# purpose-built tables. Field names follow VipSubscription.java's semantics:
#   monthly_fee_cents      <- saleAmount        (subscription price)
#   recurring_benefit      <- freeItem          ("Free drink or chips every visit")
#   visit_discount_percent <- promotionDiscount (% off each qualifying visit)

class VipProgram(Base):
    __tablename__ = 'vip_programs'
    id                     = Column(Integer, primary_key=True, index=True)
    restaurant_id          = Column(Integer, nullable=False, index=True)
    program_name           = Column(String(64), default='VIP')
    monthly_fee_cents      = Column(Integer, default=500)        # $5.00 / month
    recurring_benefit      = Column(String(255), default='Free drink or chips every visit')
    visit_discount_percent = Column(Float, default=0.0)          # e.g. 10.0 = 10% off
    discount_condition     = Column(String(16), default='always')  # always | weekdays | weekends
    is_active              = Column(Boolean, default=True)
    # PayPal Subscriptions API ids — created once, then reused.
    paypal_product_id      = Column(String(64))
    paypal_plan_id         = Column(String(64))
    # Card look & feel (merchant-configurable in admin mode).
    card_title             = Column(String(64))                  # defaults to restaurant name
    card_subtitle          = Column(String(96), default='Something new for every visit')
    benefit2               = Column(String(255))                 # optional 2nd benefit line
    benefit3               = Column(String(255))                 # optional 3rd benefit line
    accent_color           = Column(String(9), default='#C5A03C')  # gold
    bg_color               = Column(String(9), default='#121212')  # near-black
    logo_url               = Column(String(512))
    card_website           = Column(String(255))                 # shown on card back, e.g. currybliss.com
    card_url               = Column(String(512))                 # last rendered preview
    created_at             = Column(DateTime, default=datetime.utcnow)

class VipSubscriber(Base):
    __tablename__ = 'vip_subscribers'
    id                     = Column(Integer, primary_key=True, index=True)
    restaurant_id          = Column(Integer, nullable=False, index=True)
    # Identity: phone for SMS/voice sessions, else the browser session id.
    identifier             = Column(String(128), nullable=False, index=True)
    phone                  = Column(String(20))
    email                  = Column(String(255))
    paypal_subscription_id = Column(String(64))
    status                 = Column(String(20), default='active')   # active/cancelled
    last_redeemed_at       = Column(DateTime)                        # once-a-day benefit redemption
    created_at             = Column(DateTime, default=datetime.utcnow)


# ── Self-learning agent skills (operator-taught intents) ─────────────────────
# An operator turns a failed turn into a new capability: a comment becomes an
# intent (trigger phrases + response + optional action) that the unified pipeline
# runs as a deterministic fast-path — no redeploy. Mirrors the VIP keyword path.

class AgentSkill(Base):
    __tablename__ = 'agent_skills'
    id              = Column(Integer, primary_key=True, index=True)
    restaurant_id   = Column(Integer, nullable=False, index=True)
    name            = Column(String(64), nullable=False)   # e.g. 'Raffle'
    trigger_phrases = Column(Text)                          # JSON list of phrases
    response        = Column(Text)                          # operator-approved reply
    action          = Column(String(32), default='none')   # none | raffle_entry
    source_log_id   = Column(Integer)                       # CallLog turn it was taught from
    active          = Column(Boolean, default=True)
    created_at      = Column(DateTime, default=datetime.utcnow)

class RaffleEntry(Base):
    __tablename__ = 'raffle_entries'
    id            = Column(Integer, primary_key=True, index=True)
    restaurant_id = Column(Integer, nullable=False, index=True)
    skill_id      = Column(Integer)
    name          = Column(String(128))
    email         = Column(String(255))   # collected so the winner can be notified
    phone         = Column(String(32))
    channel       = Column(String(16))    # sms | text | web_voice | voice
    session_id    = Column(String(128))
    won           = Column(Boolean, default=False)
    created_at    = Column(DateTime, default=datetime.utcnow)

class GuestBook(Base):
    """Hospitality-side guest CRM — DELIBERATELY SEPARATE from the Java app's
    apppod_beta.GUESTBOOK. Raffle/web signups land here, tenant-scoped."""
    __tablename__ = 'guestbook'
    id            = Column(Integer, primary_key=True, index=True)
    restaurant_id = Column(Integer, nullable=False, index=True)
    name          = Column(String(128))
    email         = Column(String(255), index=True)
    phone         = Column(String(32), index=True)
    source        = Column(String(32))    # how they joined: 'raffle', 'order', ...
    visits        = Column(Integer, default=0)
    total_spent   = Column(Float, default=0.0)
    notes         = Column(Text)
    last_visit    = Column(DateTime)
    created_at    = Column(DateTime, default=datetime.utcnow)


class SmsTestMessage(Base):
    """Dev SMS test console log — both outbound sends and inbound replies."""
    __tablename__ = 'sms_test_messages'
    id           = Column(Integer, primary_key=True, index=True)
    direction    = Column(String(4))    # out | in
    from_number  = Column(String(32))
    to_number    = Column(String(32))
    body         = Column(Text)
    status       = Column(String(32))   # sent | failed | received
    message_uuid = Column(String(64))
    created_at   = Column(DateTime, default=datetime.utcnow)


def seed_vip_program():
    """Ensure the demo restaurant (id=1, Curry Bliss) has an active VIP program.
    Idempotent — safe to call on every startup."""
    db = SessionLocal()
    try:
        existing = db.query(VipProgram).filter(
            VipProgram.restaurant_id == 1, VipProgram.is_active == True).first()
        if not existing:
            db.add(VipProgram(
                restaurant_id=1,
                program_name='VIP',
                monthly_fee_cents=500,
                recurring_benefit='Free drink or chips every visit',
                visit_discount_percent=0.0,
                is_active=True,
            ))
            db.commit()
            print("[vip] seeded default VIP program for restaurant 1")
    except Exception as e:
        print(f"[vip] seed error: {e}")
    finally:
        db.close()


def _ensure_call_log_columns():
    """`create_all()` does NOT alter existing tables. Older deployments have a
    call_logs table without the session/annotation columns — add them in place.
    Idempotent and safe: skips anything already present, never drops data."""
    from sqlalchemy import inspect, text
    try:
        insp = inspect(engine)
        if 'call_logs' not in insp.get_table_names():
            return  # fresh DB — create_all already built it with every column
        existing = {c['name'] for c in insp.get_columns('call_logs')}
        adds = {
            'session_id':     "ADD COLUMN session_id VARCHAR(128)",
            'operator_reply': "ADD COLUMN operator_reply TEXT",
            'operator_note':  "ADD COLUMN operator_note TEXT",
            'reviewed_at':    "ADD COLUMN reviewed_at DATETIME",
        }
        clauses = [sql for col, sql in adds.items() if col not in existing]
        # Widen call_type so 'web_voice' fits (was VARCHAR(10)).
        clauses.append("MODIFY COLUMN call_type VARCHAR(16)")
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE call_logs " + ", ".join(clauses)))
        print(f"[migrate] call_logs: applied {len(clauses)} alteration(s)")
    except Exception as e:
        print(f"[migrate] call_logs alter skipped: {e}")


def _ensure_raffle_columns():
    """Add raffle_entries.email to deployments whose table predates it. Idempotent."""
    from sqlalchemy import inspect, text
    try:
        insp = inspect(engine)
        if 'raffle_entries' not in insp.get_table_names():
            return
        cols = {c['name'] for c in insp.get_columns('raffle_entries')}
        if 'email' not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE raffle_entries ADD COLUMN email VARCHAR(255)"))
            print("[migrate] raffle_entries: added email column")
    except Exception as e:
        print(f"[migrate] raffle_entries alter skipped: {e}")


def _ensure_restaurant_columns():
    """Add owner_id/slug to an existing restaurants table, and backfill slugs."""
    import re
    from sqlalchemy import inspect, text
    try:
        insp = inspect(engine)
        if 'restaurants' not in insp.get_table_names():
            return
        cols = {c['name'] for c in insp.get_columns('restaurants')}
        adds = []
        if 'owner_id' not in cols:
            adds.append("ADD COLUMN owner_id INT")
        if 'slug' not in cols:
            adds.append("ADD COLUMN slug VARCHAR(64)")
        if 'onboarded' not in cols:
            adds.append("ADD COLUMN onboarded TINYINT(1) DEFAULT 0")
        if 'requested_number' not in cols:
            adds.append("ADD COLUMN requested_number VARCHAR(20)")
        if adds:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE restaurants " + ", ".join(adds)))
            print(f"[migrate] restaurants: added {len(adds)} column(s)")
        # Backfill slugs for any rows missing one (e.g. the legacy demo tenant).
        db = SessionLocal()
        try:
            for r in db.query(Restaurant).filter((Restaurant.slug == None) | (Restaurant.slug == "")).all():
                base = re.sub(r'[^a-z0-9]+', '-', (r.name or 'store').lower()).strip('-') or 'store'
                slug, n = base, 2
                while db.query(Restaurant).filter(Restaurant.slug == slug, Restaurant.id != r.id).first():
                    slug, n = f"{base}-{n}", n + 1
                r.slug = slug
            db.commit()
        finally:
            db.close()
    except Exception as e:
        print(f"[migrate] restaurants alter skipped: {e}")


def _ensure_columns(table, coldefs):
    """Additive migration: add any missing columns. coldefs = {name: 'SQL TYPE …'}.
    Idempotent; existing rows get the column default."""
    from sqlalchemy import inspect, text
    try:
        insp = inspect(engine)
        if table not in insp.get_table_names():
            return
        existing = {c['name'] for c in insp.get_columns(table)}
        adds = [f"ADD COLUMN {name} {ddl}" for name, ddl in coldefs.items() if name not in existing]
        if not adds:
            return
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table} " + ", ".join(adds)))
        print(f"[migrate] {table}: added {len(adds)} column(s)")
    except Exception as e:
        print(f"[migrate] {table} alter skipped: {e}")


def _ensure_item_library_parity():
    """Bring menu_items / menu_categories / menu_modifier_options to full field
    parity with the Java Store schema (InventoryMerchant/ModifierMerchant/etc.)."""
    _ensure_columns('menu_items', {
        'alternate_name': 'VARCHAR(256)', 'code': 'VARCHAR(64)', 'sku': 'VARCHAR(64)',
        'price_type': 'VARCHAR(32)', 'unit_name': 'VARCHAR(32)', 'tax_id': 'VARCHAR(64)',
        'tax_rate': 'INT DEFAULT 0', 'is_radio': 'VARCHAR(8)',
        'modifier_group_id': 'VARCHAR(256)', 'modifier_group_name': 'VARCHAR(256)',
        'tags': 'VARCHAR(256)', 'flag_show': 'INT DEFAULT 1', 'flag_inv': 'INT DEFAULT 0',
        'flag_delete': 'TINYINT(1) DEFAULT 0', 'modified_time': 'BIGINT',
    })
    _ensure_columns('menu_categories', {
        'alternate_name': 'VARCHAR(256)', 'flag_cat': 'INT DEFAULT 0',
        'flag_delete': 'TINYINT(1) DEFAULT 0',
    })
    _ensure_columns('menu_modifier_options', {
        'alternate_name': 'VARCHAR(256)', 'square_id': 'VARCHAR(128)',
        'modifier_url': 'VARCHAR(512)', "default_flag": "VARCHAR(8) DEFAULT '0'",
        'flag_mod': 'INT DEFAULT 0',
    })


def init_db():
    Base.metadata.create_all(bind=engine)
    _ensure_call_log_columns()
    _ensure_raffle_columns()
    _ensure_restaurant_columns()
    _ensure_item_library_parity()
    seed_vip_program()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
