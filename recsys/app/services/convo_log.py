"""
Centralised conversation logging.

Every consumer-facing channel — phone voice (voice_ws), browser voice
(voice_web), SMS (plivo_hooks), and browser Text-to-Order (text_chat) — records
one CallLog row per turn through here. That gives the operator console a single,
complete record of every conversation across all channels, and a row to hang
human annotations on (operator_reply / operator_note).

Logging must NEVER break a live reply, so every failure is swallowed and logged.
"""

from app.database import SessionLocal, CallLog


def log_turn(call_type, transcript, recommendation, *,
             restaurant_id=None, caller_number=None, session_id=None,
             claude_latency_ms=0.0, total_latency_ms=0.0,
             noise_flag=False, status="ok"):
    """Persist one conversation turn. Returns the new row id, or None on failure."""
    db = SessionLocal()
    try:
        row = CallLog(
            restaurant_id     = restaurant_id,
            call_type         = call_type,
            caller_number     = caller_number,
            session_id        = session_id,
            transcript        = transcript,
            recommendation    = recommendation,
            claude_latency_ms = claude_latency_ms or 0.0,
            total_latency_ms  = total_latency_ms or 0.0,
            noise_flag        = bool(noise_flag),
            status            = status,
        )
        db.add(row)
        db.commit()
        return row.id
    except Exception as e:
        db.rollback()
        print(f"[convo_log] failed to log {call_type} turn: {e}")
        return None
    finally:
        db.close()
