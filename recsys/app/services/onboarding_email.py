"""
Onboarding emails (spam-safe: email, not SMS).

- send_operator_welcome: a branded welcome to the merchant — AI agents answer
  calls, gamify (raffle), upload items (PDF / POS / photos), and the $1 Stripe
  activation. Lays out the 3 things the operator must do.
- send_number_request: notifies the admin (sagar@hostbuddy.io) to authorize a
  Plivo number purchase — numbers are NOT auto-bought.

All sends go through emailer.send_html (real SMTP if configured, else simulated).
"""
from app.config import get_settings
from app.services import emailer

_WRAP = ('<div style="font-family:-apple-system,Segoe UI,Arial,sans-serif;max-width:560px;'
         'margin:0 auto;color:#1f2430">{body}'
         '<p style="color:#9aa1b1;font-size:12px;margin-top:24px">HostBuddy · AI agents for local merchants</p></div>')


def send_operator_welcome(to_email, store_name, slug):
    s = get_settings()
    base = s.public_base_url.rstrip('/')
    catalog = f"{base}/r/{slug}/catalog"
    activate = s.stripe_activation_url or "#"
    body = f"""
      <h1 style="font-size:22px">Welcome to HostBuddy 👋</h1>
      <p style="font-size:15px;line-height:1.5"><b>{store_name}</b> is getting AI agents to
      <b>answer your phone calls</b>, take orders, and <b>gamify your business</b> (raffles,
      VIP perks) — so you capture every customer, even when you're slammed.</p>

      <div style="background:#f4f6fb;border:1px solid #e6eaf1;border-radius:12px;padding:16px;margin:18px 0">
        <div style="font-weight:700;margin-bottom:8px">3 quick steps to go live:</div>
        <p style="margin:8px 0"><b>1. Activate for $1</b> — confirm your account today.<br>
          <a href="{activate}" style="display:inline-block;margin-top:6px;background:#635bff;color:#fff;text-decoration:none;padding:11px 18px;border-radius:9px;font-weight:600">Activate — $1 today →</a></p>
        <p style="margin:14px 0 8px"><b>2. Import your items</b> — upload a PDF, connect your POS, or snap photos.<br>
          <a href="{catalog}" style="color:#2563EB">Upload your catalog →</a></p>
        <p style="margin:14px 0 0"><b>3. Connect your bank</b> — so order payments are deposited to you. <span style="color:#9aa1b1">(link sent after activation)</span></p>
      </div>

      <p style="font-size:14px;color:#5a6172">Your phone agent is ready — your dedicated number activates as soon as your
      account is approved. We'll email you the moment it's live.</p>
    """
    return emailer.send_html([to_email], f"Welcome to HostBuddy — activate {store_name}",
                             _WRAP.format(body=body), from_name="HostBuddy")


def send_number_request(store_name, owner_email, number, area_code, slug):
    s = get_settings()
    base = s.public_base_url.rstrip('/')
    from urllib.parse import urlencode
    auth_url = f"{base}/admin/authorize-number?" + urlencode({"slug": slug, "key": s.smstest_key})
    body = f"""
      <h2 style="font-size:18px">📞 Number authorization needed</h2>
      <p style="font-size:15px;line-height:1.5"><b>{store_name}</b> ({owner_email}) requested a phone number.</p>
      <table style="font-size:14px;border-collapse:collapse">
        <tr><td style="color:#8a93a6;padding:3px 12px 3px 0">Number</td><td><b>+{number}</b></td></tr>
        <tr><td style="color:#8a93a6;padding:3px 12px 3px 0">Area code</td><td>{area_code}</td></tr>
        <tr><td style="color:#8a93a6;padding:3px 12px 3px 0">Store</td><td>{store_name} ({slug})</td></tr>
      </table>
      <p style="margin:16px 0">
        <a href="{auth_url}" style="display:inline-block;background:#16a34a;color:#fff;text-decoration:none;padding:11px 18px;border-radius:9px;font-weight:600">Review &amp; authorize →</a>
      </p>
      <p style="font-size:13px;color:#9aa1b1">Opens a confirmation page — nothing is purchased until you click <b>Authorize &amp; buy</b> there (~$0.50/mo). Or the operator can complete the $1 activation.</p>
    """
    return emailer.send_html([s.admin_email], f"[Authorize] {store_name} requested +{number}",
                             _WRAP.format(body=body), from_name="HostBuddy")


def send_number_live(operator_email, store_name, number, sms=False):
    channels = ("Calls <b>and texts</b> both reach your AI agent."
                if sms else "Calls reach your AI agent now; texting activates shortly.")
    body = f"""
      <h2 style="font-size:20px">📞 Your number is live!</h2>
      <p style="font-size:15px;line-height:1.5"><b>+{number}</b> is now {store_name}'s phone line —
      it answers, takes orders, and captures every customer.</p>
      <p style="font-size:14px;color:#5a6172">{channels}</p>
    """
    return emailer.send_html([operator_email], f"📞 {store_name}: your phone number is live",
                             _WRAP.format(body=body), from_name="HostBuddy")
