from sqlalchemy import create_engine, Column, Integer, String, Text, Float, DateTime, Boolean, Enum, ForeignKey, Index
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from datetime import datetime
import os, sys
sys.path.insert(0, os.path.expanduser('~/work/recsys'))
from app.config import get_settings

settings = get_settings()
engine = create_engine(settings.db_url, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class Restaurant(Base):
    __tablename__ = 'restaurants'
    id          = Column(Integer, primary_key=True, index=True)
    name        = Column(String(255), nullable=False)
    plivo_number= Column(String(20), unique=True)
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
    call_type         = Column(String(10))   # voice / sms
    caller_number     = Column(String(20))
    transcript        = Column(Text)
    recommendation    = Column(Text)
    claude_latency_ms = Column(Float)
    total_latency_ms  = Column(Float)
    noise_flag        = Column(Boolean, default=False)
    status            = Column(String(10), default='ok')
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


def init_db():
    Base.metadata.create_all(bind=engine)
    seed_vip_program()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
