from sqlalchemy import create_engine, Column, Integer, String, Text, Float, DateTime, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
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

class Playbook(Base):
    __tablename__ = 'playbooks'
    id            = Column(Integer, primary_key=True, index=True)
    restaurant_id = Column(Integer)
    week_of       = Column(String(20))
    campaigns_json= Column(Text)
    status        = Column(String(20), default='DRAFT')
    created_at    = Column(DateTime, default=datetime.utcnow)

def init_db():
    Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
