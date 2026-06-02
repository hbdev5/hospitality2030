"""
Checkout flow — PayPal Smart Buttons.

Routes:
  GET  /checkout/{order_id}          → page with PayPal Smart Buttons
  POST /api/paypal/create/{order_id} → creates PayPal order, returns paypal_order_id
  POST /api/paypal/capture/{order_id}→ captures payment, marks Order paid
"""

from datetime import datetime
from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
import os, json as _json

from app.database import get_db, Order
from app.config   import get_settings
from app.services import paypal as paypal_svc

router    = APIRouter()
settings  = get_settings()
templates = Jinja2Templates(directory=os.path.expanduser("~/work/recsys/templates"))


@router.get("/checkout/{order_id}", response_class=HTMLResponse)
async def checkout_page(order_id: int, request: Request, db: Session = Depends(get_db)):
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    # If the buyer is returning from paypal.com (SMS flow), the URL has ?token=PAYPAL_ID.
    # Auto-capture and mark the order paid before rendering.
    params = request.query_params
    token  = params.get("token") or order.payment_id
    if order.status != "paid" and params.get("paypal") == "1" and token:
        try:
            result = paypal_svc.capture_order(token)
            if result.get("status") == "COMPLETED":
                order.status         = "paid"
                order.payment_id     = token
                order.payment_provider = "paypal"
                order.paid_at        = datetime.utcnow()
                db.commit()
                print(f"[paypal] order {order_id} captured on return from paypal.com")
        except Exception as e:
            print(f"[paypal] capture-on-return failed: {e}")

    cancelled = params.get("cancel") == "1"

    # Parse line items for the receipt block
    items = []
    try:
        raw = _json.loads(order.items_json or "[]")
        for it in raw:
            if not isinstance(it, dict): continue
            qty   = it.get("quantity") or 1
            name  = (it.get("name") or "").strip()
            price = float(it.get("price") or 0.0)
            mods  = it.get("modifiers") or []
            size  = (it.get("size") or "").strip()
            note  = (it.get("special_instructions") or "").strip()
            items.append({
                "qty":   qty,
                "name":  name,
                "price": f"{price * qty:.2f}" if price else "",
                "mods":  mods,
                "size":  size,
                "note":  note,
            })
    except Exception as e:
        print(f"[checkout] items parse error: {e}")

    return templates.TemplateResponse(
        request=request,
        name="checkout.html",
        context={
            "base":             settings.base_path,
            "order_id":         order.id,
            "amount":           f"{order.total_cents / 100:.2f}",
            "items":            items,
            "status":           order.status,
            "paid":             order.status == "paid",
            "cancelled":        cancelled,
            "paypal_client_id": settings.paypal_client_id,
            "currency":         "USD",
        },
    )


@router.post("/api/paypal/create/{order_id}")
async def paypal_create(order_id: int, db: Session = Depends(get_db)):
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.status == "paid":
        raise HTTPException(status_code=400, detail="Order already paid")
    if order.total_cents <= 0:
        raise HTTPException(status_code=400, detail="Order total is zero — cannot charge")

    try:
        paypal_id = paypal_svc.create_order(
            amount_cents = order.total_cents,
            reference_id = str(order.id),
        )
    except Exception as e:
        print(f"[paypal] create error: {e}")
        raise HTTPException(status_code=502, detail=f"PayPal create failed: {e}")

    order.payment_provider = "paypal"
    order.payment_id       = paypal_id
    db.commit()
    return JSONResponse({"id": paypal_id})


@router.post("/api/paypal/capture/{order_id}")
async def paypal_capture(order_id: int, request: Request, db: Session = Depends(get_db)):
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    body      = await request.json()
    paypal_id = body.get("paypal_order_id") or order.payment_id
    if not paypal_id:
        raise HTTPException(status_code=400, detail="Missing paypal_order_id")

    try:
        result = paypal_svc.capture_order(paypal_id)
    except Exception as e:
        print(f"[paypal] capture error: {e}")
        raise HTTPException(status_code=502, detail=f"PayPal capture failed: {e}")

    if result.get("status") == "COMPLETED":
        order.status   = "paid"
        order.paid_at  = datetime.utcnow()
        db.commit()
        print(f"[paypal] order {order_id} paid (paypal id {paypal_id})")
        return JSONResponse({"status": "paid", "paypal": result})

    return JSONResponse({"status": result.get("status", "unknown"), "paypal": result})
