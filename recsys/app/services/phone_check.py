"""
Phone validation via libphonenumber (offline, no API cost).

US note: the NANP doesn't encode mobile-vs-landline in the number itself, so US
numbers come back as FIXED_LINE_OR_MOBILE — we accept those as SMS-reachable.
Definite landlines (common internationally) and invalid numbers are rejected.
For a hard carrier-level mobile check, a paid lookup (Twilio Lookup line-type /
HLR) would be required.
"""


def validate_mobile(phone, default_region="US"):
    """Returns {ok, phone (E.164 or None), type, error}.

    type ∈ none|mobile|other|landline|invalid|unknown. ok=False only for
    landline/invalid. An empty phone is allowed (ok, type='none')."""
    phone = (phone or "").strip()
    if not phone:
        return {"ok": True, "phone": None, "type": "none", "error": ""}

    try:
        import phonenumbers
        from phonenumbers import (PhoneNumberType, number_type, is_valid_number,
                                  format_number, PhoneNumberFormat)
    except ImportError:
        # Library missing — never block a signup over a missing dep.
        return {"ok": True, "phone": phone, "type": "unknown", "error": ""}

    try:
        num = phonenumbers.parse(phone, default_region)
    except Exception:
        return {"ok": False, "phone": None, "type": "invalid",
                "error": "That phone number doesn't look valid."}

    if not is_valid_number(num):
        return {"ok": False, "phone": None, "type": "invalid",
                "error": "That phone number doesn't look valid."}

    e164 = format_number(num, PhoneNumberFormat.E164)
    t = number_type(num)
    if t == PhoneNumberType.FIXED_LINE:
        return {"ok": False, "phone": e164, "type": "landline",
                "error": "That looks like a landline — please enter a mobile so we can text you."}
    if t in (PhoneNumberType.MOBILE, PhoneNumberType.FIXED_LINE_OR_MOBILE):
        return {"ok": True, "phone": e164, "type": "mobile", "error": ""}
    return {"ok": True, "phone": e164, "type": "other", "error": ""}
