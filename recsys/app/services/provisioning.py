"""
Phone-number provisioning via Plivo (onboarding "create phone" step).

Voice routes per-tenant by the CALLED number: every provisioned number is
attached to ONE Plivo Application whose answer_url points at THIS deployment's
voice inbound (…/aie26/api/voice/inbound), and plivo_hooks resolves the tenant
from `To`. New numbers are VOICE-ONLY by default — SMS stays off until the
merchant's 10DLC campaign is registered.

  search_numbers(area_code)         → available local voice numbers (free, read-only)
  provision_number(rid, number)     → BUY the number + attach voice app + assign (BILLABLE)
"""
from app.config import get_settings
from app.database import SessionLocal, Restaurant

settings = get_settings()


def _client():
    import plivo
    return plivo.RestClient(settings.plivo_auth_id, settings.plivo_auth_token)


def _voice_answer_url() -> str:
    return settings.public_base_url.rstrip('/') + "/api/voice/inbound"


def _sms_message_url() -> str:
    return settings.public_base_url.rstrip('/') + "/api/sms/inbound"


def search_numbers(area_code: str, limit: int = 6) -> dict:
    """Available local, voice-capable numbers for an area code. Read-only / free."""
    area_code = (area_code or "").strip()
    if not (area_code.isdigit() and len(area_code) == 3):
        return {"ok": False, "error": "Enter a 3-digit US area code.", "numbers": []}
    try:
        resp = _client().numbers.search(country_iso="US", type="local",
                                        pattern=area_code, services="voice")
        objs = getattr(resp, "objects", None) or resp
        nums = [{
            "number": getattr(n, "number", None),
            "rate": getattr(n, "monthly_rental_rate", None),
            "voice": getattr(n, "voice_enabled", None),
            "sms": getattr(n, "sms_enabled", None),
        } for n in objs if getattr(n, "number", None)]
        return {"ok": True, "numbers": nums[:limit]}
    except Exception as e:
        print(f"[provision] search err: {e}")
        return {"ok": False, "error": f"Search failed: {e}", "numbers": []}


def ensure_voice_app(client) -> str:
    """Find (or create) this deployment's Plivo Application — answer_url → voice
    inbound AND message_url → SMS inbound, so attached numbers route BOTH to this
    app (tenant resolved by the called/To number). Returns its app_id."""
    answer, message = _voice_answer_url(), _sms_message_url()
    try:
        apps = client.applications.list()
        for a in (getattr(apps, "objects", None) or apps):
            if getattr(a, "answer_url", "") == answer or getattr(a, "app_name", "") == "aie26-voice":
                app_id = getattr(a, "app_id", None)
                if getattr(a, "message_url", "") != message:   # backfill SMS routing
                    try:
                        client.applications.update(app_id, message_url=message, message_method="POST")
                    except Exception as e:
                        print(f"[provision] app message_url update err: {e}")
                return app_id
        created = client.applications.create(
            app_name="aie26-voice", answer_url=answer, answer_method="POST",
            message_url=message, message_method="POST")
        return getattr(created, "app_id", None) or (created.get("app_id") if isinstance(created, dict) else None)
    except Exception as e:
        print(f"[provision] ensure_voice_app err: {e}")
        return None


def campaign_status(client):
    """(linked_count, pool_limit) for the configured 10DLC campaign."""
    cid = settings.plivo_campaign_id
    if not cid:
        return 0, 0
    try:
        r = client.campaign.get_numbers(cid)
        count = len(getattr(r, "phone_numbers", []) or [])
        limit = getattr(r, "number_pool_limit", 49) or 49
        return count, limit
    except Exception as e:
        print(f"[provision] campaign_status err: {e}")
        return 0, 49


def link_number_to_campaign(client, number) -> dict:
    """Link a number to the approved 10DLC campaign so SMS is compliant.
    Guards against the per-campaign pool limit (49)."""
    cid = settings.plivo_campaign_id
    if not cid:
        return {"ok": False, "info": "no campaign configured"}
    count, limit = campaign_status(client)
    if count >= limit:
        return {"ok": False, "info": f"campaign {cid} full ({count}/{limit})"}
    try:
        client.campaign.number_link(cid, numbers=[number])
        return {"ok": True, "campaign": cid}
    except Exception as e:
        print(f"[provision] number_link err: {e}")
        return {"ok": False, "info": str(e)}


def provision_number(restaurant_id: int, number: str) -> dict:
    """BUY the number (billable), attach the voice app, assign to the store."""
    number = (number or "").strip()
    if not number:
        return {"ok": False, "error": "No number selected."}
    try:
        client = _client()
        app_id = ensure_voice_app(client)
        kwargs = {"number": number}
        if app_id:
            kwargs["app_id"] = app_id
        client.numbers.buy(**kwargs)
        # Make sure routing is attached even if buy ignored app_id.
        if app_id:
            try:
                client.numbers.update(number, app_id=app_id)
            except Exception as e:
                print(f"[provision] attach app err: {e}")
    except Exception as e:
        print(f"[provision] buy err: {e}")
        return {"ok": False, "error": f"Could not provision: {e}"}

    # Link to the approved 10DLC campaign → compliant SMS (voice already routes via app).
    sms = link_number_to_campaign(client, number)

    db = SessionLocal()
    try:
        r = db.query(Restaurant).filter(Restaurant.id == restaurant_id).first()
        if r:
            r.plivo_number = "+" + number if not number.startswith("+") else number
            db.commit()
    finally:
        db.close()
    return {"ok": True, "number": number, "voice": True,
            "sms": sms.get("ok", False), "sms_info": sms.get("info") or sms.get("campaign"),
            "app_id": app_id}
