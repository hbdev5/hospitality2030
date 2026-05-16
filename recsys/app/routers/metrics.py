from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.database import get_db, CallLog
from datetime import datetime, timedelta

router = APIRouter()

@router.get("/api/metrics")
def get_metrics(db: Session = Depends(get_db)):
    today = datetime.utcnow().date()
    since = datetime.utcnow() - timedelta(hours=24)

    total_today  = db.query(func.count(CallLog.id)).filter(func.date(CallLog.timestamp) == today).scalar()
    avg_latency  = db.query(func.avg(CallLog.total_latency_ms)).filter(CallLog.timestamp >= since).scalar()
    avg_claude   = db.query(func.avg(CallLog.claude_latency_ms)).filter(CallLog.timestamp >= since).scalar()
    noise_count  = db.query(func.count(CallLog.id)).filter(CallLog.noise_flag == True, CallLog.timestamp >= since).scalar()
    success      = db.query(func.count(CallLog.id)).filter(CallLog.status == 'ok', CallLog.timestamp >= since).scalar()
    total_24h    = db.query(func.count(CallLog.id)).filter(CallLog.timestamp >= since).scalar()
    voice_count  = db.query(func.count(CallLog.id)).filter(CallLog.call_type == 'voice', CallLog.timestamp >= since).scalar()
    sms_count    = db.query(func.count(CallLog.id)).filter(CallLog.call_type == 'sms', CallLog.timestamp >= since).scalar()

    recent = db.query(CallLog).order_by(CallLog.timestamp.desc()).limit(20).all()

    return {
        "today_calls": total_today or 0,
        "avg_total_latency_ms": round(avg_latency or 0, 1),
        "avg_claude_latency_ms": round(avg_claude or 0, 1),
        "noise_flags": noise_count or 0,
        "success_rate": round((success / total_24h * 100) if total_24h else 0, 1),
        "voice_count": voice_count or 0,
        "sms_count": sms_count or 0,
        "recent_calls": [{
            "id": c.id,
            "type": c.call_type,
            "caller": c.caller_number[-4:] if c.caller_number else "????",
            "transcript": (c.transcript or "")[:60],
            "recommendation": (c.recommendation or "")[:80],
            "total_ms": c.total_latency_ms,
            "claude_ms": c.claude_latency_ms,
            "noise": c.noise_flag,
            "status": c.status,
            "time": c.timestamp.strftime("%H:%M") if c.timestamp else ""
        } for c in recent]
    }
