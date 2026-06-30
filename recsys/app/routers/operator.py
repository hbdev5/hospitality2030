"""
Operator console API — observability + human-in-the-loop annotation.

  GET  /api/conversations              recent turns across ALL channels (full text)
  POST /api/conversations/{id}/annotate  operator edits the reply and/or leaves a note

Backs the Conversations tab in the Agent Studio console, where an operator can
read every logged conversation and, via the ✏️ pencil, correct a response or add
a comment. Edits are stored on the CallLog row (operator_reply / operator_note)
and never overwrite the original AI transcript.
"""

from fastapi import APIRouter, Request, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from datetime import datetime

from app.database import get_db, CallLog

router = APIRouter()


def _rid(request):
    """Scope to the logged-in merchant's store; fall back to the demo tenant."""
    try:
        return request.session.get('restaurant_id') or 1
    except Exception:
        return 1


def _serialize(c: CallLog) -> dict:
    return {
        "id":             c.id,
        "type":           c.call_type or "",
        "caller":         c.caller_number or "",
        "session_id":     c.session_id or "",
        "transcript":     c.transcript or "",
        "recommendation": c.recommendation or "",
        "operator_reply": c.operator_reply or "",
        "operator_note":  c.operator_note or "",
        "reviewed":       bool(c.reviewed_at),
        "total_ms":       c.total_latency_ms,
        "noise":          bool(c.noise_flag),
        "status":         c.status or "",
        "time":           c.timestamp.strftime("%Y-%m-%d %H:%M") if c.timestamp else "",
    }


@router.get("/api/conversations")
def list_conversations(request: Request, limit: int = 100, db: Session = Depends(get_db)):
    """Most-recent turns first (full text), scoped to the logged-in store."""
    limit = max(1, min(limit, 500))
    rows = (db.query(CallLog)
              .filter(CallLog.restaurant_id == _rid(request))
              .order_by(CallLog.timestamp.desc())
              .limit(limit).all())
    return {"turns": [_serialize(r) for r in rows]}


@router.post("/api/conversations/{log_id}/annotate")
async def annotate(log_id: int, request: Request, db: Session = Depends(get_db)):
    """Operator override: edit the response and/or leave a comment on a turn."""
    body = await request.json()
    row = (db.query(CallLog)
             .filter(CallLog.id == log_id, CallLog.restaurant_id == _rid(request)).first())
    if not row:
        return JSONResponse({"error": "conversation turn not found"}, status_code=404)

    if "reply" in body:
        row.operator_reply = (body.get("reply") or "").strip() or None
    if "note" in body:
        row.operator_note = (body.get("note") or "").strip() or None
    row.reviewed_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return _serialize(row)
