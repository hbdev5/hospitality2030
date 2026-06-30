from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    openai_api_key: str = ""       # primary LLM — gpt-4o-mini
    anthropic_api_key: str = ""    # kept for fallback / future use
    plivo_auth_id: str = ""
    plivo_auth_token: str = ""
    plivo_number: str = ""
    plivo_voice_app_id: str = ""   # Plivo Application (answer_url → this deploy's voice inbound)
    plivo_campaign_id:  str = "CNM8H6W"   # approved 10DLC campaign — new numbers link here for SMS
    db_url: str = "mysql+pymysql://recsys:recsys2026@localhost/recsys"
    base_path: str = "/recsys"
    secret_key: str = "recsys-hostbuddy-2026"
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = "21m00Tcm4TlvDq8ikWAM"
    deepgram_api_key: str = ""           # kept for voice_web.py REST transcription
    google_credentials: str = ""         # path to service account JSON (set via GOOGLE_APPLICATION_CREDENTIALS env var)

    google_application_credentials: str = ""  # service account JSON path

    # PayPal — sandbox by default. Get keys at developer.paypal.com → Apps & Credentials.
    paypal_client_id: str = ""
    paypal_secret:    str = ""
    paypal_mode:      str = "sandbox"   # "sandbox" or "live"

    # Public origin used to build SMS/checkout links (no trailing slash).
    public_base_url:  str = "https://support.hostbuddy.io/recsys"

    # Dev-only SMS test console (/smsTest). If set, the page + APIs require
    # ?key=<this>. Left blank = open (local dev). Not linked from any operator UI.
    smstest_key:      str = ""

    # Google OAuth (merchant login). Create a "Web application" OAuth client in
    # Google Cloud Console; redirect URI = <public_base_url>/auth/google/callback.
    google_oauth_client_id:     str = ""
    google_oauth_client_secret: str = ""

    # Email (SRM weekly campaigns). Any free SMTP works — e.g. Gmail app password,
    # Brevo, SendGrid SMTP. If unset, SRM "sends" are simulated (recorded only).
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_pass: str = ""
    smtp_from: str = ""   # display From; defaults to smtp_user

    # Onboarding / billing
    admin_email:           str = "sagar@hostbuddy.io"   # number-purchase authorizations land here
    stripe_activation_url: str = ""                      # $1 activation Stripe Payment Link

    # Gmail API send (OAuth, gmail.send scope). No password stored — a one-time
    # consent mints a send-only refresh token (saved to a file, not .env).
    gmail_client_id:     str = ""
    gmail_client_secret: str = ""
    gmail_sender:        str = "sagar@hostbuddy.io"

    class Config:
        env_file = ".env"
        extra = "ignore"   # don't fail on unknown env vars


@lru_cache()
def get_settings():
    return Settings()
