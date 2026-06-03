"""
PayPal REST API client — Orders v2.

Two operations used by the checkout flow:
  - create_order(amount_cents, currency, return_url, cancel_url) → paypal_order_id
  - capture_order(paypal_order_id) → capture dict (status COMPLETED if success)

Auth is OAuth2 client_credentials. Tokens cached in-process until ~30s before expiry.
"""

import time
import httpx
from app.config import get_settings

settings = get_settings()

_API_BASE = {
    "sandbox": "https://api-m.sandbox.paypal.com",
    "live":    "https://api-m.paypal.com",
}

_token_cache = {"access_token": "", "expires_at": 0.0}


def _api_base() -> str:
    return _API_BASE.get(settings.paypal_mode, _API_BASE["sandbox"])


def _get_access_token() -> str:
    now = time.time()
    if _token_cache["access_token"] and _token_cache["expires_at"] - 30 > now:
        return _token_cache["access_token"]

    if not (settings.paypal_client_id and settings.paypal_secret):
        raise RuntimeError("PAYPAL_CLIENT_ID / PAYPAL_SECRET not configured")

    resp = httpx.post(
        f"{_api_base()}/v1/oauth2/token",
        auth=(settings.paypal_client_id, settings.paypal_secret),
        data={"grant_type": "client_credentials"},
        headers={"Accept": "application/json"},
        timeout=10.0,
    )
    resp.raise_for_status()
    body = resp.json()
    _token_cache["access_token"] = body["access_token"]
    _token_cache["expires_at"]   = now + float(body.get("expires_in", 3000))
    return _token_cache["access_token"]


def create_order(amount_cents: int, currency: str = "USD",
                 return_url: str = "", cancel_url: str = "",
                 reference_id: str = "") -> str:
    """Create a PayPal order. Returns the PayPal order id (backward compat)."""
    return create_order_with_link(amount_cents, currency, return_url, cancel_url, reference_id)["id"]


def create_order_with_link(amount_cents: int, currency: str = "USD",
                           return_url: str = "", cancel_url: str = "",
                           reference_id: str = "") -> dict:
    """
    Create a PayPal order and return both id AND the hosted approval URL.
    The approval URL is a paypal.com link — buyer can pay with PayPal balance,
    Pay Later, Pay in 4, Venmo, or guest card. SMS-safe (trusted domain).

    Returns: {"id": "...", "approve_url": "https://www.paypal.com/checkoutnow?token=..."}
    """
    amount_str = f"{amount_cents / 100:.2f}"
    payload = {
        "intent": "CAPTURE",
        "purchase_units": [{
            "reference_id": reference_id or "default",
            "amount": {"currency_code": currency, "value": amount_str},
        }],
        "application_context": {
            "user_action": "PAY_NOW",
            "shipping_preference": "NO_SHIPPING",
            **({"return_url": return_url} if return_url else {}),
            **({"cancel_url": cancel_url} if cancel_url else {}),
        },
    }
    resp = httpx.post(
        f"{_api_base()}/v2/checkout/orders",
        headers={
            "Authorization": f"Bearer {_get_access_token()}",
            "Content-Type":  "application/json",
        },
        json=payload,
        timeout=15.0,
    )
    resp.raise_for_status()
    body = resp.json()
    approve = ""
    for link in body.get("links", []):
        # PayPal returns rel="payer-action" (Orders v2) or rel="approve" (legacy)
        if link.get("rel") in ("payer-action", "approve"):
            approve = link.get("href", "")
            break
    return {"id": body["id"], "approve_url": approve}


def capture_order(paypal_order_id: str) -> dict:
    """Capture an approved PayPal order. Returns the capture response."""
    resp = httpx.post(
        f"{_api_base()}/v2/checkout/orders/{paypal_order_id}/capture",
        headers={
            "Authorization": f"Bearer {_get_access_token()}",
            "Content-Type":  "application/json",
        },
        timeout=15.0,
    )
    resp.raise_for_status()
    return resp.json()


# ── Subscriptions (VIP membership) ────────────────────────────────────────────
# PayPal Subscriptions API: a Product + a monthly Billing Plan are created once
# and their ids cached on the VipProgram row. The browser then approves a
# Subscription against that plan_id (JS SDK buttons) and we verify server-side.

def _auth_headers() -> dict:
    return {
        "Authorization": f"Bearer {_get_access_token()}",
        "Content-Type":  "application/json",
    }


def create_product(name: str, description: str = "") -> str:
    resp = httpx.post(
        f"{_api_base()}/v1/catalogs/products",
        headers=_auth_headers(),
        json={
            "name": name,
            "description": description or name,
            "type": "SERVICE",
            # category is an optional MCC-derived enum; PayPal 400s on bad values
            # and there's no clean restaurant code, so we omit it.
        },
        timeout=15.0,
    )
    resp.raise_for_status()
    return resp.json()["id"]


def create_monthly_plan(product_id: str, name: str, amount_cents: int,
                        currency: str = "USD") -> str:
    """Create an ACTIVE monthly billing plan (infinite cycles)."""
    amount_str = f"{amount_cents / 100:.2f}"
    payload = {
        "product_id": product_id,
        "name": name,
        "status": "ACTIVE",
        "billing_cycles": [{
            "frequency": {"interval_unit": "MONTH", "interval_count": 1},
            "tenure_type": "REGULAR",
            "sequence": 1,
            "total_cycles": 0,   # 0 = until cancelled
            "pricing_scheme": {
                "fixed_price": {"value": amount_str, "currency_code": currency}
            },
        }],
        "payment_preferences": {
            "auto_bill_outstanding": True,
            "setup_fee_failure_action": "CONTINUE",
            "payment_failure_threshold": 3,
        },
    }
    resp = httpx.post(
        f"{_api_base()}/v1/billing/plans",
        headers=_auth_headers(),
        json=payload,
        timeout=15.0,
    )
    resp.raise_for_status()
    return resp.json()["id"]


def ensure_subscription_plan(program, db) -> str:
    """Return the PayPal plan id for a VipProgram, creating product+plan on first
    use and caching both ids on the row. `program` is a VipProgram, `db` an
    active Session used to persist the cached ids."""
    if program.paypal_plan_id:
        return program.paypal_plan_id
    product_id = program.paypal_product_id or create_product(
        name=f"{program.program_name} Membership",
        description=f"{program.program_name} membership — {program.recurring_benefit}",
    )
    plan_id = create_monthly_plan(
        product_id=product_id,
        name=f"{program.program_name} Monthly",
        amount_cents=program.monthly_fee_cents or 500,
    )
    program.paypal_product_id = product_id
    program.paypal_plan_id    = plan_id
    db.commit()
    print(f"[paypal] created VIP product={product_id} plan={plan_id}")
    return plan_id


def get_subscription(subscription_id: str) -> dict:
    """Fetch a subscription so we can verify it's ACTIVE/APPROVED before
    persisting the subscriber."""
    resp = httpx.get(
        f"{_api_base()}/v1/billing/subscriptions/{subscription_id}",
        headers=_auth_headers(),
        timeout=15.0,
    )
    resp.raise_for_status()
    return resp.json()


def create_subscription(plan_id: str, custom_id: str = "",
                        return_url: str = "", cancel_url: str = "") -> dict:
    """Server-side subscription create (fallback for a non-JS link). Returns
    {"id": ..., "approve_url": ...}."""
    payload = {
        "plan_id": plan_id,
        **({"custom_id": custom_id} if custom_id else {}),
        "application_context": {
            "user_action": "SUBSCRIBE_NOW",
            "shipping_preference": "NO_SHIPPING",
            **({"return_url": return_url} if return_url else {}),
            **({"cancel_url": cancel_url} if cancel_url else {}),
        },
    }
    resp = httpx.post(
        f"{_api_base()}/v1/billing/subscriptions",
        headers=_auth_headers(),
        json=payload,
        timeout=15.0,
    )
    resp.raise_for_status()
    body = resp.json()
    approve = ""
    for link in body.get("links", []):
        if link.get("rel") == "approve":
            approve = link.get("href", "")
            break
    return {"id": body["id"], "approve_url": approve}
